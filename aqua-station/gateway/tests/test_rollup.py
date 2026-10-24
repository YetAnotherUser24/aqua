"""
Rollup tests.

The aggregation is deliberately pure — in-memory accumulation with no I/O — so every
rule can be checked by feeding it a sequence of values. That is the point of the
design: the arithmetic is where a mistake would be invisible for weeks, because a
slightly-wrong average still looks like a plausible average.

Two invariants get explicit tests:

* ``to_row()`` produces a tuple whose arity matches ``INSERT_SQL``'s placeholders. A
  mismatch here writes values into the wrong columns, which is the classic way a
  hand-built bulk insert corrupts data quietly.
* ``pump_dosed_ml`` sums *positive deltas*, so a dose-counter reset does not subtract
  the previous dose from the bucket.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from aqua_gateway.db.engine import DatabaseUnavailable
from aqua_gateway.rollup.aggregator import (
    AGG_COLUMNS,
    COLUMN_SOURCE,
    INSERT_SQL,
    METRIC_SPECS,
    Agg,
    RollupAggregator,
    bucket_start,
)
from aqua_gateway.schema.registry import load_registry

# 2026-10-09T12:00:00Z and the minute after it.
MINUTE_MS = 1_791_547_200_000
NEXT_MINUTE_MS = MINUTE_MS + 60_000


class FakeDB:
    """Minimal stand-in for :class:`Database`. Records batches, can fail on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[tuple]] = []
        self.fail = fail
        self.available = True

    async def executemany(self, query: str, rows) -> None:
        if self.fail:
            raise DatabaseUnavailable("simulated outage")
        self.batches.append(list(rows))

    @property
    def row_count(self) -> int:
        return sum(len(batch) for batch in self.batches)


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture
def aggregator(registry):
    return RollupAggregator(registry)


def sample(**overrides) -> dict:
    row = {
        "drv_rpm": 400.0,
        "drv_rpm_target": 420.0,
        "drv_power_w": 80.0,
        "drv_vbus_v": 24.0,
        "drv_ibus_a": 3.3,
        "drv_running": True,
        "od_do_mg_l": 7.5,
        "od_sat_pct": 82.0,
        "od_temp_c": 23.0,
        "pump_dosed_ml": 0.0,
        "pwr_battery_v": 4.15,
    }
    row.update(overrides)
    return row


# ===========================================================================
# Declarations are self-consistent
# ===========================================================================


class TestColumnMapping:
    def test_every_aggregate_column_has_a_source(self):
        """
        Guarded at import time too, but asserted here so the failure names the column
        rather than surfacing as a stack trace during startup.
        """
        assert set(AGG_COLUMNS) == set(COLUMN_SOURCE)

    def test_column_names_use_the_suffix_not_the_enum_value(self):
        """
        ``pct_true`` -> ``_pct`` and ``delta_sum`` -> ``_sum``. Deriving the column from
        the enum value would produce names no query finds, and no error.
        """
        assert Agg.PCT_TRUE.suffix == "pct"
        assert Agg.DELTA_SUM.suffix == "sum"
        assert "drv_running_pct" in AGG_COLUMNS
        assert "pump_dosed_ml_sum" in AGG_COLUMNS

    def test_sql_placeholder_count_matches_the_row_arity(self, aggregator, registry):
        """
        The invariant that keeps the bulk insert from writing into the wrong columns.
        """
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS, experiment_id=None)
        buckets = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)

        row = buckets[0].to_row(samples_expected=100)
        placeholders = INSERT_SQL.count("$") - INSERT_SQL.count("EXCLUDED.$")

        # 6 leading columns (bucket, role, experiment_id, samples, samples_expected,
        # downtime_s) + the aggregates + uncertain_frac.
        assert len(row) == 6 + len(AGG_COLUMNS) + 1
        assert placeholders == len(row), "placeholder count must match the tuple arity"

    def test_aggregations_declared_but_missing_raise(self):
        """A spec that references a non-existent column must fail loudly, not silently."""
        from aqua_gateway.rollup.aggregator import MetricSpec

        assert all(spec.aggs for spec in METRIC_SPECS)
        assert MetricSpec("od_do_mg_l", (Agg.AVG,)).field == "od_do_mg_l"

    def test_a_rewritten_bucket_refreshes_every_column_it_writes(self):
        """
        A second write of the same ``(role, bucket)`` must not leave half the row behind.

        Reaching the conflict is ordinary: an envelope arriving after its minute was flushed
        re-creates the bucket and writes the same key again. The upsert refreshed every column
        except ``experiment_id``, which is the one that decides the cadence
        ``samples_expected`` was counted against -- so a bucket straddling monitor→experiment
        kept the old mode beside the new count, and the row held nothing that said so.

        Stated as an invariant rather than a spot check, because the next column added to the
        insert would otherwise fail the same way.
        """
        inserted = {
            column.strip()
            for column in re.search(r"INSERT INTO rollup_1min \(([^)]+)\)", INSERT_SQL).group(1).split(",")
        }
        refreshed = set(re.findall(r"(\w+) = EXCLUDED\.", INSERT_SQL))

        # ``role`` and ``bucket`` are the conflict key, so they are equal by definition and
        # cannot meaningfully be refreshed.
        assert refreshed == inserted - {"role", "bucket"}, (
            "every inserted column except the conflict key must be refreshed on conflict, "
            f"missing: {sorted(inserted - {'role', 'bucket'} - refreshed)}"
        )


