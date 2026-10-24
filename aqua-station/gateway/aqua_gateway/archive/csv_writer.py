"""
Buffered CSV writer for the telemetry archive.

The write pattern is the whole point. A database writes a 8 KB page and a WAL
record per transaction; an append-only CSV writes one sequential block. On an SD
card that difference is the entire wear story:

    measured idle (rsyslog + journald, nothing running)  ~38,000 write ops/day
    this writer, four active roles, 300 s cap             ~1,300 write ops/day

Design choices, each deliberate:

* **Every release is ``fsync``ed.** A release moves rows out of this process and into the
  kernel's page cache; a reader sees them at once, but the card does not have them until the
  kernel writes them back -- up to ``vm.dirty_expire_centisecs`` (30 s on this station).
  Left unsynced that window sits *outside* the age cap, so the cap would bound the wrong
  thing. One barrier per release closes the page cache and the card's own write cache
  together, which is what makes the durability guarantee a single number. Measured cost:
  **7.75 ms per 64 KB block against 0.14 ms unsynced**.

  A *graceful* stop loses nothing in either stage: ``close_all()`` flushes and closes every
  open file. Only a power cut does, and what it costs is at most one release interval.
* **A buffer is released on size OR age**, whichever comes first. Size alone made the
  exposure a function of the sampling rate instead of of time: 64 KB is a fixed *number of
  bytes*, so at the 5 Hz experiment cadence it is minutes, while the od_sensor's real
  0.2 Hz cadence stretches the same 64 KB past an hour. The question during a power cut is
  "how much time?", and ``max_buffer_age_s`` is what makes the answer independent of how
  fast the node happens to sample. Measured widths per role are in
  ``test_archive_block_behaviour.py``.

  Because a release is now one write *and* one commit, the cap sets the floor on both, and
  that is why it is 300 s rather than something tighter: at 60 s an idle-cadence role
  released 1440 times a day, five times the write count of 300 s, in exchange for an
  exposure the sync already makes predictable.
* **One ``write()`` per buffer fill**, not one per sample.
* **Registry-driven column order**, so a schema change is a ``roles.yaml`` edit.
* **Arrival order preserved.** Rows are appended as they arrive, which can differ
  slightly from ``t_dev_ms`` order after a replay. The export sorts; the raw file
  stays an honest record of the wire.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..schema.registry import RoleSchema
from .finalise import finalise_file

if TYPE_CHECKING:  # pragma: no cover - import cycle guard only
    from .manifest import Manifest


@dataclass(slots=True)
class WriterStats:
    rows_written: int = 0
    bytes_written: int = 0
    block_writes: int = 0
    flushes: int = 0
    rotations: int = 0

    @property
    def bytes_per_block(self) -> float:
        return self.bytes_written / self.block_writes if self.block_writes else 0.0


@dataclass
class _OpenFile:
    """A single role's current day file."""

    path: Path
    day: date
    buffer: io.StringIO
    rows: int = 0
    bytes_on_disk: int = 0
    digest: "hashlib._Hash" = field(default_factory=hashlib.sha256)
    #: Monotonic time the buffer became non-empty, or ``None`` when it is empty. The
    #: size triggers a release on its own; this is what lets age trigger one too.
    buffer_since: float | None = None

    def age_hint(self) -> int:
        return self.buffer.tell()


