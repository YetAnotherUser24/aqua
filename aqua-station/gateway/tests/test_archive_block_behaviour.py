"""
The archive writer's block behaviour, and the data-loss window it implies.

`csv_writer.py` buffers in RAM and writes one block at a time, which is the whole reason
the SD card survives: ~58 write operations a day instead of ~38,000. The cost is that a
power cut loses the *unflushed tail*, and three separate places in this repository used to
describe that tail as "the last few seconds".

It is not seconds. One block is 64 KB and a row is 36-61 bytes depending on role, so the
byte-triggered tail is 1079-1811 rows -- 3.6-6.0 minutes *at the 5 Hz experiment cadence*.
That qualification is doing a lot of work: 64 KB is a fixed number of bytes, so the same
buffer is minutes for a 5 Hz node and over an hour for the od_sensor at its real 0.2 Hz. The
writer therefore releases on size **or** age, and the numbers here are measured from the real
writer so a schema change or a different block size fails these tests before the prose
becomes wrong again.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import date

import pytest

from aqua_gateway.archive.csv_writer import CsvArchiveWriter
from aqua_gateway.config import ArchiveSettings
from aqua_gateway.schema.registry import load_registry

DAY = date(2026, 10, 9)
BLOCK = 64 * 1024


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def rows_for(schema, count: int) -> list[dict]:
    return [{field.name: 1.0 for field in schema.fields} for _ in range(count)]


async def row_bytes(registry, role: str, sample: int = 100) -> float:
    """Bytes per row as the writer actually emits them, header excluded."""
    schema = registry.for_role(role)
    with __import__("tempfile").TemporaryDirectory() as tmp:
        writer = CsvArchiveWriter(root=pathlib.Path(tmp))
        await writer.append_batch(schema, rows_for(schema, sample), day=DAY, seq=1, t_src="ntp")
        return writer._open[role].buffer.tell() / sample


class TestBlockBehaviour:
    def test_nothing_is_written_before_a_block_is_full(self, registry):
        """
        The regression that prompted this file: 1125 rows of pump telemetry produced
        `rows_written: 1125` with `bytes_written: 0` on the Pi, which looks exactly like a
        broken archive. It is not -- pump rows are ~36 bytes, so 1125 rows is ~40 KB, still
        short of 64 KB. Asserting the boundary makes that legible instead of alarming.
        """

        async def run():
            schema = registry.for_role("pump")
            with __import__("tempfile").TemporaryDirectory() as tmp:
                writer = CsvArchiveWriter(root=pathlib.Path(tmp))
                # Sized from the measured row width so the intent survives schema changes.
                per_row = await row_bytes(registry, "pump")
                just_under = int((BLOCK * 0.9) / per_row)
                await writer.append_batch(schema, rows_for(schema, just_under), day=DAY, seq=1, t_src="ntp")
                return writer.stats.bytes_written, writer.stats.rows_written

        written, rows = asyncio.run(run())
        assert rows > 1000
        assert written == 0, "under a full block nothing should reach the card yet"

    def test_the_block_is_written_once_it_fills(self, registry):
        async def run():
            schema = registry.for_role("pump")
            with __import__("tempfile").TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                writer = CsvArchiveWriter(root=root)
                per_row = await row_bytes(registry, "pump")
                count = int((BLOCK * 1.2) / per_row)
                await writer.append_batch(schema, rows_for(schema, count), day=DAY, seq=1, t_src="ntp")
                files = list(root.rglob("*.csv"))
                rows_on_disk = sum(1 for _ in files[0].open()) - 1 if files else 0
                return writer.stats.block_writes, rows_on_disk, count

        blocks, on_disk, count = asyncio.run(run())
        assert blocks >= 1
        assert on_disk == count, "everything buffered should be on the card after the flush"

    def test_a_graceful_close_writes_the_partial_block(self, registry):
        """A short day must not evaporate: stopping cleanly flushes the tail."""

        async def run():
            schema = registry.for_role("pump")
            with __import__("tempfile").TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                writer = CsvArchiveWriter(root=root)
                await writer.append_batch(schema, rows_for(schema, 10), day=DAY, seq=1, t_src="ntp")
                before = writer.stats.bytes_written
                await writer.close_all()
                files = list(root.rglob("*.csv"))
                return before, sum(1 for _ in files[0].open()) - 1 if files else 0

        before, on_disk = asyncio.run(run())
        assert before == 0
        assert on_disk == 10


class TestDocumentedLossWindow:
    """
    The byte-triggered figures quoted in `docs/DATA-FORMAT.md` and `.env.example`.

    All of them are stated *at the 5 Hz experiment cadence*, which is the honest way to quote
    a byte count. ``TestBufferAgeCap`` is what stops that qualifier being skipped: the same
    numbers do not hold at a slower rate, and reading them as the whole story is how a
    ninety-minute window got described as four minutes.
    """

    DOCUMENTED = {"od_sensor": 3.9, "aerator": 3.6, "mixer": 5.4, "pump": 6.0}

    @pytest.mark.parametrize("role", sorted(DOCUMENTED))
    def test_minutes_of_tail_matches_the_documented_figure(self, registry, role):
        per_row = asyncio.run(row_bytes(registry, role))
        rows_in_block = BLOCK / per_row
        minutes = rows_in_block / 5 / 60
        assert minutes == pytest.approx(self.DOCUMENTED[role], abs=0.35), (
            f"{role} now buffers {minutes:.1f} min at 5 Hz; the docs say "
            f"{self.DOCUMENTED[role]}. Update csv_writer.py, docs/DATA-FORMAT.md and "
            f".env.example together."
        )

    def test_the_window_is_minutes_not_seconds(self, registry):
        """
        Guards the specific mistake: the prose said "the last few seconds" while the same
        paragraph stated the 64 KB mechanism. If someone shrinks the block far enough to
        make seconds true, this test tells them the prose can be relaxed again.
        """
        widest = max(asyncio.run(row_bytes(registry, role)) for role in self.DOCUMENTED)
        minutes = (BLOCK / widest) / 5 / 60
        assert minutes > 1.0, "the loss window is no longer measured in minutes"


class TestBufferAgeCap:
    """
    The age half of the OR.

    Size alone made the exposure a function of the sampling rate instead of of time, and the
    quoted figures hid it by all being computed at 5 Hz. These assert the cap and the sweep
    that makes it apply to a role which has stopped sending.
    """

    async def test_a_part_full_buffer_is_released_once_it_ages(self, registry):
        """The regression: at 0.2 Hz a byte-only trigger held telemetry for over an hour."""
        schema = registry.for_role("od_sensor")
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            writer = CsvArchiveWriter(root=root, max_buffer_age_s=0.02)
            await writer.append_batch(schema, rows_for(schema, 5), day=DAY, seq=1, t_src="ntp")
            assert writer.stats.bytes_written == 0, "5 rows is nowhere near 64 KB"

            await asyncio.sleep(0.1)
            # The next row re-enters the write path, which is where the age check lives.
            await writer.append_batch(schema, rows_for(schema, 1), day=DAY, seq=2, t_src="ntp")

            assert writer.stats.bytes_written > 0, "age must release a part-full buffer"
            assert writer.stats.flushes >= 1, "the release must be durable, not merely visible"
            files = list(root.rglob("*.csv"))
            assert sum(1 for _ in files[0].open()) - 1 == 6

    async def test_the_sweep_releases_a_silent_roles_buffer(self, registry):
        """
        A role that stops sending never re-enters the write path, so it needs the sweep.

        This is the job the gateway's maintenance task does; without it the cap would only
        ever apply to roles that are still transmitting, which are the ones that need it
        least.
        """
        schema = registry.for_role("od_sensor")
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            writer = CsvArchiveWriter(root=root, max_buffer_age_s=0.02)
            await writer.append_batch(schema, rows_for(schema, 5), day=DAY, seq=1, t_src="ntp")

            assert await writer.flush_aged() == 0, "nothing has aged yet"

            await asyncio.sleep(0.1)
            assert await writer.flush_aged() == 1
            assert writer.stats.flushes == 1, "the sweep must sync too"

            files = list(root.rglob("*.csv"))
            assert sum(1 for _ in files[0].open()) - 1 == 5

    async def test_the_byte_trigger_is_durable_too(self, registry):
        """
        The half that is easy to miss, and the reason the guarantee is structural.

        Syncing only the timed path would leave a fast role's byte-triggered releases in the
        page cache, and the cap would then be an upper bound that holds only because that
        role's fill rate happens to beat it. Both triggers sync, so the bound is the release
        interval itself.
        """
        schema = registry.for_role("pump")
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # A block_bytes of 1 makes the size trigger fire on the first batch.
            writer = CsvArchiveWriter(root=root, block_bytes=1)
            await writer.append_batch(schema, rows_for(schema, 3), day=DAY, seq=1, t_src="ntp")

            assert writer.stats.block_writes == 1
            assert writer.stats.flushes == 1, "the byte trigger has to sync as well"

    def test_the_cap_is_what_bounds_the_slow_node(self, registry):
        """
        The arithmetic that motivated the change, asserted rather than described.

        At the od_sensor's real 0.2 Hz the byte trigger alone is well over an hour. Stated as
        a number so the size-only reasoning cannot come back: for a slow node the cap has to
        be the binding term, and it has to be a duration worth quoting.
        """
        per_row = asyncio.run(row_bytes(registry, "od_sensor"))
        byte_window_min = (BLOCK / per_row) / 0.2 / 60
        cap_min = ArchiveSettings().max_buffer_age_s / 60

        assert byte_window_min > 60, "the byte trigger really is over an hour at 0.2 Hz"
        assert cap_min < byte_window_min, "the cap has to be what binds at 0.2 Hz"
        assert 1.0 <= cap_min <= 10.0, "the cap has to be a duration worth quoting"