# ===========================================================================
# Aggregation arithmetic
# ===========================================================================


class TestAccumulation:
    def test_average_min_max(self, aggregator, registry):
        schema = registry.for_role("aerator")
        for value in (400.0, 420.0, 380.0, 440.0):
            aggregator.add(schema, sample(drv_rpm=value), t_dev_ms=MINUTE_MS, experiment_id=None)

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        row = bucket.to_row(samples_expected=None)
        labels = ("bucket", "role", "exp", "samples", "expected", "downtime", *AGG_COLUMNS, "uncertain")
        values = dict(zip(labels, row, strict=True))

        assert values["samples"] == 4
        assert values["drv_rpm_avg"] == pytest.approx(410.0, abs=1e-4)
        assert values["drv_rpm_min"] == 380.0
        assert values["drv_rpm_max"] == 440.0

    def test_percentage_true(self, aggregator, registry):
        schema = registry.for_role("aerator")
        for running in (True, True, False, True):
            aggregator.add(schema, sample(drv_running=running), t_dev_ms=MINUTE_MS, experiment_id=None)

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        values = dict(zip(AGG_COLUMNS, bucket.to_row(samples_expected=None)[6:]))

        assert values["drv_running_pct"] == pytest.approx(75.0, abs=1e-3)

    def test_missing_values_do_not_count_as_zero(self, aggregator, registry):
        """
        A metric absent from some samples must not be averaged against an implicit
        zero. Treating "not reported" as "zero" is a different claim about the water.
        """
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(od_do_mg_l=None), t_dev_ms=MINUTE_MS, experiment_id=None)
        aggregator.add(schema, sample(od_do_mg_l=8.0), t_dev_ms=MINUTE_MS + 1000, experiment_id=None)

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        values = dict(zip(AGG_COLUMNS, bucket.to_row(samples_expected=None)[6:]))

        assert values["od_do_mg_l_avg"] == 8.0, "average over one sample, not over two"

    def test_uncertain_fraction(self, aggregator, registry):
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS, experiment_id=None, flagged=True)
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS + 1000, experiment_id=None, flagged=False)
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS + 2000, experiment_id=None, flagged=True)
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS + 3000, experiment_id=None, flagged=False)

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        row = bucket.to_row(samples_expected=None)

        assert row[-1] == 0.5


