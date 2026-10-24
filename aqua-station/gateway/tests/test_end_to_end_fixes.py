"""
Regression tests for two bugs found only by running the whole stack end to end.

Both share the property that made them hard to see: nothing raised where the mistake was.
One failed inside a C extension with a message about `str` having no `days`; the other failed
silently at midnight during rotation while the gateway was otherwise healthy.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from aqua_gateway.api.app import _accounting_columns, _resolve_range
from aqua_gateway.archive.manifest import Manifest


# ---------------------------------------------------------------------------
# /api/history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected_table,expected_window",
    [
        ("1m", "rollup_1min", timedelta(minutes=1)),
        ("6h", "rollup_1min", timedelta(hours=6)),
        ("24h", "rollup_1min", timedelta(hours=24)),
        ("7d", "rollup_1h", timedelta(days=7)),
        ("30d", "rollup_1h", timedelta(days=30)),
        # Seconds come from the console's "Ventana temporal personalizada" panel, whose unit
        # dropdown offers Segundos/Minutos/Horas/Días. Only the last three were accepted, so
        # picking seconds sent `15s` and the route answered 422 while the chart died on an
        # "HTTP 422" card. Two fixes are possible — drop `s` from the dropdown or accept it
        # here — and accepting it is the smaller change and the one the operator asked for.
        ("30s", "rollup_1min", timedelta(seconds=30)),
        ("1s", "rollup_1min", timedelta(seconds=1)),
    ],
)
def test_range_window_is_a_timedelta(spec, expected_table, expected_window):
    """
    The history query binds `bucket > now() - $2::interval`.

    asyncpg encodes an interval parameter from a `datetime.timedelta`. Passing the previous
    `"1 hours"` string made the *codec* fail with "'str' object has no attribute 'days'", which
    surfaced as a bare HTTP 500. The SQL was never wrong — which is why running it in psql with a
    literal looked fine, and why this asserts the Python type rather than the query text.
    """
    table, window = _resolve_range(spec)
    assert table == expected_table
    assert isinstance(window, timedelta), f"{spec} produced {type(window).__name__}, not timedelta"
    assert window == expected_window


def test_range_rejects_out_of_bounds():
    from fastapi import HTTPException

    for spec in ("0h", "9999d"):
        with pytest.raises(HTTPException):
            _resolve_range(spec)


def test_range_pattern_accepts_every_unit_the_console_offers():
    """
    The console's custom-window dropdown offers exactly s/m/h/d, and it concatenates the number
    and the unit into one `range` query value. So the route's regex and that dropdown are two
    halves of one contract, and nothing asserted they agreed — which is how `Segundos (s)` came
    to be selectable and rejected.

    Read the pattern off the module source rather than restating it here, so this keeps checking
    the real route if the regex is ever edited.
    """
    import re

    from aqua_gateway.api import app as app_module

    source = Path(app_module.__file__).read_text()
    route_patterns = re.findall(r'pattern=r"\^\\d\+\(([^)]+)\)\$"', source)
    assert route_patterns, "no range pattern found — did the route change shape?"
    for units in route_patterns:
        assert set(units.split("|")) >= {"s", "m", "h", "d"}, (
            f"range pattern {units!r} cannot express every unit the console offers (s/m/h/d)"
        )


# ---------------------------------------------------------------------------
# manifest threading
# ---------------------------------------------------------------------------


def test_manifest_transaction_from_worker_thread(tmp_path):
    """
    The manifest must be usable off the event loop thread.

    Rotation finalises files with `asyncio.to_thread(finalise_file, manifest, path)`, while
    every other caller runs on the loop. Without `check_same_thread=False`, every such call
    raised `sqlite3.ProgrammingError`, so `closed_at` was never written — and a record that
    never closes is never compressed, flushed or pruned. That is precisely the silent disk leak
    the manifest exists to prevent, and it fails at 00:05 rather than at start-up.
    """
    manifest = Manifest(tmp_path / "manifest.db")

    async def run() -> int:
        def work() -> int:
            with manifest._tx() as conn:
                return conn.execute("SELECT 41 + 1 AS n").fetchone()["n"]

        # Same hop the real finalisation makes: worker thread, shared connection.
        return await asyncio.to_thread(work)

    try:
        assert asyncio.run(run()) == 42
    finally:
        manifest.close()


def test_manifest_concurrent_transactions_are_serialised(tmp_path):
    """
    `BEGIN IMMEDIATE ... COMMIT` spans a `yield`, so two threads interleaving would let one
    thread's ROLLBACK discard the other's committed work. The lock in `_tx` is what makes the
    cross-thread use safe rather than merely permitted.
    """
    manifest = Manifest(tmp_path / "manifest.db")
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(25):
                with manifest._tx() as conn:
                    conn.execute("SELECT 1")
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    try:
        assert not errors, f"concurrent transactions failed: {errors[:2]}"
        assert not any(thread.is_alive() for thread in threads), "a transaction deadlocked"
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# rollup table shape
# ---------------------------------------------------------------------------


def test_hourly_rollup_accounting_projection_is_null_not_invented():
    """
    A range past 24 h reads `rollup_1h`, which has no sample-accounting columns.

    The endpoint used to select `samples, samples_expected, uncertain_frac` unconditionally, so
    every range up to 24 h worked and anything beyond 500'd — the worst shape for a bug, because
    the ranges anyone tests by hand all pass. The fix is a per-table projection, and the point of
    this test is that the missing values are reported as NULL rather than faked: reporting a
    completeness figure the hourly rollup never computed is the invented-number failure this
    schema was redesigned to remove.
    """
    minute = _accounting_columns("rollup_1min")
    assert "samples_expected" in minute
    assert "uncertain_frac" in minute
    assert "NULL" not in minute, "the minute table has real values; do not mask them"

    hour = _accounting_columns("rollup_1h")
    assert "minutes_with_data AS samples" in hour, "the hourly table counts minutes, not samples"
    assert "NULL::integer AS samples_expected" in hour
    assert "NULL::double precision AS uncertain_frac" in hour
    # The columns a caller reads must exist in both projections.
    for name in ("samples", "samples_expected", "uncertain_frac"):
        assert name in minute and name in hour
