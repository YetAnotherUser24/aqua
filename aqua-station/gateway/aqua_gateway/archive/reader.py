"""
Reading raw samples back out of the archive.

The rollup tables answer "what happened over the last day", deliberately downsampled to
one row per minute. They cannot answer "what did the waveform do", and that is the
question anything scientific asks of this data -- a 5 Hz signal reduced to 60 points an
hour is not a coarser version of the same thing, it is a different thing.

So raw samples come from the CSVs the archive writes, which hold every sample as it
arrived.

Two details shape this module:

**Newest-first reads.** A day file at 5 Hz holds roughly 430,000 rows. Every caller wants
the newest handful and then stops, so scanning forwards from the top and filtering costs
a full parse per request -- the difference between a chart that refreshes and one that
stalls. Files are read backwards in chunks instead.

**The newest rows are not on disk yet.** ``CsvArchiveWriter`` buffers in memory and
issues one write per 64 KB, which at ~390 bytes a sample is about 34 seconds of data. A
reader that only opened files would therefore appear to lose the last half-minute and
would look, to the operator, exactly like a node that had stopped sending. The writer's
open buffers are read separately and merged; see ``buffered_rows``.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from zoneinfo import ZoneInfo

from ..schema.registry import RoleSchema

#: Bytes per backwards read. Large enough that a typical window lands in one or two
#: chunks, small enough that a 400 MB file does not need a 400 MB allocation.
TAIL_CHUNK_BYTES = 256 * 1024

#: How far back to search for the start of a window before giving up. A torn or
#: truncated file must not turn into an unbounded scan.
MAX_TAIL_BYTES = 64 * 1024 * 1024


def days_covering(since_ms: int, until_ms: int, timezone_name: str) -> list[date]:
    """
    Days whose files could hold the window, **newest first**.

    Newest first because the caller tail-reads until it has enough rows, and the newest
    day is almost always the only one it needs. Ordering oldest-first would make every
    query walk the entire history to reach today.

    Days are computed in the archive's timezone, not UTC: the files are named by the
    local day, so a window at 01:00 UTC in Lima belongs to the previous day's file.
    """
    tz = ZoneInfo(timezone_name)
    first = datetime.fromtimestamp(since_ms / 1000, tz).date()
    last = datetime.fromtimestamp(until_ms / 1000, tz).date()

    days: list[date] = []
    cursor = last
    while cursor >= first:
        days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def iter_lines_reverse(path: Path, *, max_bytes: int = MAX_TAIL_BYTES) -> Iterator[str]:
    """
    Yield decoded lines from the end of ``path`` towards the start.

    A partial line at a chunk boundary is held back and prepended to the next chunk
    rather than yielded, so callers never see a line cut in half.
    """
    with path.open("rb") as handle:
        handle.seek(0, io.SEEK_END)
        position = handle.tell()
        remainder = b""
        consumed = 0

        while position > 0 and consumed < max_bytes:
            # Clamp the chunk to what is left of the budget. Without this the first
            # read is always a full chunk, so a small budget is silently ignored and a
            # caller asking for a bounded scan gets an unbounded one.
            step = min(TAIL_CHUNK_BYTES, position, max_bytes - consumed)
            if step <= 0:
                break
            position -= step
            handle.seek(position)
            chunk = handle.read(step) + remainder
            consumed += step

            pieces = chunk.split(b"\n")
            # Trailing element is either empty (file ended with a newline) or a
            # fragment whose beginning was in the previous chunk.
            remainder = pieces.pop(0)
            for piece in reversed(pieces):
                if piece:
                    yield piece.decode("utf-8", "replace")

        if remainder:
            yield remainder.decode("utf-8", "replace")


def parse_metric_lines(
    lines: Iterable[str],
    schema: RoleSchema,
    metric: str,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
) -> list[tuple[int, Any]]:
    """
    Parse already-obtained CSV lines for one metric, oldest-first order preserved.

    Shared by the file reader and the open-buffer path in the API. Two parsers for the
    same format is how a column offset ends up fixed in one of them and not the other,
    and the symptom would be a metric that reads correctly from disk and wrongly from
    the buffer -- depending on whether the last 34 seconds had been flushed yet.
    """
    columns = schema.csv_header
    try:
        t_index = columns.index("t_dev_ms")
        v_index = columns.index(metric)
    except ValueError:
        return []

    field = next((f for f in schema.fields if f.name == metric), None)
    if field is None:
        return []

    out: list[tuple[int, Any]] = []
    for line in lines:
        parts = line.split(",")
        if len(parts) <= max(t_index, v_index):
            continue
        try:
            timestamp = int(parts[t_index])
        except ValueError:
            continue  # the header row, or a torn line
        if since_ms is not None and timestamp < since_ms:
            continue
        if until_ms is not None and timestamp > until_ms:
            continue
        raw = parts[v_index]
        if raw == "":
            continue  # a gap, not a zero
        out.append((timestamp, field.coerce(raw)))
    return out


def read_metric_window(
    root: Path,
    schema: RoleSchema,
    *,
    metric: str,
    since_ms: int,
    until_ms: int,
    limit: int,
    timezone_name: str,
    max_bytes: int = MAX_TAIL_BYTES,
) -> list[tuple[int, Any]]:
    """
    Raw ``(t_dev_ms, value)`` pairs for one metric, oldest first.

    Rows with a null value are skipped, matching ``/api/history``: a metric that a given
    firmware revision does not send should leave a gap in the series rather than a zero,
    and a zero would be indistinguishable from a real reading.
    """
    columns = schema.csv_header
    try:
        t_index = columns.index("t_dev_ms")
        v_index = columns.index(metric)
    except ValueError:
        return []

    field = next((f for f in schema.fields if f.name == metric), None)
    if field is None:
        return []

    collected: list[tuple[int, Any]] = []

    for day in days_covering(since_ms, until_ms, timezone_name):
        path = root / schema.role / f"{day.isoformat()}.csv"
        gz_path = root / schema.role / f"{day.isoformat()}.csv.gz"
        lines_iter = None
        if path.exists():
            lines_iter = iter_lines_reverse(path, max_bytes=max_bytes)
        elif gz_path.exists():
            import gzip
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as gz_f:
                    lines_iter = reversed(gz_f.readlines())
            except Exception:
                lines_iter = None

        if lines_iter is None:
            continue

        for line in lines_iter:
            parts = line.split(",")
            if len(parts) <= max(t_index, v_index):
                continue  # blank line, header, or a torn final row
            try:
                timestamp = int(parts[t_index])
            except ValueError:
                continue  # the header row, or corruption

            if timestamp > until_ms:
                continue  # this day's file starts later than the window
            if timestamp < since_ms:
                # Rows are chronological, so everything further back is older too.
                break

            raw = parts[v_index]
            if raw == "":
                continue
            collected.append((timestamp, field.coerce(raw)))
            if len(collected) >= limit:
                break

        if len(collected) >= limit:
            break

    collected.reverse()
    return collected
