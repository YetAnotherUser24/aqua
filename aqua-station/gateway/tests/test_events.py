"""
Event tests: derived rules and the persistence stream.

The derived rules exist to answer "is the pond in trouble?" without burying the log.
At 5 Hz an ungated check would write 300 identical events a minute, so most of what is
asserted here is about *when an event is not emitted*:

* hysteresis — a value hovering on a threshold does not flap;
* transition-only — a sustained condition reports once;
* authority — the aerator's ESP-NOW echo of dissolved oxygen is ignored, because
  alerting on it would double every DO event and could alert on water the sensor is
  not reporting at all.
"""

from __future__ import annotations

import json
import random

import pytest

from aqua_gateway.config import BackupSettings, Settings
from aqua_gateway.db.engine import DatabaseUnavailable
from aqua_gateway.events.derived import DerivedRules, Level, Thresholds
from aqua_gateway.events.model import Event, EventType, Severity, Source
from aqua_gateway.events.stream import EventStream
from aqua_gateway.schema.registry import load_registry

MINUTE_MS = 1_791_547_200_000  # 2026-10-09T12:00:00Z


class FakeDB:
    """
    Batched writes land whole, unless a row is marked poison — which must fail the batch
    exactly as asyncpg does, because surviving that is the behaviour under test.
    """

    def __init__(self, *, fail: bool = False, poison=None) -> None:
        self.rows: list[tuple] = []
        self.fail = fail
        self.poison = poison

    def _rejected(self, row) -> bool:
        return self.poison is not None and self.poison(row)

    @staticmethod
    def _error() -> ValueError:
        return ValueError(
            "invalid input for query argument $2 in element #0 of executemany() sequence: "
            "18446743641752788935 (value out of int64 range)"
        )

    async def executemany(self, query: str, rows) -> None:
        if self.fail:
            raise DatabaseUnavailable("simulated outage")
        if any(self._rejected(row) for row in rows):
            raise self._error()
        self.rows.extend(rows)

    async def execute(self, query: str, *args) -> str:
        if self.fail:
            raise DatabaseUnavailable("simulated outage")
        if self._rejected(args):
            raise self._error()
        self.rows.append(args)
        return "INSERT 0 1"


class FakeLive:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, payload) -> None:
        self.published.append(dict(payload))


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture
def settings():
    # Backup disabled so the credential validator is not exercised here.
    return Settings(backup=BackupSettings(enabled=False))


@pytest.fixture
def rules(settings, registry):
    return DerivedRules(settings, registry)


def types(events) -> list[EventType]:
    return [event.event_type for event in events]


SAMPLE_INTERVAL_MS = 60_000
"""One sample per minute — the sensor's idle cadence.

The default here, but no longer load-bearing for the rate rule: that one estimates a slope
over ``do_rate_window_ms``, so a fast cadence no longer turns ordinary noise into an
implausible figure. The *jump* rule is still evaluated on adjacent samples, so a sub-second
interval would trip it on any step over ``do_jump_mg_l``. One per minute keeps the values
in these tests looking like water.
"""


def feed(
    rules,
    registry,
    values,
    *,
    role="od_sensor",
    field="od_do_mg_l",
    interval_ms=SAMPLE_INTERVAL_MS,
):
    """
    Feed a sequence of one metric at a fixed cadence and collect every event.

    Pass ``interval_ms`` to exercise a real sampling rate: the od_sensor runs at 0.2 Hz, and
    the rate rule has to behave the same there as it does at one sample a minute.

    Callers should keep consecutive values within ~0.5 mg/L of each other for DO: a larger
    step legitimately fires ``DO_IMPLAUSIBLE_JUMP``, which would otherwise pollute a
    level-transition test.
    """
    schema = registry.for_role(role)
    produced = []
    for index, value in enumerate(values):
        produced.extend(
            rules.evaluate(
                schema,
                {field: value},
                t_dev_ms=MINUTE_MS + index * interval_ms,
                node_id="a0000000-0000-0000-0000-000000000001",
                experiment_id="EXP-1",
            )
        )
    return produced


