"""
Command outbox tests.

Most of what is asserted here is about *ordering* and *exclusions*, because those are the
two places the previous system went wrong:

* **Ordering** — the row must be written before the publish. A command that exists only
  as a publish call vanishes if the broker restarts, and the operator has no way to know.
* **Exclusions** — a setpoint supersedes earlier setpoints; an emergency stop supersedes
  nothing, is superseded by nothing, and is published even when the database is down.
  Blocking a stop command on a database write would be the wrong trade by a wide margin.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aqua_gateway.commands.outbox import (
    MAX_DEADLINE_S,
    MIN_DEADLINE_S,
    SAFETY_ACTIONS,
    SUPERSEDING_ACTIONS,
    CommandError,
    CommandOutbox,
    CommandStatus,
)
from aqua_gateway.db.engine import DatabaseUnavailable

FIXED_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


_UNSET = object()


class FakeConnection:
    """Records every statement so tests can assert on ordering and arguments."""

    def __init__(self, owner: "FakeDB") -> None:
        self.owner = owner

    async def fetch(self, query: str, *args):
        # Used inside a transaction by supersession and by the expiry sweep. One settable
        # result is enough because no single call path uses it twice.
        self.owner.journal.append(("sql:fetch", args))
        if self.owner.fail:
            raise DatabaseUnavailable("simulated outage")
        return self.owner.fetch_result

    async def fetchrow(self, query: str, *args):
        self.owner.journal.append(("sql:fetchrow", args))
        if self.owner.fail:
            raise DatabaseUnavailable("simulated outage")
        if self.owner.fetchrow_result is not _UNSET:
            return self.owner.fetchrow_result
        # Mirror the real `INSERT ... RETURNING`: echo the deadline that was passed in,
        # so the published payload carries a meaningful value rather than a fixed one.
        deadline = args[7] if len(args) > 7 else self.owner.default_deadline
        return {"issued_at": FIXED_NOW, "deadline_at": deadline}

    async def execute(self, query: str, *args):
        self.owner.journal.append(("sql:execute", args))
        if self.owner.fail:
            raise DatabaseUnavailable("simulated outage")
        return "UPDATE 1"


class FakeTransaction:
    def __init__(self, owner: "FakeDB") -> None:
        self.owner = owner

    async def __aenter__(self):
        self.owner.journal.append(("tx:begin", ()))
        return self.owner.connection

    async def __aexit__(self, *exc):
        self.owner.journal.append(("tx:end", ()))
        return False


class FakeDB:
    def __init__(self, *, fail: bool = False, available: bool = True) -> None:
        self.fail = fail
        self.available = available
        self.journal: list[tuple[str, tuple]] = []
        self.connection = FakeConnection(self)

        self.fetch_result: list[dict] = []
        """Returned by ``fetch`` inside a transaction (supersession, expiry)."""
        self.fetchrow_result: object = _UNSET
        """When set, returned by ``fetchrow``. Use ``None`` to simulate 'no such row'."""
        self.query_rows: list[dict] = []
        """Returned by the pool-level ``fetch`` (used by ``recent``)."""
        self.fetchval_result: object = 1
        """Returned by ``fetchval``. ``None`` simulates 'no such row'."""
        self.default_deadline = FIXED_NOW

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args):
        self.journal.append(("sql:execute", args))
        if self.fail:
            raise DatabaseUnavailable("simulated outage")
        return "UPDATE 1"

    async def fetch(self, query: str, *args):
        self.journal.append(("sql:query", args))
        return self.query_rows

    async def fetchval(self, query: str, *args):
        self.journal.append(("sql:fetchval", args))
        if self.fail:
            raise DatabaseUnavailable("simulated outage")
        return self.fetchval_result


class Recorder:
    """Publish spy, sharing the journal so ordering is directly assertable."""

    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.messages: list[tuple[str, dict]] = []
        self.raise_on_publish: Exception | None = None

    async def __call__(self, role: str, message) -> None:
        self.journal.append(("publish", (role, dict(message))))
        if self.raise_on_publish:
            raise self.raise_on_publish
        self.messages.append((role, dict(message)))


@pytest.fixture
def db():
    return FakeDB()


@pytest.fixture
def outbox(db):
    instance = CommandOutbox(db)
    instance.bind_publisher(Recorder(db.journal))
    return instance


def kinds(journal) -> list[str]:
    return [entry[0] for entry in journal]


# ===========================================================================
# Naming an experiment that does not exist
# ===========================================================================


class TestUnknownExperimentReference:
    """
    An unknown experiment id must be a clear rejection, not a 500.

    ``control_commands.experiment_id`` is a foreign key. Before this was checked the insert
    raised ``ForeignKeyViolationError`` -- which is not a ``CommandError``, so it escaped the
    handler in ``/api/commands`` and surfaced as a bare ``500 Internal Server Error``. The
    operator's actual mistake was naming an experiment that had not been created; the answer
    they got pointed at the server instead. This is the first half of the start-experiment
    flow, so it is worth being exact about.
    """

    async def test_unknown_experiment_is_rejected(self, outbox, db):
        db.fetchval_result = None
        with pytest.raises(CommandError) as caught:
            await outbox.issue(
                role="od_sensor",
                action="start_experiment",
                experiment_id="EXP-2026-10-09-99",
            )
        assert caught.value.code == "unknown_experiment"
        # Nothing was published and no row was written: a rejected command must not half-exist.
        assert "publish" not in kinds(db.journal)
        assert "sql:fetchrow" not in kinds(db.journal)

    async def test_known_experiment_is_accepted(self, outbox, db):
        db.fetchval_result = 1
        record = await outbox.issue(
            role="od_sensor",
            action="start_experiment",
            experiment_id="EXP-2026-10-09-01",
        )
        assert record.experiment_id == "EXP-2026-10-09-01"

    async def test_start_experiment_without_an_id_is_refused(self, outbox, db):
        """
        A run with no id is refused rather than started unattributable.

        Omitting the id used to pass the lookup above, because that only runs when an id is
        present. The command was then queued and the node started a run whose samples go out
        with ``exp: null`` and join monitor data under no experiment -- the grouping confusion
        the firmware's old ``exp_default`` sentinel caused, only quieter. The firmware refuses
        the same command for the same reason, since an HTTP or BLE client reaches the node
        without passing through this outbox.
        """
        with pytest.raises(CommandError) as caught:
            await outbox.issue(role="od_sensor", action="start_experiment")
        assert caught.value.code == "missing_experiment_id"
        # Refused before anything happened, exactly like the unknown-reference case.
        assert "publish" not in kinds(db.journal)
        assert "sql:fetchrow" not in kinds(db.journal)

    async def test_no_experiment_skips_the_lookup(self, outbox, db):
        """A monitor or idle command legitimately carries no experiment, so it must not query."""
        db.fetchval_result = None
        await outbox.issue(role="od_sensor", action="start_monitor")
        assert "sql:fetchval" not in kinds(db.journal)

    async def test_check_is_skipped_when_the_database_is_down(self, outbox, db):
        """The check must not become a new way for a command to be blocked."""
        db.available = False
        await outbox.issue(role="od_sensor", action="start_monitor")
        assert "sql:fetchval" not in kinds(db.journal)

    async def test_safety_action_still_publishes_when_database_is_down(self, outbox, db):
        """An emergency stop keeps its fast path: no experiment lookup, no insert."""
        db.available = False
        record = await outbox.issue(role="pump", action="emergency_stop")
        assert "publish" in kinds(db.journal)
        assert record.status is CommandStatus.PUBLISHED


# ===========================================================================
# Ordering of insert vs supersede
# ===========================================================================


class TestSupersessionOrder:
    """
    The insert must run before the update that names its id.

    ``superseded_by`` is a foreign key back to ``control_commands``, so an UPDATE executed
    first points at a row that does not exist yet and Postgres raises
    ``ForeignKeyViolationError``. That is not a ``CommandError``, so it escaped the handler in
    ``/api/commands`` and surfaced as a bare ``500 Internal Server Error``.

    The condition is narrow, which is what made it look like flakiness rather than a bug: it
    only fires when an earlier command for the same role is *still pending* -- i.e. when the
    operator changes their mind before the node has acknowledged. Found by issuing a dose a few
    seconds after a `set_state` on a real pump node.
    """

    async def test_insert_happens_before_the_supersede_update(self, outbox, db):
        db.fetch_result = [{"id": "prior-command"}]
        await outbox.issue(role="pump", action="dose", target={"target_ml": 5.0})

        order = kinds(db.journal)
        assert "sql:fetchrow" in order, "the new command must be inserted"
        assert "sql:fetch" in order, "prior pending intent must be superseded"
        assert order.index("sql:fetchrow") < order.index("sql:fetch"), (
            "insert must precede the update that references its id"
        )

    async def test_superseded_ids_are_still_reported(self, outbox, db):
        """Reordering must not lose the supersession record the UI reads."""
        db.query_rows = [{"id": "prior-command"}]
        db.fetch_result = [{"id": "prior-command"}]
        record = await outbox.issue(role="pump", action="dose", target={"target_ml": 5.0})
        assert record.superseded == ["prior-command"]

    async def test_non_superseding_action_skips_the_update(self, outbox, db):
        """An emergency stop retires nothing, so it must not run the supersede query."""
        await outbox.issue(role="pump", action="emergency_stop")
        assert "sql:fetch" not in kinds(db.journal)


# ===========================================================================
# Ordering
# ===========================================================================


class TestOrdering:
    async def test_persist_happens_before_publish(self, outbox, db):
        """
        A command that exists only as a publish call disappears if the broker restarts,
        and the operator has no way to find out.
        """
        await outbox.issue(role="aerator", action="set_speed", target={"drv_rpm": 420})

        order = kinds(db.journal)
        assert "sql:fetchrow" in order, "the row must be written"
        assert "publish" in order
        assert order.index("sql:fetchrow") < order.index("publish")

    async def test_supersede_and_insert_share_a_transaction(self, outbox, db):
        """
        Atomicity matters because the window between the two statements is exactly when a
        node polling at 1 Hz would pick up the command the operator has just replaced.

        The order within the transaction is ``insert`` then ``supersede``, which is the
        reverse of what this test originally asserted. That assertion was wrong: it recorded
        the sequence as written, and the sequence as written could not work, because
        ``superseded_by`` is a foreign key to the row the insert has not created yet. Pinning
        the bug into a test is how it survives a refactor, so the assertion now states the
        only order that can succeed.
        """
        await outbox.issue(role="aerator", action="set_speed", target={"drv_rpm": 420})

        order = kinds(db.journal)
        begin = order.index("tx:begin")
        insert = order.index("sql:fetchrow")
        supersede = order.index("sql:fetch")
        end = order.index("tx:end")

        # Both statements stay inside the one transaction -- that is the invariant that
        # matters. Only their relative order changed.
        assert begin < insert < supersede < end

    async def test_marked_published_only_after_publishing(self, outbox, db):
        await outbox.issue(role="aerator", action="set_speed", target={})

        order = kinds(db.journal)
        assert order.index("publish") < order.index("sql:execute")


# ===========================================================================
# Supersession
# ===========================================================================


class TestSupersession:
    async def test_setpoint_supersedes_pending_commands(self, outbox, db):
        db.fetch_result = [{"id": "old-1"}, {"id": "old-2"}]

        record = await outbox.issue(role="aerator", action="set_speed", target={"drv_rpm": 420})

        assert record.superseded == ["old-1", "old-2"]
        assert outbox.stats()["superseded"] == 2

        supersede_args = next(args for kind, args in db.journal if kind == "sql:fetch")
        assert supersede_args[1] == "aerator", "supersedes only this role"
        assert "set_speed" in supersede_args[2], "and only superseding actions"

    async def test_safety_action_supersedes_nothing(self, outbox, db):
        """
        An emergency stop must not retire a pending setpoint. It is not a replacement for
        one — it is a different instruction, and the setpoint is still the operator's
        intent once the emergency is over.
        """
        await outbox.issue(role="aerator", action="emergency_stop")

        assert "sql:fetch" not in kinds(db.journal), "no supersession query should run"

    async def test_non_superseding_action_skips_the_update(self, outbox, db):
        """A reboot has no setpoint semantics, so nothing is retired for it."""
        await outbox.issue(role="aerator", action="reboot")

        assert "sql:fetch" not in kinds(db.journal)

    def test_action_classification(self):
        assert "set_speed" in SUPERSEDING_ACTIONS
        assert "emergency_stop" in SAFETY_ACTIONS
        assert not (SAFETY_ACTIONS & SUPERSEDING_ACTIONS), "the sets must not overlap"


# ===========================================================================
# Safety fast path
# ===========================================================================


class TestSafetyActions:
    async def test_safety_action_publishes_without_a_database(self, db):
        """
        Blocking a stop command on a database write would be the wrong trade by a wide
        margin, so the safety path accepts an unpersisted command.
        """
        db.available = False
        outbox = CommandOutbox(db)
        recorder = Recorder(db.journal)
        outbox.bind_publisher(recorder)

        record = await outbox.issue(role="aerator", action="emergency_stop")

        assert len(recorder.messages) == 1
        assert record.status is CommandStatus.PUBLISHED
        assert "not persisted" in record.error, "the gap must be recorded, not hidden"

    async def test_non_safety_action_refuses_without_a_database(self, db):
        """
        The opposite trade: an operator gets a clear error rather than a command that may
        or may not have reached the node.
        """
        db.fail = True
        outbox = CommandOutbox(db)
        outbox.bind_publisher(Recorder(db.journal))

        with pytest.raises(CommandError, match="database_unavailable"):
            await outbox.issue(role="aerator", action="set_speed")

    async def test_publish_payload_carries_a_deadline(self, outbox, db):
        recorder = Recorder(db.journal)
        outbox.bind_publisher(recorder)

        await outbox.issue(role="aerator", action="set_speed", target={"drv_rpm": 420}, deadline_s=120)

        _, message = recorder.messages[0]
        assert message["v"] == 2
        assert message["action"] == "set_speed"
        assert message["target"] == {"drv_rpm": 420}
        assert message["deadline_ms"] > message["issued_ms"], "a node must be able to refuse it later"


# ===========================================================================
# Validation
# ===========================================================================


class TestValidation:
    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_empty_action_is_rejected(self, outbox, bad):
        with pytest.raises(CommandError, match="empty_action"):
            await outbox.issue(role="aerator", action=bad)

    @pytest.mark.parametrize("deadline", [0, 4, MAX_DEADLINE_S + 1, 86_400])
    async def test_deadline_bounds(self, outbox, deadline):
        """
        The upper bound exists because a command that never expires is a command that
        executes when a node wakes up days later. The lower bound keeps a command from
        being born already dead.
        """
        with pytest.raises(CommandError, match="bad_deadline"):
            await outbox.issue(role="aerator", action="set_speed", deadline_s=deadline)

    async def test_bounds_are_inclusive(self, outbox, db):
        for deadline in (MIN_DEADLINE_S, MAX_DEADLINE_S):
            await outbox.issue(role="aerator", action="set_speed", deadline_s=deadline)


# ===========================================================================
# Acknowledgements
# ===========================================================================


class TestAcknowledgement:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ("applied", CommandStatus.ACKNOWLEDGED),
            ("rejected", CommandStatus.REJECTED),
            ("failed", CommandStatus.FAILED),
            ("expired", CommandStatus.EXPIRED),
        ],
    )
    async def test_result_maps_to_status(self, outbox, db, result, expected):
        db.fetchrow_result = {"role": "aerator", "action": "set_speed"}

        assert await outbox.acknowledge(command_id="cmd-1", result=result) is True

        args = next(args for kind, args in db.journal if kind == "sql:fetchrow")
        assert args[1] == expected.value

    async def test_unknown_result_is_not_recorded(self, outbox, db):
        assert await outbox.acknowledge(command_id="cmd-1", result="banana") is False

    async def test_ack_for_an_unknown_command_is_not_an_error(self, outbox, db):
        """
        Normal, not exceptional: a command that already expired locally, or one issued
        while the database was down, has no row to update.
        """
        db.fetchrow_result = None  # the UPDATE ... RETURNING matched nothing

        assert await outbox.acknowledge(command_id="gone", result="applied") is False

    async def test_ack_tolerates_a_database_outage(self, outbox, db):
        db.available = False

        assert await outbox.acknowledge(command_id="cmd-1", result="applied") is False


# ===========================================================================
# Expiry
# ===========================================================================


class TestExpiry:
    async def test_expires_overdue_commands(self, db):
        db.fetch_result = [
            {
                "id": "stale-1",
                "role": "aerator",
                "action": "set_speed",
                "experiment_id": "EXP-1",
                "deadline_at": None,
            }
        ]
        outbox = CommandOutbox(db)

        expired = await outbox.expire_stale()

        assert expired == ["stale-1"]
        assert outbox.stats()["expired"] == 1

    async def test_expiry_is_skipped_without_a_database(self, db):
        db.available = False
        outbox = CommandOutbox(db)

        assert await outbox.expire_stale() == []

    async def test_nothing_to_expire(self, outbox, db):
        db.fetch_result = []

        assert await outbox.expire_stale() == []


# ===========================================================================
# Delivery failure
# ===========================================================================


class TestDeliveryFailure:
    async def test_publish_failure_marks_the_row_failed(self, outbox, db):
        recorder = Recorder(db.journal)
        recorder.raise_on_publish = OSError("broker unreachable")
        outbox.bind_publisher(recorder)

        record = await outbox.issue(role="aerator", action="set_speed")

        # The command was accepted and persisted; only delivery failed. Reporting it as a
        # raise would lose the record the operator needs to see.
        assert record.id
        assert outbox.stats()["last_error"] is not None
        assert any("failed" in str(args) for kind, args in db.journal if kind == "sql:execute")

    async def test_unbound_publisher_is_reported(self, db):
        outbox = CommandOutbox(db)  # no publisher bound

        await outbox.issue(role="aerator", action="set_speed")

        assert outbox.stats()["last_error"] == "publisher not bound"
        assert outbox.stats()["publisher_bound"] is False