class TestDosingCounter:
    def test_positive_deltas_are_summed(self, aggregator, registry):
        """A dose accrues: 0 -> 10 -> 25 -> 40 mL delivered 40 mL, not 75."""
        schema = registry.for_role("pump")
        for value in (0.0, 10.0, 25.0, 40.0):
            aggregator.add(schema, {"pump_dosed_ml": value}, t_dev_ms=MINUTE_MS, experiment_id=None)

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        values = dict(zip(AGG_COLUMNS, bucket.to_row(samples_expected=None)[6:]))

        assert values["pump_dosed_ml_sum"] == pytest.approx(40.0, abs=1e-4)

    def test_a_counter_reset_does_not_subtract(self, aggregator, registry):
        """
        The reason for positive-delta summation. The counter resets between doses; a
        naive sum of deltas would report -50 mL and a naive sum of values would report
        100 mL. The correct answer over two doses of 50 mL each is 100 mL.
        """
        schema = registry.for_role("pump")
        for value in (0.0, 25.0, 50.0, 0.0, 25.0, 50.0):
            aggregator.add(schema, {"pump_dosed_ml": value}, t_dev_ms=MINUTE_MS, experiment_id=None)

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        values = dict(zip(AGG_COLUMNS, bucket.to_row(samples_expected=None)[6:]))

        assert values["pump_dosed_ml_sum"] == pytest.approx(100.0, abs=1e-4)


# ===========================================================================
# Bucketing
# ===========================================================================