class CsvArchiveWriter:
    """
    Appends rows to per-role, per-day CSV files.

    One open file per role. Buffers in memory and issues a single ``write()`` when
    the buffer crosses ``block_bytes``, which is what keeps block-write counts in
    the tens per day rather than the tens of thousands.
    """

    def __init__(
        self,
        root: Path,
        *,
        block_bytes: int = 64 * 1024,
        max_buffer_age_s: float = 300.0,
        fsync_on_rotate: bool = True,
        timezone: str = "America/Lima",
        manifest: "Manifest | None" = None,
    ) -> None:
        self.root = root
        self.block_bytes = block_bytes
        self.max_buffer_age_s = max_buffer_age_s
        self.fsync_on_rotate = fsync_on_rotate
        self.timezone = timezone
        # Optional so the writer stays usable (and testable) without a manifest. When
        # present, the writer registers and closes records — which is what makes a
        # file eligible for compression and flushing at all. Without it, rotation has
        # nothing to work from and files accumulate unmanaged.
        self.manifest = manifest
        self.stats = WriterStats()
        self._open: dict[str, _OpenFile] = {}
        self._lock = asyncio.Lock()

    # -- paths -------------------------------------------------------------

    def path_for(self, role: str, day: date) -> Path:
        return self.root / role / f"{day.isoformat()}.csv"

    @property
    def open_paths(self) -> tuple[Path, ...]:
        """Passed to the retention policy so it can never delete a live file."""
        return tuple(f.path for f in self._open.values())

    async def buffered_rows(self, role: str) -> list[str]:
        """
        CSV rows for ``role`` written but not yet on disk, oldest first.

        The writer keeps up to ``block_bytes`` (64 KB) in memory before issuing a single
        write, and releases a part-full buffer once it is ``max_buffer_age_s`` old. A
        reader that only opened files would otherwise appear to lose the most recent
        stretch -- and missing data at the end of a series is indistinguishable, to an
        operator, from a node that has stopped sending.

        Taken under the writer's lock because a flush does ``getvalue()`` and then
        ``truncate(0)``; reading unsynchronised can observe a half-emptied buffer and
        return rows that are neither fully in memory nor fully on disk.
        """
        async with self._lock:
            handle = self._open.get(role)
            if handle is None:
                return []
            text = handle.buffer.getvalue()
        return [line for line in text.splitlines() if line] if text else []

    # -- writing -----------------------------------------------------------

    async def append_batch(
        self,
        schema: RoleSchema,
        rows: Sequence[Mapping[str, object]],
        *,
        day: date,
        seq: int,
        t_src: str,
    ) -> None:
        """
        Append one batch of telemetry rows.

        ``day`` comes from the caller so all rows are filed under the *device's*
        day, not the wall-clock day at the moment of writing a replay.
        """
        if not rows:
            return

        async with self._lock:
            handle = self._open.get(schema.role) or await self._open_role(schema, day)
            if handle.day != day:
                await self._rotate(schema.role, handle)
                handle = await self._open_role(schema, day)

            writer = csv.writer(handle.buffer, lineterminator="\n")
            for row in rows:
                # Iterate `schema.fields`, NOT `schema.field_names`. The latter is a frozenset,
                # and a frozenset of strings has no order — worse, str hashing is randomised per
                # process, so the values would line up with a different header on every restart.
                # The header comes from `csv_header`, which is ordered, so using the set here
                # silently scrambles every column relative to its name.
                record = [
                    row.get("t_dev_ms", ""),
                    t_src,
                    seq,
                    *(self._format(row.get(field.name)) for field in schema.fields),
                ]
                writer.writerow(record)
                handle.rows += 1
                self.stats.rows_written += 1

            # Size OR age, whichever comes first. Size alone ties the exposure to the
            # sampling rate: 64 KB is a fixed *number of bytes*, so a 5 Hz node releases it
            # in minutes while the od_sensor's 0.2 Hz cadence holds the same block for over
            # an hour. The question during a power cut is "how much *time*?", so the age cap
            # is what makes the answer independent of how fast the node happens to sample.
            #
            # Synced, because a release is where the guarantee has to be true. The write
            # alone only reaches the page cache, and leaving it there would put a second
            # window *outside* the cap. Syncing the byte trigger as well keeps the promise
            # structural, rather than resting on the fill rate of one role happening to beat
            # the cap.
            now = time.monotonic()
            if handle.buffer_since is None:
                handle.buffer_since = now
            if handle.buffer.tell() >= self.block_bytes or now - handle.buffer_since >= self.max_buffer_age_s:
                await self._flush_buffer(handle, sync=True)

    @staticmethod
    def _format(value: object) -> object:
        """Booleans become 0/1 so the archive stays trivially parseable."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return 1 if value else 0
        return value

    async def _open_role(self, schema: RoleSchema, day: date) -> _OpenFile:
        path = self.path_for(schema.role, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()

        buffer = io.StringIO()
        handle = _OpenFile(path=path, day=day, buffer=buffer)
        if fresh:
            csv.writer(buffer, lineterminator="\n").writerow(schema.csv_header)
        else:
            # Resume after a restart: pick up the byte count so `bytes` stays honest.
            handle.bytes_on_disk = path.stat().st_size

        self._open[schema.role] = handle

        # Register the record so the rest of the pipeline can see it. Idempotent
        # (ON CONFLICT DO NOTHING), which is what makes a restart mid-day safe: the
        # gateway reopens the same path and adopts the existing row.
        if self.manifest is not None:
            self.manifest.open_file(path, schema.role, day.isoformat())

        return handle

    async def _flush_buffer(self, handle: _OpenFile, *, sync: bool = False) -> None:
        """One block write. This is the only place data touches the SD card."""
        payload = handle.buffer.getvalue()
        if not payload:
            handle.buffer_since = None
            return

        handle.buffer.seek(0)
        handle.buffer.truncate(0)

        data = payload.encode("utf-8")
        handle.digest.update(data)

        with handle.path.open("ab") as sink:
            sink.write(data)
            if sync:
                sink.flush()
                os.fsync(sink.fileno())
                self.stats.flushes += 1

        handle.bytes_on_disk += len(data)
        self.stats.bytes_written += len(data)
        self.stats.block_writes += 1
        # Empty again, so the age clock restarts with the next row rather than measuring
        # from a moment that is already on disk.
        handle.buffer_since = None

    async def _flush_and_release(self, role: str, handle: _OpenFile) -> None:
        """
        Push the buffer to the SD card and forget the handle. No manifest write.

        Split out of ``_rotate`` because there are two different reasons to let go of a
        handle, and only one of them means the day is over. Flushing is the part that
        must never be skipped: whatever is still in the buffer is only in RAM.

        ``fsync_on_rotate`` still gates the explicit sync below, but it is now
        belt-and-braces: every release path syncs before reaching here, so the flag no
        longer decides whether this data is durable. It is kept because ``gateway.env``
        sets it, and settings forbid unknown keys -- dropping the field would fail
        validation at boot on a host that still has the line.
        """
        if self.fsync_on_rotate:
            await self._flush_buffer(handle, sync=True)
        else:
            await self._flush_buffer(handle)
        self._open.pop(role, None)

    async def _rotate(self, role: str, handle: _OpenFile) -> None:
        """Close the current day's file. gzip happens separately, off the hot path."""
        await self._flush_and_release(role, handle)
        self.stats.rotations += 1
        await self.on_rotated(handle)

    async def flush_aged(self) -> int:
        """
        Release every buffer that has been held longer than ``max_buffer_age_s``.

        Both triggers in the write path are only evaluated when a row arrives, so a role
        that goes quiet holding a part-full buffer would keep those rows in RAM
        indefinitely -- the buffer would never fill and nothing else would look at it.
        This sweep is the other half of the age cap; without it the cap only applies to
        roles that are still sending.

        Safe to call when nothing has aged: it is a no-op that returns 0.

        :returns: how many buffers were released.
        """
        async with self._lock:
            now = time.monotonic()
            aged = [
                handle
                for handle in self._open.values()
                if handle.buffer_since is not None and now - handle.buffer_since >= self.max_buffer_age_s
            ]
            for handle in aged:
                await self._flush_buffer(handle, sync=True)
            return len(aged)

    async def rotate_if_needed(self, *, today: date) -> list[_OpenFile]:
        """Called by a midninght timer in America/Lima. Returns the files it closed."""
        closed: list[_OpenFile] = []
        async with self._lock:
            for role, handle in list(self._open.items()):
                if handle.day != today:
                    await self._rotate(role, handle)
                    closed.append(handle)
        return closed

    async def close_all(self, *, today: date | None = None) -> None:
        """
        Flush and close everything. Called on graceful shutdown.

        Today's file is flushed and released but deliberately **not** finalised, for
        the reason ``finalise_stale`` gives: it is not finished, and the next start
        resumes appending to the same path. Setting ``closed_at`` here would hand a
        live file to the rotation process, which is entitled to gzip it and unlink
        the source -- while this process's successor writes the next buffered block
        into an inode with no name left pointing at it. That block is then gone with
        nothing having reported a failure.

        A handle for an earlier day (the midnight timer did not get to it) is still a
        finished file, so that one does go through the normal rotation.
        """
        if today is None:
            today = datetime.now(tz=ZoneInfo(self.timezone)).date()
        async with self._lock:
            for role, handle in list(self._open.items()):
                if handle.day == today:
                    await self._flush_and_release(role, handle)
                else:
                    await self._rotate(role, handle)

    async def on_rotated(self, handle: _OpenFile) -> None:
        """
        Close the manifest record for a file the writer has just finished.

        This is the handoff that makes the two-process coordination rule work: after
        this returns, the record has ``closed_at`` set and is therefore visible to
        the rotation process, which is contractually barred from touching records
        with ``closed_at IS NULL``.

        Runs in a worker thread because hashing a multi-megabyte file is blocking
        I/O, and doing it on the event loop would stall MQTT ingest for the duration.
        It happens once per role per day, so the thread hop costs nothing.
        """
        if self.manifest is None:
            return
        await asyncio.to_thread(finalise_file, self.manifest, handle.path)

    # -- observability -----------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Exposed on ``/metrics``. Block-write count is the SD-health canary."""
        return {
            "rows_written": self.stats.rows_written,
            "bytes_written": self.stats.bytes_written,
            "block_writes": self.stats.block_writes,
            "bytes_per_block": round(self.stats.bytes_per_block, 1),
            "flushes": self.stats.flushes,
            "rotations": self.stats.rotations,
            "open_files": {role: str(h.path) for role, h in self._open.items()},
        }


def rows_from_samples(
    schema: RoleSchema,
    samples: Iterable[Mapping[str, object]],
    *,
    t_dev_ms: int,
    default_ts: bool = True,
) -> list[dict[str, object]]:
    """
    Project wire samples onto registry columns.

    ``t_dev_ms`` is taken per sample when present (a batch may span time) and
    otherwise from the envelope, so a batch with a single envelope timestamp
    still files every row under the node's own clock rather than the gateway's.
    """
    projected: list[dict[str, object]] = []
    for sample in samples:
        row = schema.project(sample)
        row["t_dev_ms"] = int(sample.get("t_dev_ms", t_dev_ms)) if default_ts else t_dev_ms
        projected.append(row)
    return projected
