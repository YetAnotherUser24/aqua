"""
Export tests.

The alignment logic is where a plausible-looking mistake is most dangerous. A wide join
that carries the last value forward *without a bound* produces a dataset where a node
that died at 14:00 still reads 420 rpm at 17:00 — and an analysis built on that would
conclude the aerator ran all afternoon. So the staleness bound gets explicit tests, and
the empty-cell contract is asserted directly:

    an empty cell means no reading was CURRENT, never a reading of zero.
"""

from __future__ import annotations

import csv
import gzip
from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest

from aqua_gateway.export import bundle, flat
from aqua_gateway.export.bundle import (
    _build_wide,
    _coverage,
    build_grid,
    effective_ms,
    read_archive_csv,
    resample_locf,
    role_files,
    wide_column,
)
from aqua_gateway.schema.registry import load_registry

MINUTE_MS = 1_791_547_200_000  # 2026-10-09T12:00:00Z


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def row(t_dev_ms: int, **values) -> dict:
    return {"t_dev_ms": t_dev_ms, **values}


# ===========================================================================
# Time handling
# ===========================================================================


class TestEffectiveMs:
    def test_applies_offset(self):
        assert effective_ms({"t_dev_ms": "1000"}, 250) == 1250

    def test_accepts_float_strings(self):
        """The archive holds raw strings; the export's job is alignment, not coercion."""
        assert effective_ms({"t_dev_ms": "1791547200000.0"}, 0) == MINUTE_MS

    def test_missing_or_junk_returns_none(self):
        assert effective_ms({}, 0) is None
        assert effective_ms({"t_dev_ms": ""}, 0) is None
        assert effective_ms({"t_dev_ms": "not-a-number"}, 0) is None
        assert effective_ms({"t_dev_ms": "NaN"}, 0) is None


class TestBuildGrid:
    def test_inclusive_endpoints(self):
        start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 9, 12, 0, 3, tzinfo=timezone.utc)

        grid = build_grid(start, end, 1.0)

        assert len(grid) == 4, "start, +1, +2, end"
        assert grid[0] == MINUTE_MS
        assert grid[-1] == MINUTE_MS + 3000

    def test_sub_second_step(self):
        start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        grid = build_grid(start, start.replace(microsecond=800_000), 0.2)

        assert len(grid) == 5

    def test_rejects_non_positive_step(self):
        start = datetime(2026, 10, 9, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="positive"):
            build_grid(start, start, 0)

    def test_backwards_range_is_empty(self):
        start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 9, 11, 0, tzinfo=timezone.utc)

        assert build_grid(start, end, 1.0) == []


# ===========================================================================
# Resampling
# ===========================================================================