class TestBuckets:
    def test_bucket_start_truncates_to_the_minute(self):
        start = bucket_start(MINUTE_MS + 47_321)

        assert start == datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    def test_samples_in_one_minute_share_a_bucket(self, aggregator, registry):
        schema = registry.for_role("aerator")
        for offset in (0, 1_000, 30_000, 59_999):
            aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS + offset, experiment_id=None)

        assert aggregator.open_buckets == 1
        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        assert bucket.samples == 4

    def test_crossing_a_minute_closes_the_previous_bucket(self, aggregator, registry):
        """
        A bucket closes when the *next* sample arrives past its boundary — not on a
        timer — so a role that goes quiet mid-run still contributes its last partial
        minute rather than leaving it open indefinitely.
        """
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS, experiment_id=None)
        completed = aggregator.add(schema, sample(), t_dev_ms=NEXT_MINUTE_MS, experiment_id=None)

        assert len(completed) == 1
        assert completed[0].samples == 1
        assert aggregator.open_buckets == 1, "only the new bucket remains open"

    def test_roles_bucket_independently(self, aggregator, registry):
        aggregator.add(registry.for_role("aerator"), sample(), t_dev_ms=MINUTE_MS, experiment_id=None)
        aggregator.add(registry.for_role("mixer"), {"drv_rpm": 500.0}, t_dev_ms=NEXT_MINUTE_MS, experiment_id=None)

        assert aggregator.open_buckets == 2, "one role moving on must not close another's bucket"

    def test_due_excludes_the_current_minute(self, aggregator, registry):
        """
        A bucket is only complete once the clock has passed its end. Emitting at the
        boundary risks writing a bucket that still has a sample coming.
        """
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS, experiment_id=None)

        assert aggregator.due(now=MINUTE_MS / 1000 + 10) == [], "still inside the minute"
        assert len(aggregator.due(now=NEXT_MINUTE_MS / 1000 + 1)) == 1

    def test_samples_expected_uses_the_mode_from_the_payload(self, aggregator, registry):
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS, experiment_id="EXP-1")

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        expected = aggregator._samples_expected(bucket)

        # Experiment cadence is 5 Hz; one minute at 5 Hz is 300 samples.
        assert expected == 300

    def test_idle_mode_uses_the_idle_rate(self, aggregator, registry):
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS, experiment_id=None)

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)

        # Idle is 1/minute, so a one-minute bucket expects exactly one sample.
        assert aggregator._samples_expected(bucket) == 1

    def test_downtime_reported_when_short(self, aggregator, registry):
        """
        A bucket averaging a third of its expected samples is not the same claim as one
        covering the whole minute, so the shortfall is recorded rather than hidden.
        """
        schema = registry.for_role("aerator")
        # 10 samples 5 s apart: all inside the minute, which matters because a sample
        # past the boundary would close the bucket and leave `due()` with nothing.
        for offset in range(0, 50_000, 5_000):
            aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS + offset, experiment_id="EXP-1")

        (bucket,) = aggregator.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        row = bucket.to_row(samples_expected=300)
        values = dict(zip(("bucket", "role", "exp", "samples", "expected", "downtime"), row[:6]))

        assert values["samples"] == 10
        assert values["downtime"] == 58

    def test_narrow_bucket_reports_downtime_in_seconds_of_that_bucket(self, registry):
        """
        Downtime is seconds of *this* bucket, not seconds of a minute.

        The formula used to hardcode a 60 s base, so a 15 s bucket holding 29 of its expected
        75 samples was written out as "37 s of downtime" -- a figure larger than the bucket it
        described. That is not a smaller error, it is a meaningless one, and it is invisible
        unless you notice the number cannot fit.
        """
        narrow = RollupAggregator(registry, bucket_seconds=15)
        schema = registry.for_role("aerator")
        # 7 samples of an expected 75 spread across a 15 s bucket.
        for offset in range(0, 33_000, 5_000):
            narrow.add(schema, sample(), t_dev_ms=MINUTE_MS + offset, experiment_id="EXP-1")

        (bucket,) = narrow.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        row = bucket.to_row(samples_expected=75)
        values = dict(zip(("bucket", "role", "exp", "samples", "expected", "downtime"), row[:6]))

        assert values["samples"] == 7
        assert values["downtime"] == 14, "15 * (1 - 7/75) = 13.6, so 14 s of a 15 s bucket"
        assert values["downtime"] <= 15

    def test_idle_bucket_narrower_than_the_idle_period_is_not_an_outage(self, registry):
        """
        A monitor run is not an experiment that failed.

        Idle cadence is one sample a minute, so in a 15 s bucket the expected count rounds to
        zero. A ``samples_expected`` of 0 was reported as a *full minute* of downtime, which
        meant a node deliverng 57 samples in the bucket was recorded as 60 s down. Expecting
        nothing means nothing can have been missed, so the answer has to be no downtime.

        This is the monitor case specifically: the mode an operator watches while checking
        sensor integrity would have shown as a dead line on every chart.
        """
        narrow = RollupAggregator(registry, bucket_seconds=15)
        schema = registry.for_role("aerator")
        for offset in range(0, 13_000, 5_000):
            narrow.add(schema, sample(), t_dev_ms=MINUTE_MS + offset, experiment_id=None)

        (bucket,) = narrow.due(now=(NEXT_MINUTE_MS / 1000) + 1)
        expected = narrow._samples_expected(bucket)
        # 1 sample/minute against a 15 s bucket rounds to 0 -- the case that used to misfire.
        assert expected == 0

        row = bucket.to_row(samples_expected=expected)
        values = dict(zip(("bucket", "role", "exp", "samples", "expected", "downtime"), row[:6]))

        assert values["samples"] == 3
        assert values["downtime"] == 0


# ===========================================================================
# Persistence
# ===========================================================================


