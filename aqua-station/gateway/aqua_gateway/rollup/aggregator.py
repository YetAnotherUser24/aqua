"""
1-minute rollups.

Why this exists: the dashboard's charts must never read raw telemetry. A 24-hour chart
over 1 Hz raw data is 86,400 rows; over 1-minute rollups it is 1,440. On a Pi 3B+ that
difference is CPU and — more importantly on an SD card — read amplification.

The trade this module makes is the other half of that: instead of one insert per
sample, samples are accumulated **in memory** and one batched insert is issued per
bucket. At 1-minute buckets that is 4 inserts a minute rather than 5 per second.

Two details worth stating explicitly:

* **Buckets align to device time, not arrival time.** A batch that arrives late still
  lands in the minute it was measured in, so a replay after a network gap does not
  create a phantom bucket. Minute boundaries are identical in UTC and America/Lima,
  so no timezone conversion is needed here.
* **``pump_dosed_ml`` is summed as positive deltas, not as a sum of values.** It is a
  cumulative counter that resets between doses. Summing the raw values would count the
  same millilitres repeatedly; summing only increases handles resets correctly.

This is a pure in-memory accumulator with no I/O, deliberately. Everything that touches
the database happens in :meth:`RollupAggregator.flush`, which makes the aggregation
arithmetic testable without a database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from ..db.engine import Database, DatabaseUnavailable
from ..schema.registry import RoleSchema, SchemaRegistry

log = logging.getLogger("aqua.rollup")


class Agg(str, Enum):
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    PCT_TRUE = "pct_true"
    DELTA_SUM = "delta_sum"

    @property
    def suffix(self) -> str:
        """
        Column-name suffix, which is *not* always the enum value.

        ``pct_true`` maps to ``pct`` so the column reads ``drv_running_pct``, and
        ``delta_sum`` maps to ``sum`` so it reads ``pump_dosed_ml_sum`` — both matching
        the names in ``004_rollups.sql``. Deriving the column from the enum value
        instead would silently produce names no query ever finds.
        """
        return {
            Agg.AVG: "avg",
            Agg.MIN: "min",
            Agg.MAX: "max",
            Agg.PCT_TRUE: "pct",
            Agg.DELTA_SUM: "sum",
        }[self]


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Declares which aggregates a telemetry field contributes to."""

    field: str
    aggs: tuple[Agg, ...]


#: Only fields listed here are aggregated. Anything absent — enums, status words,
#: setpoints nobody charts — stays out of the rollup rather than being averaged into
#: something meaningless.
METRIC_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("drv_rpm", (Agg.AVG, Agg.MIN, Agg.MAX)),
    MetricSpec("drv_rpm_target", (Agg.AVG,)),
    MetricSpec("drv_power_w", (Agg.AVG, Agg.MAX)),
    MetricSpec("drv_vbus_v", (Agg.AVG,)),
    MetricSpec("drv_ibus_a", (Agg.AVG, Agg.MAX)),
    MetricSpec("drv_running", (Agg.PCT_TRUE,)),
    MetricSpec("od_do_mg_l", (Agg.AVG, Agg.MIN, Agg.MAX)),
    MetricSpec("od_sat_pct", (Agg.AVG,)),
    MetricSpec("od_temp_c", (Agg.AVG, Agg.MIN, Agg.MAX)),
    MetricSpec("pump_dosed_ml", (Agg.DELTA_SUM,)),
    MetricSpec("pwr_battery_v", (Agg.MIN,)),
)

#: Aggregate column order in ``rollup_1min``. The INSERT is generated from this tuple,
#: so the column list and the row tuple can never drift apart — which is the usual way
#: a hand-written bulk insert quietly starts writing values into the wrong columns.
AGG_COLUMNS: tuple[str, ...] = (
    "drv_rpm_avg",
    "drv_rpm_min",
    "drv_rpm_max",
    "drv_rpm_target_avg",
    "drv_power_w_avg",
    "drv_power_w_max",
    "drv_vbus_v_avg",
    "drv_ibus_a_avg",
    "drv_ibus_a_max",
    "drv_running_pct",
    "od_do_mg_l_avg",
    "od_do_mg_l_min",
    "od_do_mg_l_max",
    "od_sat_pct_avg",
    "od_temp_c_avg",
    "od_temp_c_min",
    "od_temp_c_max",
    "pump_dosed_ml_sum",
    "pwr_battery_v_min",
)