class TestResampleLocf:
    def test_carries_the_last_value_forward(self):
        rows = [row(MINUTE_MS, drv_rpm=400.0), row(MINUTE_MS + 2000, drv_rpm=410.0)]
        grid = [MINUTE_MS, MINUTE_MS + 1000, MINUTE_MS + 2000, MINUTE_MS + 3000]

        out = resample_locf(rows, grid, max_hold_ms=10_000)

        assert [entry["drv_rpm"] for entry in out] == [400.0, 400.0, 410.0, 410.0]

    def test_value_expires_past_the_hold_window(self):
        """
        The property that keeps the dataset honest. A node that stopped reporting must
        produce gaps, not an endless constant.
        """
        rows = [row(MINUTE_MS, drv_rpm=400.0)]
        grid = [MINUTE_MS, MINUTE_MS + 5_000, MINUTE_MS + 60_000]

        out = resample_locf(rows, grid, max_hold_ms=10_000)

        assert out[0] is not None
        assert out[1] is not None
        assert out[2] is None, "60 s of nothing must not still read 400 rpm"

    def test_grid_before_first_sample_is_empty(self):
        rows = [row(MINUTE_MS + 10_000, drv_rpm=400.0)]
        grid = [MINUTE_MS, MINUTE_MS + 10_000]

        out = resample_locf(rows, grid, max_hold_ms=60_000)

        assert out[0] is None
        assert out[1] is not None

    def test_clock_offset_shifts_alignment(self):
        """A node whose clock ran 5 s fast must line up after correction."""
        rows = [row(MINUTE_MS + 5_000, drv_rpm=400.0)]
        grid = [MINUTE_MS, MINUTE_MS + 5_000]

        uncorrected = resample_locf(rows, grid, max_hold_ms=60_000)
        corrected = resample_locf(rows, grid, offset_ms=-5_000, max_hold_ms=60_000)

        assert uncorrected[0] is None, "arrives late without the correction"
        assert corrected[0] is not None, "lines up once the offset is applied"

    def test_unsorted_input_is_handled(self):
        rows = [row(MINUTE_MS + 2000, drv_rpm=410.0), row(MINUTE_MS, drv_rpm=400.0)]
        grid = [MINUTE_MS, MINUTE_MS + 1000, MINUTE_MS + 2000]

        out = resample_locf(rows, grid, max_hold_ms=60_000)

        assert [entry["drv_rpm"] for entry in out] == [400.0, 400.0, 410.0]

    def test_rows_without_timestamps_are_skipped(self):
        rows = [{"drv_rpm": 999.0}, row(MINUTE_MS, drv_rpm=400.0)]
        grid = [MINUTE_MS]

        out = resample_locf(rows, grid, max_hold_ms=60_000)

        assert out[0]["drv_rpm"] == 400.0

    def test_empty_inputs(self):
        assert resample_locf([], [MINUTE_MS]) == [None]
        assert resample_locf([row(MINUTE_MS)], []) == []

    def test_handles_a_large_grid_efficiently(self):
        """
        A three-hour experiment at 1 s resolution is 10,800 grid points. The two-pointer
        merge is O(n + m); a naive per-point scan would be O(n x m) and never finish.
        """
        # Three hours of 5 Hz samples against a 1 s grid: 54,000 rows, 10,800 points.
        rows = [row(MINUTE_MS + index * 200, drv_rpm=float(index)) for index in range(0, 3 * 3600 * 5)]
        grid = list(range(MINUTE_MS, MINUTE_MS + 3 * 3_600_000, 1000))

        out = resample_locf(rows, grid, max_hold_ms=1_000)

        assert len(out) == len(grid)
        assert all(entry is not None for entry in out)


# ===========================================================================
# Column naming
# ===========================================================================


class TestWideColumn:
    def test_authoritative_role_columns_stay_bare(self):
        assert wide_column("od_sensor", "od_do_mg_l", "od_sensor") == "od_do_mg_l"

    def test_other_roles_are_prefixed(self):
        assert wide_column("aerator", "drv_rpm", "od_sensor") == "aerator_drv_rpm"

    def test_no_authoritative_role_means_everything_is_prefixed(self):
        assert wide_column("od_sensor", "od_do_mg_l", None) == "od_sensor_od_do_mg_l"

    def test_the_echo_is_never_bare(self):
        """
        `peer_do_mg_l` on the aerator is that node's own control-loop input. It must not
        be mistakable for the authoritative reading, so it is always prefixed.
        """
        assert wide_column("aerator", "peer_do_mg_l", "od_sensor") == "aerator_peer_do_mg_l"


