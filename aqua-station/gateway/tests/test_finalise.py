"""
Finalising tests.

``hash_and_count`` is where a subtle mistake is hardest to notice and most costly: an
undercounted row total makes the flush's row check pass on data that is actually
incomplete, and the local file then becomes prunable while the archive copy is short.

The power-cut case — a file whose last line is partial — is the one that matters.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from aqua_gateway.archive.finalise import finalise_file, find_record, hash_and_count

from conftest import sha256_of


class TestHashAndCount:
    def test_counts_data_rows_excluding_header(self, tmp_path: Path):
        path = tmp_path / "a.csv"
        path.write_text("h1,h2\n1,2\n3,4\n5,6\n")

        _, rows, size = hash_and_count(path)

        assert rows == 3
        assert size == path.stat().st_size

    def test_hash_matches_a_plain_read(self, tmp_path: Path):
        path = tmp_path / "a.csv"
        path.write_text("h\n1\n2\n")

        digest, _, _ = hash_and_count(path)

        assert digest == sha256_of(path)

    def test_truncated_final_line_still_counts(self, tmp_path: Path):
        """
        A power cut mid-write leaves a last line with no trailing newline. It is a real
        (partial) record and must be counted — undercounting here is what would make the
        flush believe an incomplete archive matches.
        """
        path = tmp_path / "a.csv"
        path.write_bytes(b"h1,h2\n1,2\n3,4\n5,6")

        _, rows, _ = hash_and_count(path)

        assert rows == 3

    def test_header_only_counts_zero_rows(self, tmp_path: Path):
        # An experiment that produced no samples is legitimate and must not be
        # reported as a negative or padded row count.
        path = tmp_path / "a.csv"
        path.write_text("h1,h2\n")

        _, rows, _ = hash_and_count(path)

        assert rows == 0

    def test_empty_file_counts_zero_rows(self, tmp_path: Path):
        path = tmp_path / "a.csv"
        path.write_bytes(b"")

        _, rows, size = hash_and_count(path)

        assert rows == 0
        assert size == 0

    def test_large_file_is_streamed_in_chunks(self, tmp_path: Path):
        """Exercises the multi-chunk path so the loop is not only tested at 2 line sizes."""
        path = tmp_path / "big.csv"
        with path.open("w") as handle:
            handle.write("t,v\n")
            for index in range(200_000):
                handle.write(f"{index},1\n")

        _, rows, size = hash_and_count(path)

        assert rows == 200_000
        assert size > (1 << 20), "fixture should exceed one chunk"


class TestFinaliseFile:
    def test_closes_the_manifest_record(self, tmp_path, manifest):
        path = tmp_path / "telemetry" / "aerator" / "2026-10-09.csv"
        path.parent.mkdir(parents=True)
        path.write_text("t,v\n1,2\n3,4\n")
        manifest.open_file(path, "aerator", "2026-10-09")

        record = finalise_file(manifest, path, now=1_791_548_022.0)

        assert record is not None
        assert record.closed_at == 1_791_548_022.0
        assert record.rows == 2
        assert record.sha256 == sha256_of(path)
        assert record.compressed_at is None, "finalising must not imply compression"

    def test_is_idempotent(self, tmp_path, manifest):
        """Retrying after a crash must not change the recorded values."""
        path = tmp_path / "telemetry" / "aerator" / "2026-10-09.csv"
        path.parent.mkdir(parents=True)
        path.write_text("t,v\n1,2\n")
        manifest.open_file(path, "aerator", "2026-10-09")

        first = finalise_file(manifest, path)
        second = finalise_file(manifest, path)

        assert first.sha256 == second.sha256
        assert first.rows == second.rows

    def test_missing_file_returns_none_without_raising(self, tmp_path, manifest):
        missing = tmp_path / "telemetry" / "aerator" / "nope.csv"

        assert finalise_file(manifest, missing) is None

    def test_find_record_includes_soft_deleted(self, tmp_path, manifest):
        path = tmp_path / "telemetry" / "aerator" / "2026-10-09.csv"
        path.parent.mkdir(parents=True)
        path.write_text("t,v\n1,2\n")
        manifest.open_file(path, "aerator", "2026-10-09")
        manifest.mark_deleted(path)

        assert find_record(manifest, path) is not None
        assert manifest.all_records() == []


class TestGzipRoundTrip:
    def test_compressed_row_count_matches_original(self, tmp_path: Path):
        """
        The invariant ``compress_file`` verifies before it is allowed to mark a file
        compressed: decompressing the archive must yield the same number of rows.
        """
        source = tmp_path / "a.csv"
        source.write_text("t,v\n" + "".join(f"{i},1\n" for i in range(37)))
        _, original_rows, _ = hash_and_count(source)

        archive = tmp_path / "a.csv.gz"
        with gzip.open(archive, "wb") as dst:
            dst.write(source.read_bytes())

        with gzip.open(archive, "rb") as check:
            lines = sum(chunk.count(b"\n") for chunk in iter(lambda: check.read(1 << 20), b""))

        assert lines == original_rows + 1  # + header
