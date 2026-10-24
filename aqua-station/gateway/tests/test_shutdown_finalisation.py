"""
A graceful shutdown must not hand today's unfinished file to the rotation process.

`CsvArchiveWriter.close_all()` runs on every graceful shutdown, and it used to call
the same `_rotate` path as the midnight timer. That path ends by finalising the
record, which is the handoff that lets the rotation process see the file at all. The
two situations are not the same:

* the midnight timer closes a day that has genuinely ended;
* a shutdown closes a file that the next start reopens and appends to.

Setting ``closed_at`` on shutdown therefore tells the rest of the pipeline that an
in-progress day is complete. Rotation is entitled to gzip such a file and unlink the
source -- and the restarted gateway holds an open handle to that very path, so its
next buffered block is written into an inode with no directory entry left. The block
is gone, and nothing reports a failure: the writer's counters keep climbing and the
manifest row count silently stops matching the day.

`finalise_stale` already states the rule that was broken, in its own docstring:

    Today's file is left open on purpose: the gateway may still be resuming it, and
    closing it here would let the writer keep appending to a record the rest of the
    pipeline believes is finished.

`rotate()` now also refuses to compress a file dated today, as a second line of
defence that holds even if something else sets the flag early. Both halves are
tested here.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aqua_gateway.archive.csv_writer import CsvArchiveWriter
from aqua_gateway.archive.finalise import finalise_file
from aqua_gateway.archive.rotate import rotate
from aqua_gateway.config import ArchiveSettings, BackupSettings, Settings
from aqua_gateway.schema.registry import load_registry

TZ = "America/Lima"


def _today() -> date:
    """The date `rotate()` will see, computed the same way it computes it."""
    return datetime.now(tz=ZoneInfo(TZ)).date()


# These must be the *real* dates, not a fixture: `rotate()` derives its notion of today
# from the clock, so a hard-coded past date would silently take the "yesterday, safe to
# compress" branch and the guard under test would never be reached.
TODAY = _today()
YESTERDAY = TODAY - timedelta(days=1)
# Enough pump rows (~36 B each) to clear one 64 KB block, so the file on disk is real
# rather than a header.
ROWS = 2000


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture
def settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(
        manifest_path=tmp_path / "manifest.db",
        timezone=TZ,
        archive=ArchiveSettings(root=tmp_path / "telemetry"),
        backup=BackupSettings(enabled=False),
    )


def rows_for(schema, count: int) -> list[dict]:
    return [{field.name: 1.0 for field in schema.fields} for _ in range(count)]


def record_for(manifest, path: pathlib.Path):
    matches = [r for r in manifest.all_records() if r.path.name == path.name]
    assert matches, f"no manifest record for {path.name}"
    return matches[0]


async def append(root: pathlib.Path, manifest, registry, *, day: date, count: int) -> CsvArchiveWriter:
    """A fresh writer for `day`, as a process start would build."""
    writer = CsvArchiveWriter(root=root, manifest=manifest)
    schema = registry.for_role("pump")
    await writer.append_batch(schema, rows_for(schema, count), day=day, seq=1, t_src="ntp")
    return writer


class TestGracefulShutdown:
    def test_shutdown_does_not_finalise_today(self, tmp_path, manifest, registry):
        """The file is flushed to the card, but the day is not declared over."""
        root = tmp_path / "telemetry"

        async def scenario():
            writer = await append(root, manifest, registry, day=TODAY, count=ROWS)
            await writer.close_all(today=TODAY)

        asyncio.run(scenario())

        path = root / "pump" / f"{TODAY.isoformat()}.csv"
        assert path.exists() and path.stat().st_size > 0, "the buffer must reach the card"

        record = record_for(manifest, path)
        assert record.closed_at is None, "today's file is not finished; marking it closed hands a live file to rotation"

    def test_shutdown_does_finalise_an_earlier_day(self, tmp_path, manifest, registry):
        """
        The other half: a handle left over from a day that has ended *is* finished, so
        shutting down is the last chance to close it properly.
        """
        root = tmp_path / "telemetry"

        async def scenario():
            writer = await append(root, manifest, registry, day=YESTERDAY, count=ROWS)
            await writer.close_all(today=TODAY)

        asyncio.run(scenario())

        record = record_for(manifest, root / "pump" / f"{YESTERDAY.isoformat()}.csv")
        assert record.closed_at is not None
        assert record.sha256, "finalising also records the digest the flush verifies"


class TestRotationRefusesToday:
    def test_rotate_leaves_todays_closed_file_alone(self, tmp_path, manifest, registry, settings):
        """
        The second line of defence, tested on its own terms.

        The dangerous state is forced here rather than produced by a shutdown, because
        a shutdown no longer produces it. The guard has to hold anyway: any future path
        that sets ``closed_at`` early -- a clock change, a manual recovery, a third
        caller of `finalise_file` -- must not be able to reach the unlink.
        """
        root = settings.archive.root
        path = root / "pump" / f"{TODAY.isoformat()}.csv"

        async def write_it():
            writer = await append(root, manifest, registry, day=TODAY, count=ROWS)
            await writer.close_all(today=TODAY)

        asyncio.run(write_it())

        # Force the state: today's still-growing file, recorded as closed.
        finalise_file(manifest, path)
        assert any(r.path.name == path.name for r in manifest.closed_uncompressed()), (
            "precondition -- without it this test would pass even with no guard"
        )

        asyncio.run(rotate(settings, manifest))

        assert path.exists(), "gzipping this would unlink a file the gateway still appends to"
        assert not path.with_suffix(".csv.gz").exists()

    def test_a_restart_can_keep_writing_after_a_rotation_pass(self, tmp_path, manifest, registry, settings):
        """
        The failure, end to end: block, shutdown, rotation tick, restart, more data.

        With the old behaviour the rotation pass unlinked the file, and the second
        block -- written through an open handle to that same path -- went into an
        anonymous inode. The test would find a truncated file and no error anywhere.
        """
        root = settings.archive.root
        path = root / "pump" / f"{TODAY.isoformat()}.csv"

        async def first_life():
            writer = await append(root, manifest, registry, day=TODAY, count=ROWS)
            await writer.close_all(today=TODAY)
            return await rotate(settings, manifest)

        asyncio.run(first_life())
        after_first = path.stat().st_size

        async def second_life():
            # A new process: it has no idea a rotation has just run.
            writer = await append(root, manifest, registry, day=TODAY, count=ROWS)
            await writer.close_all(today=TODAY)

        asyncio.run(second_life())

        assert path.exists()
        assert path.stat().st_size > after_first, "the second block must not disappear"
        # Header plus both blocks.
        assert path.read_text().count("\n") == 2 * ROWS + 1