class TestBuildWide:
    def test_header_names_and_order(self, registry):
        series = {
            "od_sensor": [row(MINUTE_MS, od_do_mg_l=7.5, od_temp_c=23.0)],
            "aerator": [row(MINUTE_MS, drv_rpm=400.0)],
        }
        grid = [MINUTE_MS]

        header, rows = _build_wide(registry, series, grid, {}, max_hold_ms=60_000)

        assert header[:2] == ["grid_ts", "timestamp_utc"]
        assert "od_do_mg_l" in header, "authoritative role stays unprefixed"
        assert "aerator_drv_rpm" in header
        assert "aerator_peer_do_mg_l" in header
        assert len(header) == 2 + len(rows[0]) - 2

    def test_row_width_matches_header(self, registry):
        series = {
            "od_sensor": [row(MINUTE_MS, od_do_mg_l=7.5)],
            "pump": [row(MINUTE_MS, pump_dosed_ml=10.0)],
        }

        header, rows = _build_wide(registry, series, [MINUTE_MS], {}, max_hold_ms=60_000)

        assert all(len(cells) == len(header) for cells in rows)

    def test_gaps_are_empty_strings_not_zeros(self, registry):
        """
        The contract that keeps a missing reading distinguishable from a real zero.
        """
        series = {"od_sensor": [row(MINUTE_MS, od_do_mg_l=7.5)]}
        grid = [MINUTE_MS, MINUTE_MS + 300_000]  # second point is long after the data

        header, rows = _build_wide(registry, series, grid, {}, max_hold_ms=1_000)

        do_index = header.index("od_do_mg_l")
        assert rows[0][do_index] == 7.5
        assert rows[1][do_index] == "", "a stale value must become an empty cell"

    def test_offset_is_applied_per_role(self, registry):
        series = {"aerator": [row(MINUTE_MS + 5_000, drv_rpm=400.0)]}
        grid = [MINUTE_MS]

        _, without = _build_wide(registry, series, grid, {}, max_hold_ms=60_000)
        _, with_offset = _build_wide(registry, series, grid, {"aerator": -5_000}, max_hold_ms=60_000)

        rpm_index = 2  # first field of the only role
        assert without[0][rpm_index] == ""
        assert with_offset[0][rpm_index] == 400.0

    def test_deterministic_across_runs(self, registry):
        series = {
            "aerator": [row(MINUTE_MS, drv_rpm=400.0)],
            "od_sensor": [row(MINUTE_MS, od_do_mg_l=7.5)],
        }

        first = _build_wide(registry, series, [MINUTE_MS], {}, max_hold_ms=60_000)
        second = _build_wide(registry, series, [MINUTE_MS], {}, max_hold_ms=60_000)

        assert first == second


# ===========================================================================
# Reading the archive
# ===========================================================================


class TestReadArchiveCsv:
    def test_reads_a_plain_csv(self, tmp_path: Path):
        path = tmp_path / "2026-10-09.csv"
        path.write_text("t_dev_ms,drv_rpm\n100,400\n200,410\n")

        rows = read_archive_csv(path)

        assert len(rows) == 2
        assert rows[0]["drv_rpm"] == "400"

    def test_reads_a_gzipped_csv(self, tmp_path: Path):
        path = tmp_path / "2026-10-09.csv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
            handle.write("t_dev_ms,drv_rpm\n100,400\n")

        rows = read_archive_csv(path)

        assert rows == [{"t_dev_ms": "100", "drv_rpm": "400"}]

    def test_missing_file_is_empty_not_an_error(self, tmp_path: Path):
        assert read_archive_csv(tmp_path / "nope.csv") == []


class TestRoleFiles:
    def test_prefers_the_compressed_form(self, tmp_path: Path):
        """
        Rotation keeps the .gz and deletes the .csv; when both exist mid-rotation, the
        compressed one is the one the archive is keyed to.
        """
        role_dir = tmp_path / "aerator"
        role_dir.mkdir()
        (role_dir / "2026-10-09.csv").write_text("t\n1\n")
        (role_dir / "2026-10-09.csv.gz").write_bytes(b"")

        start = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)

        files = role_files(tmp_path, "aerator", start, end)

        assert files == [role_dir / "2026-10-09.csv.gz"]

    def test_window_is_respected(self, tmp_path: Path):
        """
        The window spans the file-days it covers *in the archive timezone*.

        UTC 2026-10-08T00:00 → 2026-10-09T00:00 is Lima 2026-10-07T19:00 → 2026-10-08T19:00,
        so it touches the 07 and 08 files. Only the 09 and 10 files are outside it.
        """
        role_dir = tmp_path / "aerator"
        role_dir.mkdir()
        for day in ("2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"):
            (role_dir / f"{day}.csv").write_text("t\n1\n")

        files = role_files(
            tmp_path,
            "aerator",
            datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc),
        )

        assert [path.name for path in files] == ["2026-10-07.csv", "2026-10-08.csv"]

    def test_unknown_role_directory_is_empty(self, tmp_path: Path):
        start = datetime(2026, 10, 9, tzinfo=timezone.utc)

        assert role_files(tmp_path, "nope", start, start) == []

    def test_day_boundary_follows_the_archive_timezone_not_utc(self, tmp_path: Path):
        """
        Archive files are named by the day of the *device* timestamps they hold, which are
        filed in the archive timezone. An experiment that starts at 01:44 UTC is 20:44 the
        previous day in Lima, so its data lives in the earlier file.

        The regression: `role_files` took `start.date()` in whatever zone the caller had —
        UTC from `experiments.started_at` — so for any window starting between 19:00 and
        midnight Lima it looked for the *next* day's file and reported "no telemetry found"
        for data that was plainly on disk.
        """
        role_dir = tmp_path / "od_sensor"
        role_dir.mkdir()
        # The window starts 2026-10-11T01:44Z, but the file holding that data is the 10th.
        (role_dir / "2026-10-10.csv").write_text("t\n1\n")
        (role_dir / "2026-10-11.csv").write_text("t\n1\n")

        files = role_files(
            tmp_path,
            "od_sensor",
            datetime(2026, 10, 11, 1, 44, tzinfo=timezone.utc),
            datetime(2026, 10, 11, 1, 45, tzinfo=timezone.utc),
            "America/Lima",
        )

        assert [path.name for path in files] == ["2026-10-10.csv"]


