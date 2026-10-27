"""
Operator command outbox.

Delivers an operator's intent to a node, and keeps a durable record of it. Three
properties matter, each of which exists because the previous system got it wrong:

**Persist before publish.** The row is written first and marked ``published`` only
after the broker accepted it. A command that lives only in a publish call disappears
if the broker restarts, and the operator has no way to know.

**Atomic supersession.** Issuing a setpoint supersedes any earlier *pending* setpoint
for the same role, in the same transaction. The previous system discovered this the
hard way: a ``set_state`` queued by a recipe, still undelivered, was served to a node
*after* the operator's ``MANUAL_OVERRIDE`` — and because nodes apply what they last
received, the manual override was silently undone and the PID re-armed itself. Here
the superseded command is explicitly retired and the link recorded in
``superseded_by``, so the resolution is visible rather than inferred from timestamps.

**Safety actions are never queued behind anything.** ``emergency_stop`` does not
supersede, is not superseded, and is published even when the database is unreachable.
Blocking a stop command on a database write would be the wrong trade by a wide margin.

Everything else follows from those.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..db.engine import Database, DatabaseUnavailable
from ..events.model import Event, EventType, Source
from ..events.stream import EventStream

log = logging.getLogger("aqua.commands")

Publisher = Callable[[str, Mapping[str, Any]], Awaitable[None]]


class CommandStatus(str, Enum):
    QUEUED = "queued"
    PUBLISHED = "published"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"

    @property
    def is_pending(self) -> bool:
        return self in (CommandStatus.QUEUED, CommandStatus.PUBLISHED)

    @property
    def is_terminal(self) -> bool:
        return not self.is_pending


#: Actions that represent the *latest intent* for a role. Issuing one retires any
#: earlier pending one, because only the newest setpoint is meaningful.
SUPERSEDING_ACTIONS: frozenset[str] = frozenset(
    {
        "set_speed",
        "set_state",
        "set_mode",
        "set_config",
        "start",
        "stop",
        "start_mixer",
        "stop_mixer",
        "dose",
        "prime",
        "drain",
        "suckback",
        "retract",
        "force_off",
        "set_meniscus",
        "set_recipe",
    }
)

#: Actions that must never be superseded, and never supersede. Enqueuing an emergency
#: stop behind a setpoint, or letting a later setpoint cancel one, would both be wrong.
SAFETY_ACTIONS: frozenset[str] = frozenset({"emergency_stop", "clear_estop", "failsafe_clear"})

#: Actions that are meaningless without an experiment id. A run started without one cannot be
#: attributed afterwards: its samples go out with `exp: null` and land in the archive's "no
#: experiment" group, sharing a bucket with monitor data. That is the grouping confusion the
#: firmware's old `exp_default` sentinel caused, only quieter, so the id is required here rather
#: than treated as an optional label. The firmware refuses the same command for the same reason,
#: because an HTTP or BLE client reaches the node without passing through this outbox.
EXPERIMENT_BOUND_ACTIONS: frozenset[str] = frozenset({"start_experiment"})

MAX_DEADLINE_S = 3_600
MIN_DEADLINE_S = 5


class CommandError(ValueError):
    """A command that must not be accepted. Carries a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """One row of the outbox, as returned to callers."""

    id: str
    role: str
    action: str
    target: Mapping[str, Any]
    status: CommandStatus
    experiment_id: str | None
    requested_by: str
    issued_at: datetime | None
    deadline_at: datetime | None
    published_at: datetime | None
    ack_at: datetime | None
    superseded_by: str | None
    error: str | None
    superseded: Sequence[str] = ()
    """Ids retired by this command, populated only on the issuing call."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "action": self.action,
            "target": dict(self.target),
            "status": self.status.value,
            "experiment_id": self.experiment_id,
            "requested_by": self.requested_by,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "ack_at": self.ack_at.isoformat() if self.ack_at else None,
            "superseded_by": self.superseded_by,
            "superseded": list(self.superseded),
            "error": self.error,
        }


_SUPERSEDE_SQL = """
UPDATE control_commands
   SET status = 'superseded', superseded_by = $1
 WHERE role = $2
   AND status IN ('queued', 'published')
   AND action = ANY($3::text[])
   AND id <> $1