def levels_only(events) -> list[EventType]:
    """Filter to DO threshold transitions, excluding rate and jump observations."""
    return [
        event.event_type
        for event in events
        if event.event_type in (EventType.DO_LOW_WARNING, EventType.DO_LOW_CRITICAL, EventType.DO_RECOVERED)
    ]


# ===========================================================================
# Dissolved oxygen
# ===========================================================================


class TestDissolvedOxygen:
    def test_critical_fires_once_on_transition_not_per_sample(self, rules, registry):
        """
        The property that makes the log readable. Nine consecutive critical readings are
        one event, not nine.
        """
        events = feed(rules, registry, [7.0, 2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7])

        critical = [e for e in events if e.event_type is EventType.DO_LOW_CRITICAL]
        assert len(critical) == 1
        assert critical[0].severity is Severity.CRITICAL

    def test_warning_then_critical_escalates(self, rules, registry):
        # Filtered to threshold transitions: the larger steps also (correctly) raise
        # rate and jump events, which are asserted separately below.
        events = feed(rules, registry, [7.0, 6.6, 5.2, 4.8, 3.2, 2.8])

        assert levels_only(events) == [EventType.DO_LOW_WARNING, EventType.DO_LOW_CRITICAL]

    def test_recovery_emits(self, rules, registry):
        events = feed(rules, registry, [7.0, 4.5, 8.0])

        assert EventType.DO_LOW_WARNING in types(events)
        assert EventType.DO_RECOVERED in types(events)

    def test_deadband_prevents_flapping(self, rules, registry):
        """
        A value oscillating around the warning threshold must not produce an event per
        oscillation. The deadband holds the level once it is set.
        """
        # 5.0 is the warning threshold. Once below it, values between 5.0 and 5.3 stay
        # in warning rather than repeatedly recovering and re-triggering.
        events = feed(rules, registry, [4.9, 5.1, 4.9, 5.2, 5.05, 5.15, 4.95])

        warnings = [e for e in events if e.event_type is EventType.DO_LOW_WARNING]
        recovered = [e for e in events if e.event_type is EventType.DO_RECOVERED]

        assert len(warnings) == 1, "only the initial transition"
        assert recovered == [], "deadband should have held the level"

    def test_recovery_above_deadband_is_reported(self, rules, registry):
        # 5.4 clears the 5.3 recovery point, and the 0.9 step stays under the jump
        # threshold so this test isolates the level rule.
        events = feed(rules, registry, [4.5, 5.4])

        assert levels_only(events) == [EventType.DO_LOW_WARNING, EventType.DO_RECOVERED]

    def test_drop_rate_exceeded(self, rules, registry):
        """
        A sustained fall is a real event.

        Three idle samples is the least that covers the two-minute trend window, so the fall
        has to have lasted long enough for a rate to exist at all. The reported figure is the
        slope over those three points, not the last step between two of them.
        """
        events = feed(rules, registry, [7.0, 6.0, 5.5])

        rate_events = [e for e in events if e.event_type is EventType.DO_DROP_RATE_EXCEEDED]
        assert len(rate_events) == 1
        assert rate_events[0].detail["rate_mg_l_per_min"] < -0.5

    def test_a_rate_needs_the_whole_window(self, rules, registry):
        """
        Two idle samples are a minute apart -- less history than the trend window holds.

        The rule reports nothing rather than extrapolating a rate from a pair of points. A
        rate estimated over a single interval is exactly what made ordinary noise read as a
        crash, so having no rate at all is the right answer here.
        """
        events = feed(rules, registry, [7.0, 5.5])

        assert EventType.DO_DROP_RATE_EXCEEDED not in types(events)

    def test_gradual_fall_does_not_raise_a_rate_event(self, rules, registry):
        """Falling slowly toward the threshold is the normal case and must stay quiet."""
        events = feed(rules, registry, [7.00, 6.99, 6.98, 6.97])

        assert EventType.DO_DROP_RATE_EXCEEDED not in types(events)

    def test_noise_at_the_experiment_cadence_does_not_raise_a_rate_event(self, rules, registry):
        """
        Regression for the storm this rule used to produce.

        At 5 Hz one interval is 200 ms, and normalising it to a minute divided by its own
        length -- multiplying the noise by 300. A 0.0017 mg/L dip from a healthy probe read as
        a 0.5 mg/L/min fall, so the alarm fired on roughly half the samples of a node that was
        working perfectly. Sampled at the real cadence, for well more than the window, with
        realistic noise, it must stay silent.
        """
        rng = random.Random(20261010)
        values = [7.8 + rng.gauss(0, 0.05) for _ in range(900)]

        events = feed(rules, registry, values, interval_ms=200)

        assert EventType.DO_DROP_RATE_EXCEEDED not in types(events)

    def test_a_continuing_fall_is_one_event_not_one_per_sample(self, rules, registry):
        """
        Latched, like the level rules: a sustained condition reports once.

        Unlatched, this rule wrote an event every five seconds for as long as the water kept
        falling -- dozens of events for one continuous fact.
        """
        # 0.2 Hz, falling 0.05 mg/L per sample = 0.6 mg/L/min, sustained past the window.
        values = [8.0 - 0.05 * index for index in range(60)]

        events = feed(rules, registry, values, interval_ms=5000)

        rate_events = [e for e in events if e.event_type is EventType.DO_DROP_RATE_EXCEEDED]
        assert len(rate_events) == 1, "one condition, recorded once"

    def test_implausible_jump_is_flagged(self, rules, registry):
        """
        A step larger than a sensor can physically report is an artefact — a bubble, an
        air gap, fouling — not a change in the water.
        """
        events = feed(rules, registry, [7.0, 7.1, 9.5])

        jumps = [e for e in events if e.event_type is EventType.DO_IMPLAUSIBLE_JUMP]
        assert len(jumps) == 1
        assert jumps[0].detail["delta"] == pytest.approx(2.4, abs=1e-3)

    def test_jump_is_not_latched(self, rules, registry):
        """Each step change is an instantaneous observation, so each is recorded."""
        events = feed(rules, registry, [7.0, 9.5, 7.0])

        jumps = [e for e in events if e.event_type is EventType.DO_IMPLAUSIBLE_JUMP]
        assert len(jumps) == 2

    def test_authoritative_role_only(self, rules, registry):
        """
        The aerator reports ``peer_do_mg_l``, an ESP-NOW echo of the sensor's value.
        Evaluating DO on the aerator would double every alert — and if the echo went
        stale while the sensor was fine, it would alert on water nobody measured.
        """
        schema = registry.for_role("aerator")
        events = rules.evaluate(
            schema,
            {"od_do_mg_l": 1.0},  # a DO field on a non-authoritative role
            t_dev_ms=MINUTE_MS,
            node_id="b0000000-0000-0000-0000-000000000002",
            experiment_id="EXP-1",
        )

        assert events == []

    def test_missing_value_is_ignored(self, rules, registry):
        assert feed(rules, registry, [None, None]) == []