# ===========================================================================
# Coverage reporting
# ===========================================================================


class TestCoverage:
    """
    `_coverage` feeds the export manifest, whose whole job is to make a shortfall visible.

    It reported rates that were wrong by up to 300x because it divided the sample count by the
    experiment *window* while the rows it counted spanned the whole day's file. On a real export
    the aerator came out at 149 Hz against a declared 5, and a 0.2 Hz sensor at 5.77. Nothing
    caught it: the number was only ever looked at by a human reading the manifest, and a wrong
    rate reads as a property of the deployment rather than a bug in the arithmetic.
    """

    def test_rate_uses_the_sample_span_not_the_window(self, registry):
        """
        A role whose rows cover an hour must report its true rate for a five-minute window.

        This is the regression in one assertion: 3600 rows over 3600 s is 1 Hz whatever the
        window is. Dividing by a 300 s window would report 12 Hz for the same data.
        """
        start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 9, 12, 5, tzinfo=timezone.utc)  # 300 s window
        # One row per second for an hour, well past the window's end.
        rows = [row(MINUTE_MS + i * 1000, od_do_mg_l=7.0) for i in range(3600)]

        cov = _coverage(registry, {"od_sensor": rows}, start, end)

        assert cov["od_sensor"]["samples"] == 3600
        assert cov["od_sensor"]["observed_hz"] == pytest.approx(1.0, rel=1e-3)

    def test_rate_is_none_rather_than_infinite_for_a_single_sample(self, registry):
        """
        One sample has no span, so there is no rate to report.

        Dividing by a zero-length span raises; clamping the span to 0 and dividing anyway
        produces a number that looks like a measurement. `None` is the honest answer, and it is
        what the manifest should carry so a reader can tell "not measurable" from "very fast".
        """
        start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 9, 12, 5, tzinfo=timezone.utc)

        cov = _coverage(registry, {"od_sensor": [row(MINUTE_MS, od_do_mg_l=7.0)]}, start, end)

        assert cov["od_sensor"]["samples"] == 1
        assert cov["od_sensor"]["observed_hz"] is None

    def test_truncated_compares_coverage_against_the_window(self, registry):
        """
        `truncated` answers "does this role span the window?", so the window is its denominator.

        This is the one place the window is the right baseline, and it is the same expression
        that was wrong in `observed_hz`. Rows covering 30 s of a 300 s window are truncated;
        rows covering 300 s are not.
        """
        start = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 9, 12, 5, tzinfo=timezone.utc)  # 300 s window

        short = [row(MINUTE_MS + i * 1000, od_do_mg_l=7.0) for i in range(30)]
        full = [row(MINUTE_MS + i * 1000, od_do_mg_l=7.0) for i in range(300)]

        cov_short = _coverage(registry, {"od_sensor": short}, start, end)
        cov_full = _coverage(registry, {"od_sensor": full}, start, end)

        assert cov_short["od_sensor"]["truncated"] is True
        assert cov_full["od_sensor"]["truncated"] is False