RETURNING id
"""

_INSERT_SQL = """
INSERT INTO control_commands
    (id, node_id, role, experiment_id, action, target, status, requested_by, deadline_at)
VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb, 'queued', $7, $8)
RETURNING issued_at, deadline_at
"""

_MARK_PUBLISHED_SQL = """
UPDATE control_commands SET status = 'published', published_at = now()
 WHERE id = $1 AND status = 'queued'
"""

_ACK_SQL = """
UPDATE control_commands
   SET status = $2, ack_at = now(), error = $3
 WHERE id = $1 AND status IN ('queued', 'published')
RETURNING role, action
"""

_EXPIRE_SQL = """
UPDATE control_commands
   SET status = 'expired', error = 'deadline passed before delivery'
 WHERE status IN ('queued', 'published') AND deadline_at < now()
RETURNING id, role, action, experiment_id, deadline_at
"""

_RECENT_SQL = """
SELECT id, role, action, target, status, experiment_id, requested_by,
       issued_at, deadline_at, published_at, ack_at, superseded_by, error
  FROM control_commands
 WHERE ($1::text IS NULL OR role = $1)
 ORDER BY issued_at DESC
 LIMIT $2
"""


def _as_mapping(value: Any) -> dict[str, Any]:
    """
    Coerce a JSONB column into a dict.

    asyncpg returns JSONB as ``str`` unless a codec has been registered on the connection,
    and this pool does not register one. ``dict(value)`` on that string does not raise the
    way it looks like it should -- it happily builds ``{'{"interval_sec": 5}': ...}``-shaped
    nonsense -- so decoding explicitly is the only way to be sure what reaches the client.

    A target that cannot be decoded yields an empty mapping rather than raising: the action
    and the status are what the operator is actually looking at, and losing those because
    one column is malformed would be the worse failure.
    """
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


class CommandOutbox:
    """Durable command queue with MQTT delivery."""

    def __init__(self, db: Database, events: EventStream | None = None) -> None:
        self.db = db
        self.events = events
        # Bound after construction: the outbox publishes through the ingest client, and
        # the ingest client needs the outbox to resolve acknowledgements. A setter
        # breaks that cycle without either object reaching for the other at import time.
        self.publisher: Publisher | None = None
        self.issued = 0
        self.superseded = 0
        self.expired = 0
        self.last_error: str | None = None

    def bind_publisher(self, publisher: Publisher) -> None:
        self.publisher = publisher

    # -- issuing -----------------------------------------------------------

    async def _require_experiment(self, experiment_id: str) -> None:
        """
        Raise :class:`CommandError` unless the experiment exists.

        A command may legitimately carry no experiment -- that is what an idle or monitor
        command does -- so only a *named* experiment has to be real.

        Checked here rather than left to the foreign key on ``control_commands``. The key
        does catch it, but it raises ``ForeignKeyViolationError``, which is not a
        ``CommandError``, so it escaped the handler in ``/api/commands`` and surfaced as a
        bare ``500 Internal Server Error``. An operator who tried to start an experiment
        they had not yet created got a server error instead of being told exactly that.
        """
        if not self.db.available:
            # Let the insert fail with its own clearer error rather than guessing here.
            return
        exists = await self.db.fetchval("SELECT 1 FROM experiments WHERE id = $1", experiment_id)
        if not exists:
            raise CommandError(
                "unknown_experiment",
                f"experiment {experiment_id!r} does not exist; create it first",
            )

    async def issue(
        self,
        *,
        role: str,
        action: str,
        target: Mapping[str, Any] | None = None,
        experiment_id: str | None = None,
        requested_by: str = "operator:web",
        deadline_s: int = 300,
        node_id: str | None = None,
    ) -> CommandRecord:
        """
        Accept a command, persist it, publish it, and return the record.

        :raises CommandError: when the command is malformed or the deadline is absurd.
        """
        action = (action or "").strip()
        if not action:
            raise CommandError("empty_action", "action must be a non-empty string")
        if not MIN_DEADLINE_S <= deadline_s <= MAX_DEADLINE_S:
            raise CommandError(
                "bad_deadline",
                f"deadline_s must be between {MIN_DEADLINE_S} and {MAX_DEADLINE_S}",
            )
        if experiment_id is not None:
            await self._require_experiment(experiment_id)
        elif action in EXPERIMENT_BOUND_ACTIONS:
            # Refused rather than queued with a null experiment. Queueing it would publish a
            # command the node should not carry out, and the operator would learn about the
            # mistake only from a rejected acknowledgement.
            raise CommandError(
                "missing_experiment_id",
                f"{action!r} requires experiment_id: without it the run cannot be attributed, "
                "and its samples would join monitor data under no experiment",
            )

        payload = dict(target or {})
        command_id = str(uuid.uuid4())
        is_safety = action in SAFETY_ACTIONS
        deadline_at = datetime.now(tz=timezone.utc) + timedelta(seconds=deadline_s)

        # -- safety fast path -------------------------------------------------
        # An emergency stop must not be blocked on a database write. Record the
        # intent best-effort and publish regardless; a failed insert is reported,
        # never silently swallowed, and the event stream spills to disk.
        if is_safety and not self.db.available:
            log.warning("publishing safety action without persistence", extra={"action": action, "role": role})
            await self._publish(command_id, role, action, payload, experiment_id, deadline_at, requested_by)
            await self._emit(
                EventType.COMMAND_ISSUED,
                role=role,
                command_id=command_id,
                experiment_id=experiment_id,
                detail={"action": action, "target": payload, "persisted": False, "safety": True},
            )
            return CommandRecord(
                id=command_id,
                role=role,
                action=action,
                target=payload,
                status=CommandStatus.PUBLISHED,
                experiment_id=experiment_id,
                requested_by=requested_by,
                issued_at=None,
                deadline_at=deadline_at,
                published_at=datetime.now(tz=timezone.utc),
                ack_at=None,
                superseded_by=None,
                error="not persisted: database unavailable",
            )

        try:
            issued_at, stored_deadline, superseded = await self._persist(
                command_id=command_id,
                node_id=node_id,
                role=role,
                action=action,
                payload=payload,
                experiment_id=experiment_id,
                requested_by=requested_by,
                deadline_at=deadline_at,
                supersede=action in SUPERSEDING_ACTIONS,
            )
        except DatabaseUnavailable as exc:
            self.last_error = str(exc)
            # Not a safety action, so refusing is correct: the operator gets a clear
            # error instead of a command that may or may not have reached the node.
            raise CommandError("database_unavailable", "cannot durably queue the command") from exc

        self.issued += 1
        if superseded:
            self.superseded += len(superseded)
            log.info(
                "superseded %d pending command(s)",
                len(superseded),
                extra={"role": role, "action": action, "superseded": list(superseded)},
            )
            await self._emit(
                EventType.COMMAND_SUPERSEDED,
                role=role,
                command_id=command_id,
                experiment_id=experiment_id,
                detail={"superseded_ids": list(superseded), "by_action": action},
            )

        await self._publish(command_id, role, action, payload, experiment_id, stored_deadline, requested_by)
        await self._mark_published(command_id)

        await self._emit(
            EventType.COMMAND_ISSUED,
            role=role,
            command_id=command_id,
            experiment_id=experiment_id,
            detail={
                "action": action,
                "target": payload,
                "deadline_at": stored_deadline.isoformat(),
                "superseded": list(superseded),
            },
        )

        return CommandRecord(
            id=command_id,
            role=role,
            action=action,
            target=payload,
            status=CommandStatus.PUBLISHED,
            experiment_id=experiment_id,
            requested_by=requested_by,
            issued_at=issued_at,
            deadline_at=stored_deadline,
            published_at=datetime.now(tz=timezone.utc),
            ack_at=None,
            superseded_by=None,
            error=None,
            superseded=superseded,
        )

    async def _persist(
        self,
        *,
        command_id: str,
        node_id: str | None,
        role: str,
        action: str,
        payload: Mapping[str, Any],
        experiment_id: str | None,
        requested_by: str,
        deadline_at: datetime,
        supersede: bool,
    ) -> tuple[datetime, datetime, list[str]]:
        """
        Insert the new command and retire prior pending intent, in one transaction.

        Atomic because the window between the two statements is exactly when a node
        polling at 1 Hz would pick up the command the operator has just replaced.

        The insert has to come *first*. The opposite order reads more naturally, but
        ``superseded_by`` is a foreign key back to this same table, so the update would
        reference a row the insert has not created yet and Postgres raises
        ``ForeignKeyViolationError`` -- which is not a ``CommandError``, so it escapes the
        handler in ``/api/commands`` as a bare 500. It only fires when an earlier command for
        the same role is *still pending*, which is precisely what an operator does when they
        change their mind a second later, so it presents as an intermittent server error
        rather than as a bug.

        Reordering is safe because ``_SUPERSEDE_SQL`` excludes the new id itself
        (``id <> $1``). Without that guard the new command would match its own predicate --
        it is 'queued' and its action is a superseding one -- and retire itself.
        """
        async with self.db.transaction() as connection:
            row = await connection.fetchrow(
                _INSERT_SQL,
                command_id,
                node_id,
                role,
                experiment_id,
                action,
                json.dumps(dict(payload), default=str),
                requested_by,
                deadline_at,
            )

            superseded: list[str] = []
            if supersede:
                retired = await connection.fetch(
                    _SUPERSEDE_SQL,
                    command_id,
                    role,
                    sorted(SUPERSEDING_ACTIONS),
                )
                superseded = [str(retired_row["id"]) for retired_row in retired]

        return row["issued_at"], row["deadline_at"], superseded

    async def _mark_published(self, command_id: str) -> None:
        try:
            await self.db.execute(_MARK_PUBLISHED_SQL, command_id)
        except DatabaseUnavailable:
            # The command is already on the wire; failing to record that is worth
            # logging but must not surface as a failure to the operator.
            log.warning("could not mark command published", extra={"command_id": command_id})

    async def _publish(
        self,
        command_id: str,
        role: str,
        action: str,
        payload: Mapping[str, Any],
        experiment_id: str | None,
        deadline_at: datetime,
        requested_by: str,
    ) -> None:
        """Publish on ``aqua/cmd/<role>``. Never raises."""
        message = {
            "v": 2,
            "cmd_id": command_id,
            "issued_ms": int(time.time() * 1000),
            "exp": experiment_id,
            "action": action,
            "target": dict(payload),
            "deadline_ms": int(deadline_at.timestamp() * 1000),
            "requested_by": requested_by,
        }
        if self.publisher is None:
            log.error("no publisher bound; command %s was NOT delivered", command_id)
            self.last_error = "publisher not bound"
            return
        try:
            await self.publisher(role, message)
        except Exception as exc:  # noqa: BLE001 - transport surfaces many types
            self.last_error = str(exc)
            log.error("publish failed", extra={"command_id": command_id, "role": role, "error": str(exc)})
            try:
                await self.db.execute(
                    "UPDATE control_commands SET status='failed', error=$2 WHERE id=$1",
                    command_id,
                    f"publish failed: {exc}",
                )
            except DatabaseUnavailable:
                pass

    # -- acknowledgement ---------------------------------------------------

    async def acknowledge(
        self,
        *,
        command_id: str,
        result: str,
        error: str | None = None,
    ) -> bool:
        """
        Record a node's acknowledgement. Returns whether a pending row was updated.

        ``False`` is normal and not an error: a command that already expired locally, or
        one issued while the database was down, has no row to update.
        """
        mapping = {
            "applied": CommandStatus.ACKNOWLEDGED,
            "rejected": CommandStatus.REJECTED,
            "failed": CommandStatus.FAILED,
            "expired": CommandStatus.EXPIRED,
        }
        status = mapping.get(result)
        if status is None:
            log.warning("unknown ack result", extra={"command_id": command_id, "result": result})
            return False

        if not self.db.available:
            return False

        try:
            async with self.db.transaction() as connection:
                row = await connection.fetchrow(_ACK_SQL, command_id, status.value, error)
        except (DatabaseUnavailable, Exception):  # noqa: BLE001
            log.warning("could not record acknowledgement", extra={"command_id": command_id})
            return False

        if row is None:
            log.debug("ack for an unknown or already-settled command", extra={"command_id": command_id})
            return False

        log.info("command %s -> %s", command_id, status.value)
        return True

    # -- expiry ------------------------------------------------------------

    async def expire_stale(self) -> list[str]:
        """
        Retire commands whose deadline has passed.

        This is the mechanism that stops a days-old command executing when a node
        finally wakes from deep sleep. The deadline is set at issue time and the node
        refuses anything past it; this sweep keeps the queue and the event log honest
        about what was never delivered.
        """
        if not self.db.available:
            return []

        try:
            async with self.db.transaction() as connection:
                rows = await connection.fetch(_EXPIRE_SQL)
        except (DatabaseUnavailable, Exception):  # noqa: BLE001
            return []

        expired = [str(row["id"]) for row in rows]
        for row in rows:
            self.expired += 1
            await self._emit(
                EventType.COMMAND_EXPIRED,
                role=row["role"],
                command_id=str(row["id"]),
                experiment_id=row["experiment_id"],
                detail={"action": row["action"], "deadline_at": existing_iso(row["deadline_at"])},
            )
        if expired:
            log.info("expired %d command(s)", len(expired))
        return expired

    # -- reading -----------------------------------------------------------

    async def recent(self, *, limit: int = 100, role: str | None = None) -> list[dict[str, Any]]:
        """
        Recent outbox entries, newest first, in the same shape ``issue()`` returns.

        This used to return the raw database rows, which quietly disagreed with the issue
        path in two ways that are only visible once commands actually exist:

        * ``target`` is a JSONB column, so with no codec registered it arrived as a *string*.
          The dashboard expected an object and rendered a JSON blob wherever a target was
          shown.
        * ``superseded`` is not a column at all -- it is computed when a command is issued,
          recording which earlier commands that one retired -- so the key was simply absent.
          ``command.superseded.length`` in the command console then threw on ``undefined``
          and took the entire page down with it.

        Both were invisible while the outbox was empty, which is the state every fresh
        install starts in, and neither is visible in the API response unless you look for
        it. Going through ``as_dict()`` keeps the two paths in step by construction instead
        of by two people remembering to change both.

        ``superseded`` is empty for historical rows on purpose: the ids a command retired
        were only knowable at the moment it was issued and are not stored. ``superseded_by``
        on the retiring command's own row is the durable half of the same fact.
        """
        rows = await self.db.fetch(_RECENT_SQL, role, min(max(limit, 1), 1_000))
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                CommandRecord(
                    id=str(row["id"]),
                    role=row["role"],
                    action=row["action"],
                    target=_as_mapping(row["target"]),
                    status=CommandStatus(row["status"]),
                    experiment_id=row["experiment_id"],
                    requested_by=row["requested_by"],
                    issued_at=row["issued_at"],
                    deadline_at=row["deadline_at"],
                    published_at=row["published_at"],
                    ack_at=row["ack_at"],
                    superseded_by=row["superseded_by"],
                    error=row["error"],
                ).as_dict()
            )
        return out

    # -- helpers -----------------------------------------------------------

    async def _emit(
        self,
        event_type: EventType,
        *,
        role: str,
        command_id: str,
        experiment_id: str | None,
        detail: Mapping[str, Any],
    ) -> None:
        if self.events is None:
            return
        event = Event.make(
            event_type,
            source=Source.GATEWAY,
            experiment_id=experiment_id,
            correlation_id=command_id,
            action=detail.get("action"),
            detail={"role": role, **detail},
        )
        try:
            await self.events.emit(event)
        except Exception:  # noqa: BLE001
            log.exception("could not emit command event")

    def stats(self) -> dict[str, Any]:
        return {
            "issued": self.issued,
            "superseded": self.superseded,
            "expired": self.expired,
            "last_error": self.last_error,
            "publisher_bound": self.publisher is not None,
        }


def existing_iso(value: Any) -> str | None:
    """Format a value that is already a datetime, or pass through None."""
    return value.isoformat() if isinstance(value, datetime) else None