# ===========================================================================
# Temperature and battery
# ===========================================================================


class TestOtherRules:
    def test_high_temperature_fires_once(self, rules, registry):
        events = feed(rules, registry, [23.0, 31.0, 31.5, 32.0], field="od_temp_c")

        assert types(events) == [EventType.TEMP_HIGH]

    def test_temperature_recovery(self, rules, registry):
        events = feed(rules, registry, [31.0, 28.0], field="od_temp_c")

        assert types(events) == [EventType.TEMP_HIGH, EventType.TEMP_LOW]

    def test_battery_escalates_once(self, rules, registry):
        """
        Battery is two levels, and the critical one is one-way: a logger battery that
        recovers without intervention means someone changed it, which is an operator
        fact rather than a telemetry one.
        """
        events = feed(rules, registry, [4.2, 3.4, 3.3, 3.1, 3.0, 3.05], field="pwr_battery_v")

        assert types(events) == [EventType.BATTERY_LOW, EventType.BATTERY_CRITICAL]

    def test_thresholds_are_configurable(self, registry):
        custom = Thresholds(do_warning_mg_l=8.0, do_critical_mg_l=7.0)
        engine = DerivedRules(Settings(backup=BackupSettings(enabled=False)), registry, custom)

        events = feed(engine, registry, [6.5])

        assert types(events) == [EventType.DO_LOW_CRITICAL]

    def test_levels_are_introspectable(self, rules, registry):
        feed(rules, registry, [4.5])

        levels = rules.current_levels()

        assert levels["od_sensor"]["do"] == "WARNING"