# ---------------------------------------------------------------------------
# Event selection — which rows belong to an experiment
# ---------------------------------------------------------------------------


class _RecordingDB:
    """A database that answers nothing but remembers exactly what it was asked."""

    available = True

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[tuple] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        self.params.append(args)
        return []


@pytest.mark.asyncio
async def test_events_are_selected_by_experiment_not_by_time_window():
    """
    An event belongs to a run because of *which* run it is, not when it happened.

    Selecting by `[started_at, ended_at]` was wrong in both directions at once, measured on
    the Pi's own data for `EXP-2026-10-11-02`: of 21 events belonging to that experiment, a
    windowed query returned 20 -- dropping 5 of the experiment's own events that fired after
    `ended_at`, and admitting 4 that were not its (three with a null `experiment_id`, one from
    a different experiment whose window overlapped). A run's own late events are not noise:
    `command_expired` fires ~79 s after the run ends and is the only record that the commands
    issued in its final second never executed.

    This asserts the query, because that is where the defect was: the parameter must be the
    experiment id, and no timestamp range may appear.
    """
    db = _RecordingDB()
    await flat._load_events(db, "EXP-2026-10-11-02")

    assert len(db.queries) == 1, "expected exactly one event query"
    sql = " ".join(db.queries[0].split()).lower()
    assert db.params[0] == ("EXP-2026-10-11-02",), "the experiment id must be what is bound"
    assert "experiment_id = $1" in sql, "events must be selected by experiment identity"
    assert "between" not in sql, "a time window loses the run's own late events"
    assert "ts >=" not in sql and "ts <=" not in sql, "no timestamp bound may survive"


@pytest.mark.asyncio
async def test_the_aligned_bundle_selects_events_the_same_way():
    """
    Both exporters read events, and both carried the same window bug.

    The native export and the aligned bundle are different shapes of the same data, so a
    reader that trusts one and not the other would get two different event sets for one run.
    Pinned separately because fixing one call site and missing the sibling is the exact shape
    of the mistake that produced this.
    """
    db = _RecordingDB()
    await bundle.load_events(db, "EXP-2026-10-11-02")

    sql = " ".join(db.queries[0].split()).lower()
    assert db.params[0] == ("EXP-2026-10-11-02",)
    assert "experiment_id = $1" in sql
    assert "between" not in sql


@pytest.mark.asyncio
async def test_an_offline_database_yields_no_events_rather_than_raising():
    """An export is a read of what exists; a missing database is no events, not a failure."""

    class Offline:
        available = False

    assert await flat._load_events(Offline(), "EXP-1") == []
    assert await bundle.load_events(Offline(), "EXP-1") == []


def test_the_manifest_documents_the_row_order_and_the_empty_cells(tmp_path: Path):
    """
    The manifest must say the table is wide, sorted by t_dev_ms, and not filled in.

    Both halves are promises a reader relies on and neither is visible from the data alone:
    sorting is what lets `df` be used without a re-sort, and the empty cells are the
    difference between a real reading and one that was carried forward. Someone who assumes
    the cells were filled will read a slow node's columns as continuous when they are sparse,
    and conclude the sensor sampled faster than it did.

    A note is easy to drop in a refactor and its absence is invisible until an analysis is
    built on the wrong assumption, so it is pinned here.
    """
    from aqua_gateway.export.flat import FlatReport, write_manifest

    report = FlatReport(experiment_id="EXP-1", out_dir=tmp_path)
    report.rows = 2
    report.columns = ["role", "t_dev_ms"]
    report.events = 0
    report.roles_present = ["od_sensor", "aerator"]

    started = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    path = tmp_path / "manifest.json"
    write_manifest(
        path,
        report=report,
        experiment={"title": "t", "status": "completed"},
        registry=load_registry(),
        started=started,
        ended=started,
        t0_ms=MINUTE_MS,
    )

    import json

    notes = " ".join(json.loads(path.read_text())["notes"]).lower()
    assert "one row per *instant*" in notes, "the manifest must state the row grain"
    assert "sorted ascending" in notes, "and that the file is in time order"
    assert "no value is carried forward" in notes, "and that empty cells mean no reading"
    assert "every non-empty cell is a real reading" in notes, "which is the reason it matters"
    # The merge is the least guessable part of the format: a reader who does not know two
    # roles can share a row will misread the `a+b` in `role` and the `merge_spread_ms` column,
    # and a reader who does not know the window is bounded under the fastest interval cannot
    # tell whether a sample went missing.
    assert "merged within" in notes, "the manifest must explain the instant merge"
    assert "merge_spread_ms" in notes, "and name the column that reports it"
    assert "never collapse" in notes, "and that the window cannot merge one role's samples"