#: column -> (field, aggregate). Built once from the specs so the two declarations
#: cannot disagree. The guard below fires at import time rather than at midnight, which
#: is when a mismatch would otherwise surface — the first time a bucket is written.
COLUMN_SOURCE: dict[str, tuple[str, Agg]] = {}
for _spec in METRIC_SPECS:
    for _agg in _spec.aggs:
        _column = f"{_spec.field}_{_agg.suffix}"
        if _column in AGG_COLUMNS:
            COLUMN_SOURCE[_column] = (_spec.field, _agg)
        else:
            # Declared as an aggregate but with no matching column: almost always a
            # typo in AGG_COLUMNS, and always a value that would never be written.
            raise RuntimeError(f"{_spec.field} declares {_agg.name} but no column {_column!r} exists in AGG_COLUMNS")

_missing = set(AGG_COLUMNS) - set(COLUMN_SOURCE)
if _missing:  # pragma: no cover - caught at import, which is the point
    raise RuntimeError(
        f"rollup columns have no source metric: {sorted(_missing)}. "
        f"Every entry in AGG_COLUMNS must be named <field>_<agg.suffix> for a declared MetricSpec."
    )


@dataclass(slots=True)
class _FieldState:
    """Running state for one field within one bucket."""

    n: int = 0
    total: float = 0.0
    lo: float | None = None
    hi: float | None = None
    true_count: int = 0
    delta_sum: float = 0.0
    last: float | None = None

    def add(self, value: Any) -> None:
        if isinstance(value, bool):
            self.n += 1
            self.true_count += 1 if value else 0
            return

        try:
            number = float(value)
        except (TypeError, ValueError):
            return

        self.n += 1
        self.total += number
        self.lo = number if self.lo is None else min(self.lo, number)
        self.hi = number if self.hi is None else max(self.hi, number)

        if self.last is not None:
            delta = number - self.last
            # Only increases count. `pump_dosed_ml` resets between doses, and a reset
            # must not subtract the previous dose from this bucket's delivered volume.
            if delta > 0:
                self.delta_sum += delta
        self.last = number

    def value(self, agg: Agg) -> float | None:
        if self.n == 0:
            return None
        if agg is Agg.AVG:
            return round(self.total / self.n, 4)
        if agg is Agg.MIN:
            return self.lo
        if agg is Agg.MAX:
            return self.hi
        if agg is Agg.PCT_TRUE:
            return round(100.0 * self.true_count / self.n, 3)
        if agg is Agg.DELTA_SUM:
            # First sample establishes a baseline, so a single sample contributes 0.
            return round(self.delta_sum, 4)
        return None


