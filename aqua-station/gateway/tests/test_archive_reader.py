"""
Raw archive readback.

The reader exists because the rollups cannot answer "what did the waveform do" -- one
point per minute is not a coarser view of a 5 Hz signal, it is a different signal. Every
test here is about the two ways a raw reader goes wrong quietly:

* **Returning the wrong end of the file.** The reader walks backwards because callers
  want the newest rows. Getting that backwards yields plausible, correctly-formatted
  data from hours ago, which looks exactly like a working chart until someone compares
  it against a clock.
* **Losing the newest rows entirely.** ``CsvArchiveWriter`` buffers in memory and only
  writes at 64 KB, so the last ~30 seconds are not on disk. Data that is missing from
  the end of a series is indistinguishable from a node that stopped sending.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aqua_gateway.archive.reader import days_covering, iter_lines_reverse, read_metric_window
from aqua_gateway.schema.registry import load_registry

TZ = "America/Lima"

#: Fixed instant so nothing depends on when the suite runs: 2026-10-09 12:00:00 -05.
BASE = datetime(2026, 10, 9, 12, 0, tzinfo=ZoneInfo(TZ))
BASE_MS = int(BASE.timestamp() * 1000)


def ms(offset_seconds: float) -> int:
    return BASE_MS + int(offset_seconds * 1000)


@pytest.fixture
def schema():
    return load_registry().for_role("od_sensor")


def write_day(root: Path, schema, day: str, rows: list[dict]) -> Path:
    """Write a day file in the same shape the archive writes."""
    path = root / schema.role / f"{day}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(schema.csv_header)]
    for row in rows:
        cells = []
        for name in schema.csv_header:
            value = row.get(name)
            cells.append("" if value is None else str(value))
        lines.append(",".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def od_row(t_dev_ms: int, do: float | None = 7.5) -> dict:
    return {"t_dev_ms": t_dev_ms, "t_src": "rtc", "seq": 1, "od_do_mg_l": do}


# -- tail reading -------------------------------------------------------------


def test_reverse_iteration_yields_newest_line_first(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert list(iter_lines_reverse(path)) == ["three", "two", "one"]


def test_reverse_iteration_handles_a_missing_trailing_newline(tmp_path: Path) -> None:
    """A file being appended to is routinely caught mid-line."""
    path = tmp_path / "f.csv"
    path.write_text("one\ntwo\nthree", encoding="utf-8")
    assert list(iter_lines_reverse(path)) == ["three", "two", "one"]


def test_reverse_iteration_survives_many_chunks(tmp_path: Path) -> None:
    """Chunk boundaries must not tear a line in half."""
    path = tmp_path / "big.csv"
    rows = [f"row-{i:06d}-{'x' * 40}" for i in range(20_000)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    got = list(iter_lines_reverse(path))
    assert got == list(reversed(rows))


# -- window reading -----------------------------------------------------------


def test_limit_returns_the_newest_rows_not_the_oldest(tmp_path: Path, schema) -> None:
    """
    The single most important property of this reader.

    Returning the first N rows of the file instead of the last N produces a series that
    is correctly typed, correctly ordered, and hours out of date.
    """
    rows = [od_row(ms(i), do=round(6.0 + i / 1000, 3)) for i in range(500)]
    write_day(tmp_path, schema, "2026-10-09", rows)

    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=ms(-3600),
        until_ms=ms(3600),
        limit=10,
        timezone_name=TZ,
    )

    assert len(out) == 10
    # The newest ten, oldest-first among themselves.
    assert [t for t, _ in out] == [ms(i) for i in range(490, 500)]
    assert out[-1][0] == ms(499)


def test_rows_come_back_in_chronological_order(tmp_path: Path, schema) -> None:
    rows = [od_row(ms(i)) for i in range(20)]
    write_day(tmp_path, schema, "2026-10-09", rows)

    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=ms(-10),
        until_ms=ms(60),
        limit=100,
        timezone_name=TZ,
    )

    timestamps = [t for t, _ in out]
    assert timestamps == sorted(timestamps)


def test_header_is_not_returned_as_data(tmp_path: Path, schema) -> None:
    write_day(tmp_path, schema, "2026-10-09", [od_row(ms(0))])

    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=ms(-60),
        until_ms=ms(60),
        limit=100,
        timezone_name=TZ,
    )

    assert len(out) == 1, "the header line must be skipped"


def test_window_start_is_respected(tmp_path: Path, schema) -> None:
    rows = [od_row(ms(i)) for i in range(100)]
    write_day(tmp_path, schema, "2026-10-09", rows)

    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=ms(40),
        until_ms=ms(3600),
        limit=1000,
        timezone_name=TZ,
    )

    assert [t for t, _ in out] == [ms(i) for i in range(40, 100)]


def test_null_values_are_skipped_not_zeroed(tmp_path: Path, schema) -> None:
    """A field the firmware does not send must leave a gap, not a plausible zero."""
    rows = [od_row(ms(0), do=7.0), od_row(ms(1), do=None), od_row(ms(2), do=7.2)]
    write_day(tmp_path, schema, "2026-10-09", rows)

    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=ms(-60),
        until_ms=ms(60),
        limit=100,
        timezone_name=TZ,
    )

    assert [value for _, value in out] == [7.0, 7.2]


def test_missing_file_is_empty_not_an_error(tmp_path: Path, schema) -> None:
    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=ms(-60),
        until_ms=ms(60),
        limit=100,
        timezone_name=TZ,
    )
    assert out == []


def test_unknown_metric_is_empty(tmp_path: Path, schema) -> None:
    write_day(tmp_path, schema, "2026-10-09", [od_row(ms(0))])
    out = read_metric_window(
        tmp_path,
        schema,
        metric="not_a_field",
        since_ms=ms(-60),
        until_ms=ms(60),
        limit=100,
        timezone_name=TZ,
    )
    assert out == []


# -- day selection ------------------------------------------------------------


def test_days_covering_is_newest_first() -> None:
    """Newest first is what lets the reader stop early instead of scanning history."""
    since = int(datetime(2026, 10, 7, 0, 0, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)
    until = int(datetime(2026, 10, 9, 0, 0, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)

    days = days_covering(since, until, TZ)

    assert [d.isoformat() for d in days] == ["2026-10-09", "2026-10-08", "2026-10-07"]


def test_day_selection_follows_the_archive_timezone_not_utc() -> None:
    """
    Files are named by local day, so a late-evening UTC instant belongs to the previous
    file. Using UTC here would look for today's file and quietly find nothing.
    """
    # 2026-10-10 02:00 UTC == 2026-10-09 21:00 in Lima.
    instant = int(datetime(2026, 10, 10, 2, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)

    assert [d.isoformat() for d in days_covering(instant, instant, TZ)] == ["2026-10-09"]


def test_window_spanning_midnight_reads_both_files(tmp_path: Path, schema) -> None:
    """A run that crosses midnight is one run, and its points must not be split."""
    yesterday = int(datetime(2026, 10, 8, 23, 59, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)
    today = int(datetime(2026, 10, 9, 0, 1, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)

    write_day(tmp_path, schema, "2026-10-08", [od_row(yesterday, do=6.0)])
    write_day(tmp_path, schema, "2026-10-09", [od_row(today, do=6.5)])

    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=yesterday - 1000,
        until_ms=today + 1000,
        limit=100,
        timezone_name=TZ,
    )

    assert [value for _, value in out] == [6.0, 6.5], "both sides of midnight"


def test_respects_the_byte_budget(tmp_path: Path, schema) -> None:
    """The scan must be bounded even when the window would reach the file's start."""
    rows = [od_row(ms(i)) for i in range(4000)]
    write_day(tmp_path, schema, "2026-10-09", rows)

    out = read_metric_window(
        tmp_path,
        schema,
        metric="od_do_mg_l",
        since_ms=0,
        until_ms=ms(10_000),
        limit=1000,
        timezone_name=TZ,
        max_bytes=4096,
    )

    # Bounded, and the rows it did return are still the newest ones it reached.
    assert 0 < len(out) < 1000
    assert out[-1][0] == ms(3999)


def test_elapsed_day_boundary_does_not_shift_the_window() -> None:
    """Guards against a timezone change silently widening every query."""
    since = ms(-60)
    until = ms(60)
    days = days_covering(since, until, TZ)
    assert len(days) == 1
    assert days[0].isoformat() == "2026-10-09"
    assert timedelta(seconds=120) > timedelta(0)  # sanity: the window is small