# ---------------------------------------------------------------------------
# Wide layout — one row per instant, fields as columns
# ---------------------------------------------------------------------------


def _archive(tmp_path: Path, role: str, rows: list[dict[str, str]]) -> Path:
    """Write one per-role archive file in the shape `_read_role_rows` expects."""
    directory = tmp_path / role
    directory.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    path = directory / "2026-10-09.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_wide_export_puts_both_roles_on_one_row_per_instant(tmp_path: Path):
    """
    The property the long format could not give: DO and RPM readable on the same line.

    Two roles reporting at overlapping instants must land on shared rows, not in separate
    blocks. This is the whole point of the change -- in the long format an operator filtering
    on `role == 'od_sensor'` never saw `drv_rpm` at all, because that column was empty on
    every row they had.

    The rows also have to come out sorted by `t_dev_ms`. The archive is not guaranteed sorted
    (the 131k-row aerator file for 2026-10-10 has 3 out-of-order stamps), so this feeds one
    deliberately out of order and requires the output to be ordered anyway.
    """
    # The sensor reports at 0 and 2000 ms; the aerator at 0, 1000 and 2000. In the archive the
    # aerator's 1000 ms row is written last, to prove the writer sorts rather than appends.
    _archive(
        tmp_path,
        "od_sensor",
        [
            {
                "role": "od_sensor",
                "t_dev_ms": str(MINUTE_MS),
                "t_rel_ms": "-1",
                "t_src": "rtc",
                "seq": "1",
                "od_do_mg_l": "7.4",
            },
            {
                "role": "od_sensor",
                "t_dev_ms": str(MINUTE_MS + 2000),
                "t_rel_ms": "-1",
                "t_src": "rtc",
                "seq": "2",
                "od_do_mg_l": "7.3",
            },
        ],
    )
    _archive(
        tmp_path,
        "aerator",
        [
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "1",
                "drv_rpm": "0",
            },
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS + 2000),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "3",
                "drv_rpm": "240",
            },
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS + 1000),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "2",
                "drv_rpm": "120",
            },
        ],
    )

    out = tmp_path / "wide.csv"
    registry = load_registry()
    columns = ["role", "t_dev_ms", "t_rel_ms", "t_src", "seq", "od_do_mg_l", "drv_rpm"]
    per_role, missing, total = flat._write_wide_csv(
        out,
        root=tmp_path,
        roles=["od_sensor", "aerator"],
        registry=registry,
        columns=columns,
        started=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        ended=datetime(2026, 10, 9, 13, 0, tzinfo=UTC),
        t0_ms=MINUTE_MS,
    )

    assert missing == [], "both roles have rows"
    # 3 distinct instants: 0, 1000, 2000. Not 5 (the sum of samples) -- that was the long
    # format's count and reporting it here would overstate the file.
    assert total == 3, "one row per distinct t_dev_ms"
    assert per_role == {"od_sensor": 2, "aerator": 3}, "per-role sample counts are kept separately"

    rows = list(csv.DictReader(out.open()))
    assert [int(r["t_dev_ms"]) for r in rows] == [MINUTE_MS, MINUTE_MS + 1000, MINUTE_MS + 2000], (
        "rows must be sorted ascending, though the archive was not"
    )
    # The whole feature, asserted: one row carries both roles' fields.
    assert rows[0]["od_do_mg_l"] == "7.4" and rows[0]["drv_rpm"] == "0", (
        "the instants both roles reported must share a row"
    )
    assert rows[1]["drv_rpm"] == "120" and not rows[1]["od_do_mg_l"], (
        "an instant only the aerator reported leaves the sensor column EMPTY, not repeated"
    )
    assert rows[2]["od_do_mg_l"] == "7.3" and rows[2]["drv_rpm"] == "240"