@dataclass(slots=True)
class _Bucket:
    role: str
    start: datetime
    experiment_id: str | None
    #: Width of this bucket in seconds. Carried on the bucket rather than passed to
    #: ``to_row`` so it cannot be omitted at a call site -- an absent width silently
    #: reverts to a minute, which is how a 15 s bucket came to report downtime in a unit it
    #: was not aggregated over.
    bucket_seconds: int
    samples: int = 0
    uncertain: int = 0
    fields: dict[str, _FieldState] = field(default_factory=dict)

    def add(self, row: Mapping[str, Any], *, flagged: bool) -> None:
        self.samples += 1
        if flagged:
            self.uncertain += 1
        for spec in METRIC_SPECS:
            value = row.get(spec.field)
            if value is None:
                continue
            self.fields.setdefault(spec.field, _FieldState()).add(value)

    def to_row(self, *, samples_expected: int | None) -> tuple[Any, ...]:
        aggregates: list[Any] = []
        for column in AGG_COLUMNS:
            field_name, agg = COLUMN_SOURCE[column]
            state = self.fields.get(field_name)
            aggregates.append(state.value(agg) if state is not None else None)

        uncertain_fraction = round(self.uncertain / self.samples, 4) if self.samples else 0.0

        downtime = 0
        if samples_expected:
            # Reported rather than silently averaged away: an average over a third of the
            # expected samples is not the same claim as one over all of them.
            #
            # Measured against the bucket, not against a hardcoded minute. With a 15 s bucket
            # an expected count of 75 yielded "37 s of downtime" -- a figure larger than the
            # bucket it described, which is not a smaller error but a meaningless one.
            shortfall = 1 - self.samples / samples_expected
            if shortfall > 0:
                downtime = int(round(self.bucket_seconds * shortfall))

        # ``samples_expected`` of 0 used to be reported as a full minute of downtime. That is
        # backwards, and it is the flag that made a healthy monitor run look dead: idle cadence
        # is one sample a minute, so in a 15 s bucket the expectation rounds to 0 -- and a
        # bucket holding 57 samples was written out claiming 60 s of outage. Expecting nothing
        # means nothing can have been missed, so the answer is no downtime.

        return (
            self.start,
            self.role,
            self.experiment_id,
            self.samples,
            samples_expected,
            downtime,
            *aggregates,
            uncertain_fraction,
        )


#: Columns refreshed when the same ``(role, bucket)`` is written a second time, together with
#: every aggregate column. Reaching the conflict is ordinary rather than exotic: an envelope that
#: arrives after its minute was already flushed re-creates the bucket and writes the same key
#: again, and the aerator's 25-sample batches make a late arrival a matter of one bad second.
#:
#: The rule is that *every* inserted column except the conflict key is refreshed. A partial
#: refresh leaves one half of the row from the first write and the other half from the second,
#: and the row carries nothing that says so. ``experiment_id`` was the column left out, which is
#: the worst one to omit: it is what decides the cadence ``samples_expected`` was counted
#: against, so a bucket straddling monitor→experiment kept the old mode beside the new count.
UPSERT_COLUMNS: tuple[str, ...] = (
    "experiment_id",
    "samples",
    "samples_expected",
    "downtime_s",
    "uncertain_frac",
)

INSERT_SQL = (
    "INSERT INTO rollup_1min (bucket, role, experiment_id, samples, samples_expected, downtime_s, "
    + ", ".join(AGG_COLUMNS)
    + ", uncertain_frac) VALUES ("
    + ", ".join(f"${index}" for index in range(1, 7 + len(AGG_COLUMNS) + 1))
    + ") ON CONFLICT (role, bucket) DO UPDATE SET "
    + ", ".join(f"{column} = EXCLUDED.{column}" for column in (*UPSERT_COLUMNS, *AGG_COLUMNS))
)


def bucket_start(t_dev_ms: int) -> datetime:
    """Truncate a device timestamp to its minute."""
    moment = datetime.fromtimestamp(t_dev_ms / 1000, tz=timezone.utc)
    return moment.replace(second=0, microsecond=0)