class TestFlush:
    async def test_writes_one_batch_for_many_buckets(self, aggregator, registry):
        """
        The write-amplification point: four roles' worth of buckets become one round
        trip, not one insert per sample.
        """
        for role in ("aerator", "mixer", "pump", "od_sensor"):
            aggregator.add(registry.for_role(role), sample(), t_dev_ms=MINUTE_MS, experiment_id=None)

        db = FakeDB()
        written = await aggregator.flush(db, now=(NEXT_MINUTE_MS / 1000) + 1)

        assert written == 4
        assert len(db.batches) == 1, "one executemany for all buckets"
        assert db.row_count == 4

    async def test_no_rows_means_no_round_trip(self, aggregator):
        db = FakeDB()

        assert await aggregator.flush(db, now=MINUTE_MS / 1000) == 0
        assert db.batches == []

    async def test_database_failure_is_swallowed(self, aggregator, registry):
        """
        Rollups are derived data. A database outage must not propagate into the MQTT
        ingest loop, because that would let derived-data failure stop telemetry.
        """
        aggregator.add(registry.for_role("aerator"), sample(), t_dev_ms=MINUTE_MS, experiment_id=None)

        written = await aggregator.flush(FakeDB(fail=True), now=(NEXT_MINUTE_MS / 1000) + 1)

        assert written == 0
        assert aggregator.last_error is not None

    async def test_recovery_clears_the_error(self, aggregator, registry):
        schema = registry.for_role("aerator")
        aggregator.add(schema, sample(), t_dev_ms=MINUTE_MS, experiment_id=None)
        await aggregator.flush(FakeDB(fail=True), now=(NEXT_MINUTE_MS / 1000) + 1)
        assert aggregator.last_error is not None

        # A sample two minutes on closes the bucket opened at NEXT_MINUTE_MS. Passing
        # the completed buckets explicitly avoids depending on the clock.
        aggregator.add(schema, sample(), t_dev_ms=NEXT_MINUTE_MS, experiment_id=None)
        completed = aggregator.add(schema, sample(), t_dev_ms=NEXT_MINUTE_MS + 60_000, experiment_id=None)
        written = await aggregator.flush(FakeDB(), buckets=completed)

        assert written == 1
        assert aggregator.last_error is None

    def test_discard_empties_state(self, aggregator, registry):
        aggregator.add(registry.for_role("aerator"), sample(), t_dev_ms=MINUTE_MS, experiment_id=None)

        assert aggregator.discard() == 1
        assert aggregator.open_buckets == 0


# ===========================================================================
# The declared cadence has to describe the firmware
# ===========================================================================


class TestDeclaredCadenceMatchesTheFirmware:
    """
    `expected_rate_hz` is divided into the bucket to compute downtime, so it must describe what
    the node actually sends. The mixer and the pump both declared `{experiment: 5.0, idle:
    0.0167}` -- identical to the aerator, which is what copy-paste looks like -- while
    `config.h` has them publish every 3000 ms running and 20000 ms idle. At 5.0 Hz a healthy
    running pump was written out with ~56 s of downtime in every 60 s bucket, in the mode an
    operator watches to check that the system is working.

    The od_sensor carried that same copied pair while sampling every 5000 ms, so it is asserted
    here too. The defect is not "a sensing node" -- it is any role declaring a rate its firmware
    does not produce, in either direction.
    """

    def test_mixer_and_pump_declare_their_real_cadence(self, registry):
        # 1/3 Hz running (config.h TELEMETRY_INTERVAL_MS = 3000) and 1/20 Hz idle.
        for role in ("mixer", "pump"):
            rate = registry.for_role(role).expected_rate_hz
            assert rate["experiment"] == pytest.approx(1 / 3.0, rel=1e-3), (
                f"{role} publishes every 3000 ms while running"
            )
            assert rate["idle"] == pytest.approx(1 / 20.0, rel=1e-3), (
                f"{role} publishes every 20000 ms idle; 0.0167 is one sample a minute, "
                "which is neither node's idle cadence"
            )

    def test_each_sampling_node_declares_its_own_real_cadence(self, registry):
        # The aerator samples every 200 ms (TELEMETRY_SAMPLE_INTERVAL_MS) and batches into
        # envelopes, so 5 Hz is right for it. The od_sensor does not: `main.cpp` samples every
        # `experimentIntervalMs` (5000 ms), so 0.2 Hz is what it produces. Asserting one number
        # for both is how the od_sensor came to claim 5 Hz -- the same copy-paste, one level
        # down -- so they are asserted separately rather than as a pair.
        assert registry.for_role("aerator").expected_rate_hz["experiment"] == 5.0
        assert registry.for_role("od_sensor").expected_rate_hz["experiment"] == pytest.approx(0.2)