def test_a_value_is_never_carried_into_the_next_row(tmp_path: Path):
    """
    Empty means "no reading", never "same as last time".

    This is the difference between this export and the aligned bundle, and the reason it is
    safe to build an analysis on: a held value is a reading that was never taken. The failure
    it prevents is specific -- a node that dies at 14:00 would otherwise appear to hold its
    last setpoint until the end of the run.
    """
    _archive(
        tmp_path,
        "od_sensor",
        [
            {
                "role": "od_sensor",
                "t_dev_ms": str(MINUTE_MS),
                "t_rel_ms": "-1",
                "t_src": "rtc",
                "seq": "1",
                "od_do_mg_l": "7.4",
            }
        ],
    )
    _archive(
        tmp_path,
        "aerator",
        [
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "1",
                "drv_rpm": "240",
            },
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS + 1000),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "2",
                "drv_rpm": "240",
            },
        ],
    )

    out = tmp_path / "wide.csv"
    flat._write_wide_csv(
        out,
        root=tmp_path,
        roles=["od_sensor", "aerator"],
        registry=load_registry(),
        columns=["role", "t_dev_ms", "t_rel_ms", "t_src", "seq", "od_do_mg_l", "drv_rpm"],
        started=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        ended=datetime(2026, 10, 9, 13, 0, tzinfo=UTC),
        t0_ms=MINUTE_MS,
    )

    rows = list(csv.DictReader(out.open()))
    assert rows[1]["drv_rpm"] == "240", "the aerator's own reading is there"
    assert rows[1]["od_do_mg_l"] == "", (
        "the sensor did not report at this instant, so its cell must be empty -- filling it would invent a reading"
    )


def test_a_merged_row_reports_its_spread_even_when_it_is_zero(tmp_path: Path):
    """
    A row that combined two samples must be tellable from one that did not.

    Two roles reporting the *same* millisecond give a spread of 0. The first version of this
    wrote the column only `if spread`, so those rows -- the majority, since the nodes'
    timestamps coincide exactly far more often than they drift -- came out with an empty
    `merge_spread_ms` and looked identical to a single-role row. The column exists to say
    "this row is a merge", and an empty cell has to keep meaning "it is not".

    So `0` is a measurement, not a missing value.
    """
    _archive(
        tmp_path,
        "od_sensor",
        [
            {
                "role": "od_sensor",
                "t_dev_ms": str(MINUTE_MS),
                "t_rel_ms": "-1",
                "t_src": "rtc",
                "seq": "1",
                "od_do_mg_l": "7.4",
            }
        ],
    )
    _archive(
        tmp_path,
        "aerator",
        [
            # Same millisecond as the sensor: merges with spread 0.
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "1",
                "drv_rpm": "240",
            },
            # 40 ms later: inside the 150 ms window of the *next* group, and alone in it.
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS + 5000),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "2",
                "drv_rpm": "241",
            },
        ],
    )

    out = tmp_path / "wide.csv"
    flat._write_wide_csv(
        out,
        root=tmp_path,
        roles=["od_sensor", "aerator"],
        registry=load_registry(),
        columns=["role", "t_dev_ms", "t_rel_ms", "t_src", "seq", "merge_spread_ms", "od_do_mg_l", "drv_rpm"],
        started=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        ended=datetime(2026, 10, 9, 13, 0, tzinfo=UTC),
        t0_ms=MINUTE_MS,
    )

    rows = list(csv.DictReader(out.open()))
    assert rows[0]["role"] == "od_sensor+aerator"
    assert rows[0]["merge_spread_ms"] == "0", (
        "a merged row whose stamps were identical must report 0, not leave the cell empty"
    )
    assert rows[1]["role"] == "aerator"
    assert rows[1]["merge_spread_ms"] == "", (
        "a row that merged nothing leaves the cell empty -- that is how the two are told apart"
    )