class RollupAggregator:
    """Accumulates samples into 1-minute buckets and writes them in batches."""

    def __init__(self, registry: SchemaRegistry, *, bucket_seconds: int = 60) -> None:
        self.registry = registry
        self.bucket_seconds = bucket_seconds
        self._buckets: dict[tuple[str, datetime], _Bucket] = {}
        self.rows_written = 0
        self.buckets_written = 0
        self.last_error: str | None = None

    @property
    def open_buckets(self) -> int:
        """Bounded by the number of roles, so this is a leak canary, not a metric."""
        return len(self._buckets)

    # -- accumulation ------------------------------------------------------

    def add(
        self,
        schema: RoleSchema,
        row: Mapping[str, Any],
        *,
        t_dev_ms: int,
        experiment_id: str | None,
        flagged: bool = False,
    ) -> list[_Bucket]:
        """
        Fold one sample in. Returns any buckets that completed as a result.

        Returning completed buckets instead of writing directly keeps this method pure,
        which is what makes the aggregation arithmetic testable without a database.
        """
        start = bucket_start(t_dev_ms)
        key = (schema.role, start)

        completed: list[_Bucket] = []
        current = self._buckets.get(key)
        if current is None:
            # A new bucket means any older bucket for this role is finished. Close them
            # now rather than waiting for the timer, so a role that goes quiet mid-run
            # still contributes its last partial minute.
            completed.extend(self._close_older_than(schema.role, start))
            current = _Bucket(
                role=schema.role,
                start=start,
                experiment_id=experiment_id,
                bucket_seconds=self.bucket_seconds,
            )
            self._buckets[key] = current

        current.add(row, flagged=flagged)
        return completed

    def _close_older_than(self, role: str, start: datetime) -> list[_Bucket]:
        closed = [
            bucket
            for (bucket_role, bucket_start_at), bucket in self._buckets.items()
            if bucket_role == role and bucket_start_at < start
        ]
        for bucket in closed:
            self._buckets.pop((bucket.role, bucket.start), None)
        return closed

    def due(self, *, now: float | None = None) -> list[_Bucket]:
        """
        Buckets whose minute has fully elapsed.

        A bucket is only complete once the clock has moved past its end. Emitting at
        the boundary itself risks writing a bucket that still has one sample coming.
        """
        moment = now if now is not None else time.time()
        cutoff = bucket_start(int(moment * 1000))
        closed: list[_Bucket] = []
        for key, bucket in list(self._buckets.items()):
            if bucket.start < cutoff:
                closed.append(bucket)
                self._buckets.pop(key, None)
        return closed

    # -- persistence -------------------------------------------------------

    def _samples_expected(self, bucket: _Bucket) -> int | None:
        schema = self.registry.roles.get(bucket.role)
        if schema is None or not schema.expected_rate_hz:
            return None
        # Which cadence applies is known from the payload, not guessed: the firmware
        # tags experiment batches, and idle batches carry no experiment id.
        mode = "experiment" if bucket.experiment_id else "idle"
        rate = schema.expected_rate_hz.get(mode)
        if not rate:
            return None
        return int(round(rate * self.bucket_seconds))

    async def flush(self, db: Database, *, buckets: Iterable[_Bucket] | None = None, now: float | None = None) -> int:
        """
        Write completed buckets. Returns the number of rows written.

        Never raises for a database problem: rollups are derived data, and losing a
        minute costs a re-aggregation. Raising here would propagate into the MQTT
        ingest loop and stop telemetry — the one thing that must not happen.
        """
        pending = list(buckets) if buckets is not None else self.due(now=now)
        if not pending:
            return 0

        rows = [bucket.to_row(samples_expected=self._samples_expected(bucket)) for bucket in pending]

        try:
            await db.executemany(INSERT_SQL, rows)
        except DatabaseUnavailable as exc:
            self.last_error = str(exc)
            log.warning("database unavailable; %d rollup bucket(s) dropped", len(rows))
            return 0
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.exception("rollup insert failed for %d bucket(s)", len(rows))
            return 0

        self.rows_written += len(rows)
        self.buckets_written += len(rows)
        self.last_error = None
        log.debug("wrote %d rollup bucket(s)", len(rows))
        return len(rows)

    def stats(self) -> dict[str, Any]:
        return {
            "open_buckets": self.open_buckets,
            "buckets_written": self.buckets_written,
            "rows_written": self.rows_written,
            "last_error": self.last_error,
            "tracked_metrics": len(METRIC_SPECS),
        }

    def discard(self) -> int:
        """Drop all in-memory state. Used on shutdown after a final flush."""
        dropped = len(self._buckets)
        self._buckets.clear()
        return dropped


def rollup_summary(buckets: Sequence[_Bucket]) -> list[dict[str, Any]]:
    """Debug helper: a JSON-able view of buckets, used by the simulator and tests."""
    return [
        {
            "role": bucket.role,
            "bucket": bucket.start.isoformat(),
            "samples": bucket.samples,
            "uncertain": bucket.uncertain,
            "fields": sorted(bucket.fields),
        }
        for bucket in buckets
    ]