# ===========================================================================
# Event stream
# ===========================================================================


def make_event(**overrides) -> Event:
    payload = {
        "event_type": EventType.NODE_ONLINE,
        "source": Source.FIRMWARE,
        "node_id": "b0000000-0000-0000-0000-000000000002",
    }
    payload.update(overrides)
    return Event.make(**payload)


class TestEventStream:
    async def test_buffers_then_flushes(self):
        db = FakeDB()
        stream = EventStream(db, buffer_max=10)

        await stream.emit(make_event())
        assert db.rows == [], "a single event should not force a round trip"

        written = await stream.flush()

        assert written == 1
        assert len(db.rows) == 1

    async def test_publishes_to_live_even_when_persistence_fails(self):
        """The dashboard should see the event even if the database is unreachable."""
        live = FakeLive()
        stream = EventStream(FakeDB(fail=True), buffer_max=10)

        await stream.emit(make_event(), live=live)

        assert len(live.published) == 1
        assert live.published[0]["kind"] == "event"

    async def test_failure_rebuffers_rather_than_losing(self):
        db = FakeDB(fail=True)
        stream = EventStream(db, buffer_max=50)

        await stream.emit(make_event())
        await stream.flush()

        assert stream.stats()["buffered"] == 1, "the event must still be pending"

    async def test_recovery_writes_the_backlog(self):
        db = FakeDB(fail=True)
        stream = EventStream(db, buffer_max=50)
        await stream.emit(make_event())
        await stream.flush()

        db.fail = False
        written = await stream.flush()

        assert written == 1
        assert stream.stats()["buffered"] == 0

    async def test_overflow_spills_instead_of_dropping(self, tmp_path):
        """
        Events are the audit record; an event log that silently loses its most
        interesting entries is worse than none, because it implies coverage it does not
        have. Overflow goes to disk.
        """
        spill = tmp_path / "spill.jsonl"
        stream = EventStream(FakeDB(fail=True), buffer_max=3, spill_path=spill)

        for _ in range(6):
            await stream.emit(make_event())

        assert spill.exists()
        lines = [json.loads(line) for line in spill.read_text().splitlines() if line.strip()]
        assert len(lines) >= 1
        assert stream.stats()["spilled"] >= 1

    async def test_overflow_without_a_spill_path_counts_the_loss(self):
        stream = EventStream(FakeDB(fail=True), buffer_max=3, spill_path=None)

        for _ in range(6):
            await stream.emit(make_event())

        assert stream.stats()["spilled"] >= 1
        assert stream.spill_count() == 0

    async def test_replay_drains_the_spill_file(self, tmp_path):
        spill = tmp_path / "spill.jsonl"
        spill.write_text(
            "\n".join(
                json.dumps(
                    {
                        "ts": 1_760_004_000.0,
                        "t_dev_ms": MINUTE_MS,
                        "experiment_id": "EXP-1",
                        "event_type": "node_offline",
                        "severity": "warning",
                        "scope": "node",
                        "source": "gateway",
                        "node_id": None,
                        "correlation_id": None,
                        "action": None,
                        "detail": {"reason": "lwt"},
                    }
                )
                for _ in range(3)
            )
            + "\n"
        )
        db = FakeDB()
        stream = EventStream(db, spill_path=spill)

        replayed = await stream.replay_spill()

        assert replayed == 3
        assert len(db.rows) == 3
        assert not spill.exists()
        assert stream.stats()["replayed"] == 3

    async def test_replay_keeps_the_file_when_the_write_fails(self, tmp_path):
        """
        Truncation happens only after a successful write, so a crash mid-replay
        duplicates a few events rather than losing them — duplicates are the correct
        failure direction for an audit log.
        """
        spill = tmp_path / "spill.jsonl"
        spill.write_text(
            json.dumps(
                {
                    "ts": 1_760_004_000.0,
                    "event_type": "node_online",
                    "severity": "info",
                    "scope": "node",
                    "source": "firmware",
                }
            )
            + "\n"
        )
        stream = EventStream(FakeDB(fail=True), spill_path=spill)

        assert await stream.replay_spill() == 0
        assert spill.exists()

    async def test_row_arity_matches_the_insert(self):
        """Same guard as the rollup insert: a mismatch writes into the wrong columns."""
        from aqua_gateway.events.stream import INSERT_SQL, _as_row

        row = _as_row(make_event())
        placeholders = sum(f"${index}" in INSERT_SQL for index in range(1, 20))

        assert len(row) == 11
        assert placeholders == 11

    async def test_rejects_unknown_filter_keys(self):
        """A filter assembled from user input is how a read endpoint becomes a write."""
        stream = EventStream(FakeDB())

        with pytest.raises(ValueError, match="unsupported filter"):
            await stream.recent(filters={"'; DROP TABLE events; --": "x"})

    async def test_out_of_range_device_time_becomes_null_not_a_failed_batch(self):
        """
        A node whose clock never synced can report a wrapped 64-bit time. ``t_dev_ms`` is a
        signed ``bigint``, so the value is dropped to NULL — a timestamp no reader could
        have used anyway. Storing the event is what matters; the timestamp is not.
        """
        from aqua_gateway.events.stream import _as_row, sane_node_id, sane_t_dev_ms

        assert sane_t_dev_ms(2**64 - 1) is None, "wrapped unsigned time"
        assert sane_t_dev_ms(MINUTE_MS) == MINUTE_MS
        assert sane_t_dev_ms(None) is None
        assert sane_t_dev_ms("not-a-number") is None
        assert sane_node_id("not-a-uuid") is None
        assert sane_node_id("b0000000-0000-0000-0000-000000000002") == "b0000000-0000-0000-0000-000000000002"

        assert _as_row(make_event(t_dev_ms=2**64 - 1))[1] is None

    async def test_one_unstoreable_row_does_not_stall_the_stream(self, tmp_path):
        """
        The batch is retried verbatim, so a single row the database will never accept used
        to stop the event log from advancing — permanently, with only a repeated WARNING to
        show for it. Retrying row by row bounds the damage to that one event, which is
        quarantined where it can be read rather than dropped without trace.
        """
        spill = tmp_path / "spill.jsonl"
        db = FakeDB(poison=lambda row: row[3] == EventType.PROTOCOL_ERROR.value)
        stream = EventStream(db, buffer_max=10, spill_path=spill)

        await stream.emit(make_event(event_type=EventType.PROTOCOL_ERROR))
        await stream.emit(make_event(event_type=EventType.NODE_ONLINE))

        written = await stream.flush()

        assert written == 1, "the event the database can store still lands"
        assert len(db.rows) == 1
        assert stream.stats()["buffered"] == 0, "the stream is not stalled"
        assert stream.stats()["rejected"] == 1
        assert "protocol_error" in (tmp_path / "spill.jsonl.rejected").read_text()

    async def test_a_database_outage_still_rebuffers_rather_than_rejecting(self):
        """
        Row-by-row retry is for data the table refuses, not for a database that is down. An
        outage must still hold every event for the next attempt, or the isolation would
        turn a reconnect into data loss.
        """
        stream = EventStream(FakeDB(fail=True), buffer_max=10, spill_path=None)

        await stream.emit(make_event())
        written = await stream.flush()

        assert written == 0
        assert stream.stats()["buffered"] == 1
        assert stream.stats()["rejected"] == 0
