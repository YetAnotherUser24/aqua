"""
Unified event stream: append to the database, push to the live view.

Events are the audit record of what happened and why. Unlike rollups — which are
derived and re-derivable — an event that is lost is lost, so this module favours
keeping them over staying simple:

* In-memory buffer with a bounded size while the database is unavailable.
* On overflow, spill to a local JSONL file rather than dropping. An event log that
  silently loses its most interesting entries, which are the faults, is worse than no
  event log at all, because it implies coverage it does not have.
* On reconnect, drain the spill back into the table.

Writes are batched. At a few events a minute the volume is trivial, but the *number*
of transactions is not — on an SD-backed filesystem, one insert carrying twenty rows
is much cheaper than twenty inserts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Mapping, Sequence

from ..db.engine import Database, DatabaseUnavailable
from .model import Event, EventType, Severity, Source

log = logging.getLogger("aqua.events")

INSERT_SQL = """
INSERT INTO events (
    ts, t_dev_ms, experiment_id, event_type, severity, scope, source,
    node_id, correlation_id, action, detail
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid, $9, $10, $11::jsonb)
"""

#: Columns the API may filter on, mapped to SQL. Whitelisted because a filter string
#: assembled from user input is the classic way a read-only endpoint becomes a write
#: endpoint.
_FILTERS: dict[str, str] = {
    "experiment_id": "experiment_id = $",
    "node_id": "node_id = $::uuid",
    "event_type": "event_type = $",
    "severity": "severity = $",
    "scope": "scope = $",
    "source": "source = $",
}


class EventStream:
    """Buffered, batched writer for the unified event stream."""

    def __init__(
        self,
        db: Database,
        *,
        buffer_max: int = 2_000,
        spill_path: Path | None = None,
        flush_interval_s: float = 5.0,
    ) -> None:
        self.db = db
        self.buffer_max = buffer_max
        self.spill_path = spill_path
        self.flush_interval_s = flush_interval_s

        self._buffer: Deque[Event] = deque()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

        self.written = 0
        self.spilled = 0
        self.replayed = 0
        self.rejected = 0
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._flush_forever(), name="events-flush")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Best effort: on a clean shutdown, get what is buffered into the table.
        await self.flush()

    async def _flush_forever(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self.flush_interval_s)
            try:
                await self.replay_spill()
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("event flush loop iteration failed")

    # -- writing -----------------------------------------------------------

    async def emit(
        self,
        event: Event,
        *,
        live: Any | None = None,
        persist: bool = True,
    ) -> None:
        """
        Record an event.

        Never raises. An event-stream failure must not propagate into the MQTT ingest
        loop, because that would let a database problem stop telemetry — the one thing
        that must keep working.
        """
        if live is not None:
            # Push first: the dashboard should see the event even if persistence is
            # about to fail, and the live view is cheap.
            try:
                await live.publish(
                    {
                        "kind": "event",
                        "event_type": event.event_type.value,
                        "severity": event.severity.value,
                        "scope": event.scope.value,
                        "ts": event.ts,
                        "node_id": event.node_id,
                        "detail": dict(event.detail),
                    }
                )
            except Exception:  # noqa: BLE001
                log.debug("live event publish failed", exc_info=True)

        if not persist:
            return

        async with self._lock:
            self._buffer.append(event)
            if len(self._buffer) > self.buffer_max:
                self._spill_one(self._buffer.popleft())

        if len(self._buffer) >= 64:
            await self.flush()

    async def flush(self) -> int:
        """Write buffered events. Returns the number written."""
        async with self._lock:
            if not self._buffer:
                return 0
            batch = list(self._buffer)
            self._buffer.clear()

        try:
            await self.db.executemany(INSERT_SQL, [_as_row(event) for event in batch])
        except DatabaseUnavailable as exc:
            # The database is down. Keep everything for the next attempt.
            self.last_error = str(exc)
            async with self._lock:
                self._buffer.extendleft(reversed(batch))
                while len(self._buffer) > self.buffer_max:
                    self._spill_one(self._buffer.popleft())
            log.warning("event write failed, %d buffered", len(batch), extra={"error": str(exc)})
            return 0
        except Exception as exc:  # noqa: BLE001
            # The database is up but refused the batch. One row holding a value its column
            # cannot take fails the whole ``executemany``, and because the batch is retried
            # verbatim, that single row stops the entire event log from ever advancing —
            # the exact silent loss this module exists to prevent. Retry row by row so the
            # good events land and only the offending row is isolated.
            log.warning(
                "event batch rejected; retrying row by row",
                extra={"error": str(exc), "count": len(batch)},
            )
            written = await self._write_individually(batch)
            self.written += written
            return written

        self.written += len(batch)
        self.last_error = None
        log.debug("wrote %d event(s)", len(batch))
        return len(batch)

    async def _write_individually(self, batch: list[Event]) -> int:
        """
        Insert one event at a time, quarantining any row the database will not take.

        A batch that fails as a whole must not be retried as a whole forever: the same
        row fails every time, so the log never advances. Row by row bounds the failure to
        the one event carrying the bad value.
        """
        written = 0
        rejected = 0
        for index, event in enumerate(batch):
            try:
                await self.db.execute(INSERT_SQL, *_as_row(event))
                written += 1
            except DatabaseUnavailable as exc:
                # The database went away mid-retry. Hold the remainder for the next tick.
                self.last_error = str(exc)
                async with self._lock:
                    self._buffer.extendleft(reversed(batch[index:]))
                return written
            except Exception as exc:  # noqa: BLE001
                rejected += 1
                self._reject_one(event, exc)
        if rejected:
            self.last_error = f"{rejected} event(s) quarantined as unstoreable"
        return written

    # -- spill -------------------------------------------------------------

    def _spill_one(self, event: Event) -> None:
        """
        Append an event to the local spill file.

        Deliberately a plain append of one JSON line: cheap, crash-safe, and readable
        with ``jq`` when someone needs to know what happened during an outage.
        """
        if self.spill_path is None:
            self.spilled += 1
            log.error(
                "event buffer full with no spill path configured; an event was DROPPED",
                extra={"event_type": event.event_type.value},
            )
            return
        try:
            self.spill_path.parent.mkdir(parents=True, exist_ok=True)
            with self.spill_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_as_dict(event), separators=(",", ":")) + "\n")
            self.spilled += 1
        except OSError:
            self.spilled += 1
            log.exception("could not spill event; it is now DROPPED")

    def _reject_one(self, event: Event, error: Exception) -> None:
        """
        Quarantine an event the table will never accept.

        Written next to the spill file but **never replayed** — replaying it would fail
        the same way on every tick and pin the queue forever. Kept on disk so the record
        outlives a reboot and can be read with ``jq``: the point of an audit log is that a
        lost entry is visible, not quietly missing.
        """
        self.rejected += 1
        log.error(
            "quarantining an event the database cannot store",
            extra={"event_type": event.event_type.value, "error": str(error)},
        )
        if self.spill_path is None:
            return
        try:
            path = self.spill_path.with_name(self.spill_path.name + ".rejected")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_as_dict(event), separators=(",", ":")) + "\n")
        except OSError:
            log.exception("could not quarantine the rejected event")

    def spill_count(self) -> int:
        if self.spill_path is None or not self.spill_path.exists():
            return 0
        with self.spill_path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    async def replay_spill(self) -> int:
        """
        Move spilled events into the table, then truncate the spill file.

        Truncation happens only after a successful write, so a crash mid-replay
        duplicates a few events rather than losing them. Duplicates are the correct
        failure direction for an audit log.
        """
        if self.spill_path is None or not self.spill_path.exists():
            return 0

        lines: list[str] = []
        try:
            with self.spill_path.open("r", encoding="utf-8") as handle:
                lines = [line for line in handle if line.strip()]
        except OSError:
            return 0
        if not lines:
            return 0

        rows = []
        for line in lines:
            try:
                rows.append(_dict_to_row(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                log.warning("skipping unparseable spilled event")

        try:
            await self.db.executemany(INSERT_SQL, rows)
        except (DatabaseUnavailable, Exception):  # noqa: BLE001
            return 0

        try:
            self.spill_path.unlink()
        except OSError:
            # Could not truncate; the events are in the table, so the retry would
            # duplicate them. Log it rather than raising, and let an operator look.
            log.warning("replayed spill but could not remove %s; duplicates possible", self.spill_path)

        self.replayed += len(rows)
        log.info("replayed %d spilled event(s)", len(rows))
        return len(rows)

    # -- reading -----------------------------------------------------------

    async def recent(
        self,
        *,
        limit: int = 200,
        filters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query the stream, newest first.

        Filter keys are whitelisted, and values are always bound as parameters. A query
        assembled from user input is how a read endpoint becomes a write endpoint.
        """
        clauses: list[str] = []
        args: list[Any] = []

        for key, value in (filters or {}).items():
            if value is None:
                continue
            template = _FILTERS.get(key)
            if template is None:
                raise ValueError(f"unsupported filter {key!r}; allowed: {sorted(_FILTERS)}")
            args.append(value)
            clauses.append(template.replace("$", f"${len(args)}"))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(min(max(limit, 1), 2_000))

        query = (
            "SELECT id, ts, t_dev_ms, experiment_id, event_type, severity, scope, source, "
            "node_id, correlation_id, action, detail "
            f"FROM events{where} ORDER BY ts DESC LIMIT ${len(args)}"
        )
        return await self.db.fetch(query, *args)

    # -- observability -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Surfaced on ``/healthz``. ``spilled`` climbing means the database is behind."""
        return {
            "buffered": len(self._buffer),
            "written": self.written,
            "spilled": self.spilled,
            "replayed": self.replayed,
            "rejected": self.rejected,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# Row conversion
# ---------------------------------------------------------------------------

#: ``bigint`` bounds. A firmware clock that never synced can report a wrapped 64-bit time,
#: and asyncpg rejects the *row* — which, in a batch, means the whole batch.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def sane_t_dev_ms(value: Any) -> int | None:
    """
    A device timestamp the ``bigint`` column can hold, or ``None``.

    ``t_dev_ms`` is device time, so it is *not* trusted — the gateway's own ``ts`` is. An
    untrusted value still has to fit the column: a node that never synced can report a
    wrapped unsigned value, and one such row fails the whole batch insert. Dropping the
    timestamp to NULL loses nothing a reader could use; dropping the *event* loses the
    record, which is the one thing this stream must not do.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if _INT64_MIN <= number <= _INT64_MAX else None


def sane_node_id(value: Any) -> str | None:
    """
    A ``node_id`` the ``uuid`` column can accept, or ``None``.

    Same reasoning as :func:`sane_t_dev_ms`: a malformed id from firmware must not take the
    surrounding events down with it.
    """
    if not isinstance(value, str):
        return None
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    return value


def _as_row(event: Event) -> tuple[Any, ...]:
    return (
        datetime.fromtimestamp(event.ts, tz=timezone.utc),
        sane_t_dev_ms(event.t_dev_ms),
        event.experiment_id,
        event.event_type.value,
        event.severity.value,
        event.scope.value,
        event.source.value,
        sane_node_id(event.node_id),
        event.correlation_id,
        event.action,
        json.dumps(dict(event.detail), default=str),
    )


def _as_dict(event: Event) -> dict[str, Any]:
    return {
        "ts": event.ts,
        "t_dev_ms": event.t_dev_ms,
        "experiment_id": event.experiment_id,
        "event_type": event.event_type.value,
        "severity": event.severity.value,
        "scope": event.scope.value,
        "source": event.source.value,
        "node_id": event.node_id,
        "correlation_id": event.correlation_id,
        "action": event.action,
        "detail": dict(event.detail),
    }


def _dict_to_row(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    """Rebuild an insert tuple from a spilled JSON line."""
    return (
        datetime.fromtimestamp(float(payload["ts"]), tz=timezone.utc),
        sane_t_dev_ms(payload.get("t_dev_ms")),
        payload.get("experiment_id"),
        str(payload["event_type"]),
        str(payload["severity"]),
        str(payload["scope"]),
        str(payload["source"]),
        sane_node_id(payload.get("node_id")),
        payload.get("correlation_id"),
        payload.get("action"),
        json.dumps(dict(payload.get("detail") or {}), default=str),
    )


__all__ = [
    "EventStream",
    "INSERT_SQL",
    "_as_dict",
    "_as_row",
    "_dict_to_row",
]
