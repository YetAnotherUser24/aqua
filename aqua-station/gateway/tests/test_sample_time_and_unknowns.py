"""
Regression tests: a per-sample timestamp is part of the format, not an unknown field.

A node samples faster than it publishes. At the 5 Hz experiment rate an envelope carries five
readings, and a single envelope-level ``t_dev_ms`` cannot describe all of them -- every row
lands on the same instant and a plot collapses the five points onto one. So the format reserves
``t_dev_ms`` inside a sample object and ``rows_from_samples`` prefers it when present.

That key sits in an awkward place: it is not a registry field, because ``roles.yaml`` declares
*measurements* and this is metadata the writer puts in the first column itself. But it is not
unknown either. The unknown-field side channel exists precisely so a new firmware field cannot
go missing quietly, so if the reserved key were treated as unknown, a node stamping its own
times -- which is correct behaviour -- would look like it was emitting junk. That alarm would
then be worthless, because it would fire on every healthy node.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from aqua_gateway.archive.csv_writer import rows_from_samples
from aqua_gateway.mqtt.envelope import SAMPLE_RESERVED_FIELDS, split_known_unknown
from aqua_gateway.schema.registry import load_registry

ROLE = "od_sensor"


@pytest.fixture
def schema():
    return load_registry().for_role(ROLE)


def _sample(**overrides: object) -> dict[str, object]:
    """A minimal valid od_sensor sample, with any field overridable."""
    sample: dict[str, object] = {"od_do_mg_l": 7.874, "od_do_raw": 7874}
    sample.update(overrides)
    return sample


# ---------------------------------------------------------------------------
# The reserved key is format, not registry
# ---------------------------------------------------------------------------


def test_reserved_set_names_the_per_sample_timestamp() -> None:
    assert "t_dev_ms" in SAMPLE_RESERVED_FIELDS


def test_reserved_key_is_not_a_registry_field(schema) -> None:
    """
    It must not be one, or the CSV would grow a second ``t_dev_ms`` column.

    ``csv_header`` already prepends ``t_dev_ms`` as the node's own clock, and the writer sets
    that column from the row dict rather than from the projection.
    """
    assert "t_dev_ms" not in schema.field_names


def test_csv_header_has_exactly_one_timestamp_column(schema) -> None:
    header = list(schema.csv_header)
    assert header.count("t_dev_ms") == 1
    assert header[0] == "t_dev_ms"


# ---------------------------------------------------------------------------
# The unknown-field side channel
# ---------------------------------------------------------------------------


def test_reserved_key_alone_is_not_unknown_when_known_is_union_ed(schema) -> None:
    """The union is what the live client actually passes."""
    known = schema.field_names | SAMPLE_RESERVED_FIELDS
    known_values, unknown = split_known_unknown(_sample(t_dev_ms=1_791_568_200_423), known)
    assert unknown == {}
    # ``known_values`` is just the complement of ``unknown``, so the reserved key appears here.
    # Keeping it out of the *row* is the projection's job, asserted below.
    assert known_values["t_dev_ms"] == 1_791_568_200_423


def test_projection_drops_the_reserved_key(schema) -> None:
    """``schema.project`` keeps only registry fields, so the reserved key cannot become a column."""
    projected = schema.project(_sample(t_dev_ms=1_791_568_200_423))
    assert "t_dev_ms" not in projected
    assert projected["od_do_mg_l"] == 7.874


def test_reserved_key_would_be_reported_unknown_without_the_union(schema) -> None:
    """
    Establishes that the union in ``client.py`` is load-bearing.

    If this ever stops failing, the reserved key has become a registry field and the union is
    redundant -- which is worth knowing, not a silent no-op.
    """
    _, unknown = split_known_unknown(_sample(t_dev_ms=1_791_568_200_423), schema.field_names)
    assert unknown == {"t_dev_ms": 1_791_568_200_423}


def test_a_genuinely_unknown_field_is_still_reported(schema) -> None:
    """
    The exclusion must not have switched the side channel off.

    This is the whole point of the channel: an unexpected field has to surface, because the
    previous system lost 5 Hz fidelity for weeks when an unexpected condition failed quietly.
    """
    known = schema.field_names | SAMPLE_RESERVED_FIELDS
    _, unknown = split_known_unknown(_sample(t_dev_ms=1, od_turbidity_ntu=12.5), known)
    assert unknown == {"od_turbidity_ntu": 12.5}


def test_unknown_detection_reports_every_unexpected_field(schema) -> None:
    known = schema.field_names | SAMPLE_RESERVED_FIELDS
    _, unknown = split_known_unknown(_sample(alpha=1, beta=2), known)
    assert set(unknown) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# The timestamp actually reaches the archive
# ---------------------------------------------------------------------------

ENVELOPE_MS = 1_791_568_200_000


def test_rows_use_each_samples_own_timestamp(schema) -> None:
    """Five samples at 5 Hz must not all be filed under one instant."""
    samples = [_sample(od_do_mg_l=7.5 + i / 10) for i in range(5)]
    for index, sample in enumerate(samples):
        sample["t_dev_ms"] = ENVELOPE_MS + index * 200

    rows = rows_from_samples(schema, samples, t_dev_ms=ENVELOPE_MS)

    assert [row["t_dev_ms"] for row in rows] == [ENVELOPE_MS + i * 200 for i in range(5)]
    assert len({row["t_dev_ms"] for row in rows}) == 5  # no collision: the plot keeps 5 points


def test_rows_fall_back_to_the_envelope_timestamp(schema) -> None:
    """
    A node that does not stamp its samples still files under its own clock.

    The fallback is what keeps older firmware working, so it has to stay.
    """
    rows = rows_from_samples(schema, [_sample(), _sample()], t_dev_ms=ENVELOPE_MS)
    assert [row["t_dev_ms"] for row in rows] == [ENVELOPE_MS, ENVELOPE_MS]


def test_rows_do_not_carry_the_reserved_key_as_a_column(schema) -> None:
    """The projection must not leak the reserved key into the measurement columns."""
    row = rows_from_samples(schema, [_sample(t_dev_ms=ENVELOPE_MS + 200)], t_dev_ms=ENVELOPE_MS)[0]
    assert row["t_dev_ms"] == ENVELOPE_MS + 200
    # Exactly one t_dev_ms key, and it is the timestamp -- not a duplicated measurement.
    assert list(row).count("t_dev_ms") == 1


def test_mixed_batch_keeps_only_the_stamped_rows_distinct(schema) -> None:
    """A partially stamped batch is the realistic case, so it must not distort the shared rows."""
    stamped = _sample(t_dev_ms=ENVELOPE_MS + 200)
    unstamped = _sample()
    rows = rows_from_samples(schema, [stamped, unstamped], t_dev_ms=ENVELOPE_MS)
    assert [row["t_dev_ms"] for row in rows] == [ENVELOPE_MS + 200, ENVELOPE_MS]


# ---------------------------------------------------------------------------
# The reserved key must not reach the live view
# ---------------------------------------------------------------------------


async def _fold_one_sample(schema, cache, *, t_dev_ms: int, **overrides: object) -> dict[str, object]:
    row = rows_from_samples(schema, [_sample(t_dev_ms=t_dev_ms, **overrides)], t_dev_ms=t_dev_ms)[0]
    await cache.update(
        schema,
        row=row,
        t_dev_ms=t_dev_ms,
        t_src="rtc",
        seq=1,
        boot_id=1,
        rssi=-61,
        flagged=False,
        schema_version=2,
    )
    return cache.snapshot()["nodes"][schema.role]


async def test_live_metrics_exclude_the_reserved_key(schema) -> None:
    """
    The row carries ``t_dev_ms`` for the CSV writer; that does not make it a measurement.

    The live cache stored every non-null row key as a metric, so the node's clock was served
    by ``/api/live`` and listed on the dashboard as something a sensor had measured. The
    rollup never had the problem because it is driven by ``METRIC_SPECS`` -- and that is
    exactly why it went unnoticed: this loop is the only code that reads the row's keys.
    """
    from aqua_gateway.live.cache import LiveCache

    node = await _fold_one_sample(schema, LiveCache(), t_dev_ms=ENVELOPE_MS + 200)

    assert "t_dev_ms" not in node["metrics"]
    # The real measurements are unaffected.
    assert node["metrics"]["od_do_mg_l"] == 7.874


async def test_live_history_excludes_the_reserved_key(schema) -> None:
    """No sparkline either: a history series would draw the timestamp as a metric curve."""
    from aqua_gateway.live.cache import LiveCache

    cache = LiveCache()
    await _fold_one_sample(schema, cache, t_dev_ms=ENVELOPE_MS + 200)

    assert cache.history(ROLE, "t_dev_ms") == []
    assert cache.history(ROLE, "od_do_mg_l")  # non-empty


async def test_clock_offset_is_still_reported(schema) -> None:
    """
    The timestamp must remain available as *state*, just not as a metric.

    ``NodeState`` exposes it as ``clock_offset_ms`` and ``t_src``; dropping it from ``metrics``
    must not remove it from the snapshot, or the dashboard loses its clock-trust indicator.
    """
    from aqua_gateway.live.cache import LiveCache

    node = await _fold_one_sample(schema, LiveCache(), t_dev_ms=ENVELOPE_MS + 200)

    assert node["schema_version"] == 2
    assert node["t_src"] == "rtc"
    assert node["clock_offset_ms"] is not None


# ---------------------------------------------------------------------------
# The simulator has to produce the same shape as firmware
# ---------------------------------------------------------------------------


def _simulator():
    """
    Load ``tools/mqtt-simulator.py``.

    The filename carries a hyphen, so it is not importable by name; loading it by path is the
    price of keeping the script's name readable. Its dependencies are checked first because
    it calls ``sys.exit`` when one is missing, and an ``sys.exit`` during import would take
    the whole test session down with it rather than fail one test.
    """
    pytest.importorskip("paho.mqtt.client")
    pytest.importorskip("yaml")

    path = Path(__file__).resolve().parents[2] / "tools" / "mqtt-simulator.py"
    spec = importlib.util.spec_from_file_location("mqtt_simulator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, not after: the module defines a dataclass, and
    # ``dataclasses`` resolves field annotations through ``sys.modules[cls.__module__]``.
    # Leave it out and the import dies with an unrelated AttributeError.
    sys.modules["mqtt_simulator"] = module
    spec.loader.exec_module(module)
    return module


def test_simulated_samples_carry_their_own_distinct_timestamps() -> None:
    """
    Regression: the simulator sent one envelope timestamp for the whole batch.

    Every sample inherited it, so 25 rows landed on a single instant. Firmware does not do
    that, and it made simulated data uncomparable with real data -- the archive read as a node
    bursting 25 readings at one moment instead of sampling at a steady rate, which is exactly
    the false conclusion this test prevents.
    """
    times = _simulator().sample_times(1_791_636_144_143, 25, 5.0)

    assert len(times) == 25
    assert len(set(times)) == 25, "every sample needs its own instant"
    assert times == sorted(times), "a batch must be in time order"
    assert times[-1] == 1_791_636_144_143, "newest sample is the envelope's own instant"

    gaps = {b - a for a, b in zip(times, times[1:])}
    assert gaps == {200}, f"5 Hz means 200 ms spacing, got {sorted(gaps)}"


def test_simulated_sample_spacing_follows_the_rate() -> None:
    """0.2 Hz is the od_sensor's real cadence, and the simulator must express it."""
    times = _simulator().sample_times(1_000_000, 5, 0.2)

    assert {b - a for a, b in zip(times, times[1:])} == {5000}


def test_simulated_batch_projects_to_distinct_archive_timestamps(schema) -> None:
    """
    The outcome the fix exists for: a simulator batch no longer collapses in the archive.

    This is the join between the tool and the writer -- ``sample_times`` stamps each sample and
    ``rows_from_samples`` prefers that stamp to the envelope's.
    """
    base = 1_791_636_144_143
    samples = [{**_sample(), "t_dev_ms": t} for t in _simulator().sample_times(base, 25, 5.0)]

    stamps = [row["t_dev_ms"] for row in rows_from_samples(schema, samples, t_dev_ms=base)]

    assert len(set(stamps)) == 25
    assert stamps[-1] == base