def test_the_merge_window_stays_under_the_fastest_role_interval(tmp_path: Path):
    """
    The window must not be wide enough to swallow two of the fastest role's own samples.

    This is the guard on `DEFAULT_MERGE_MS`. The aerator reports every 200 ms; a window of
    200 ms or more would put two of its consecutive readings on one row and keep only the
    first, silently deleting data at the cadence that matters most. Raising the constant is a
    data-loss bug, not a tuning choice, so it is asserted against the registry's own declared
    rate rather than a hardcoded 200.
    """
    registry = load_registry()
    fastest_period_ms = min(
        1000.0 / schema.expected_rate_hz.get("experiment", 1.0)
        for schema in registry.roles.values()
        if schema.expected_rate_hz.get("experiment")
    )
    assert flat.DEFAULT_MERGE_MS < fastest_period_ms, (
        f"merge window {flat.DEFAULT_MERGE_MS} ms must stay below the fastest role's "
        f"{fastest_period_ms:.0f} ms interval, or it merges distinct samples"
    )

    # And prove it with data: two aerator samples one period apart must stay on two rows.
    _archive(
        tmp_path,
        "aerator",
        [
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "1",
                "drv_rpm": "200",
            },
            {
                "role": "aerator",
                "t_dev_ms": str(MINUTE_MS + int(fastest_period_ms)),
                "t_rel_ms": "-1",
                "t_src": "espnow",
                "seq": "2",
                "drv_rpm": "240",
            },
        ],
    )
    out = tmp_path / "wide.csv"
    _, _, total = flat._write_wide_csv(
        out,
        root=tmp_path,
        roles=["aerator"],
        registry=registry,
        columns=["role", "t_dev_ms", "t_rel_ms", "t_src", "seq", "merge_spread_ms", "drv_rpm"],
        started=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        ended=datetime(2026, 10, 9, 13, 0, tzinfo=UTC),
        t0_ms=MINUTE_MS,
    )
    assert total == 2, "two consecutive samples of one role must never merge"
    rpms = [r["drv_rpm"] for r in csv.DictReader(out.open())]
    assert rpms == ["200", "240"], "and neither reading may be dropped"


@pytest.mark.asyncio
async def test_undeclared_role_with_telemetry_is_included_in_export(tmp_path: Path, registry):
    """
    If an experiment's recipe declared only od_sensor and pump, but the aerator was run,
    its telemetry must NOT be discarded from the native export or aligned bundle.
    """
    class _Settings:
        def __init__(self, root: Path):
            self.archive = type("Archive", (), {"root": root})()
            self.timezone = "UTC"

    class _MockDB:
        available = True

        async def fetchrow(self, query, *args):
            return {
                "id": "EXP-2026-10-09-01",
                "title": "Deox only declared",
                "status": "completed",
                # aerator is NOT declared here:
                "roles": ["od_sensor", "pump"],
                "started_at": datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
                "ended_at": datetime(2026, 10, 9, 12, 5, tzinfo=UTC),
                "calibration": {},
            }

        async def fetch(self, query, *args):
            return []

    root = tmp_path / "telemetry"
    # Write sensor data
    _archive(
        root,
        "od_sensor",
        [{"role": "od_sensor", "t_dev_ms": str(MINUTE_MS), "t_rel_ms": "0", "t_src": "rtc", "seq": "1", "od_do_mg_l": "7.0"}],
    )
    # Write aerator data even though it was not declared
    _archive(
        root,
        "aerator",
        [{"role": "aerator", "t_dev_ms": str(MINUTE_MS + 200), "t_rel_ms": "200", "t_src": "espnow", "seq": "1", "drv_rpm": "300"}],
    )

    out_native = tmp_path / "out_native"
    report = await flat.build_flat_export(
        _Settings(root),
        manifest=None,
        db=_MockDB(),
        registry=registry,
        experiment_id="EXP-2026-10-09-01",
        out_dir=out_native,
        compress=False,
    )

    assert "aerator" in report.roles_present, "aerator must be included because it had telemetry"
    assert report.per_role_rows["aerator"] == 1
    assert "pump" in report.roles_missing, "declared role without telemetry is noted missing"

    with (out_native / "telemetry_native.csv").open() as fh:
        reader = list(csv.DictReader(fh))
    assert len(reader) == 2
    assert any(r["drv_rpm"] == "300" for r in reader), "aerator columns and data must be present"

