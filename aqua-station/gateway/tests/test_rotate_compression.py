"""
Compression, and the round-trip it promises.

`compress_file` had **no test**, and it was broken from the day it was written:
`rotate.py` calls `hash_and_count` but imports only `finalise_file` from
``.finalise``, so every pass of `aqua-rotate.service` died with::

    File "aqua_gateway/archive/rotate.py", line 160, in compress_file
        sha256, _, size = hash_and_count(target)
    NameError: name 'hash_and_count' is not defined

The unit is a 15-minute timer with no alerting, so on the Pi this failed quietly,
forever, taking day-closure, compression, retention pruning and the Supabase
backup with it: nothing was ever marked closed, so nothing was ever flushed. A
single test touching `compress_file` would have caught it on the first run.

These tests exercise the function rather than reading it.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import pathlib

import pytest

from aqua_gateway.archive.manifest import FileRecord
from aqua_gateway.archive.rotate import compress_file


def make_csv(tmp_path: pathlib.Path, rows: int, *, role: str = "pump") -> tuple[pathlib.Path, FileRecord]:
    """A CSV shaped like the archive's: header + `rows` data lines."""
    directory = tmp_path / role
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-10-09.csv"

    lines = ["t_dev_ms,t_src,seq,pump_dosed_ml"]
    lines += [f"1791600000{i:04d},ntp,{i},0.5" for i in range(rows)]
    body = ("\n".join(lines) + "\n").encode()
    path.write_bytes(body)

    record = FileRecord(
        path=path,
        role=role,
        day="2026-10-09",
        # `compress_file` compares decompressed lines against rows + 1 (the header),
        # so the fixture must agree with itself or the verification check fires.
        rows=rows,
        bytes=len(body),
        sha256=None,
        closed_at=1.0,
        compressed_at=None,
        flushed_at=None,
        archive_key=None,
        verified=False,
        verify_attempts=0,
        last_error=None,
    )
    return path, record


class TestCompressFile:
    def test_compresses_and_removes_the_source(self, tmp_path):
        path, record = make_csv(tmp_path, 50)
        target, sha256, size = compress_file(record)

        assert target == path.with_suffix(".csv.gz")
        assert target.exists()
        assert not path.exists(), "the source is removed by default"
        assert size == target.stat().st_size > 0
        # The digest must be of the compressed file, since that is what the flush
        # verifies against the remote object.
        assert sha256 == hashlib.sha256(target.read_bytes()).hexdigest()

    def test_the_compressed_file_round_trips(self, tmp_path):
        _, record = make_csv(tmp_path, 50)
        target, _, _ = compress_file(record)

        with gzip.open(target, "rb") as handle:
            restored = handle.read().decode()

        assert restored.count("\n") == record.rows + 1
        assert restored.startswith("t_dev_ms,t_src,seq,pump_dosed_ml\n")

    def test_a_truncated_source_is_refused_and_leaves_nothing_behind(self, tmp_path):
        """
        The verification exists so a truncated `.gz` cannot become the only copy. The
        record claims more rows than the file holds, which is what a mid-write power cut
        looks like.
        """
        path, record = make_csv(tmp_path, 50)
        record = dataclasses.replace(record, rows=999)

        with pytest.raises(ValueError, match="gzip verification failed"):
            compress_file(record)

        assert not path.with_suffix(".csv.gz").exists(), "no half-made archive may survive"
        assert not path.with_suffix(".csv.gz.tmp").exists()
        assert path.exists(), "and the source must not be deleted"

    def test_remove_source_false_keeps_the_csv(self, tmp_path):
        path, record = make_csv(tmp_path, 10)
        target, _, _ = compress_file(record, remove_source=False)

        assert target.exists() and path.exists()

    def test_zero_row_file_compresses(self, tmp_path):
        """A day with no telemetry still has a header to archive."""
        path, record = make_csv(tmp_path, 0)
        target, sha256, size = compress_file(record)

        assert target.exists() and size > 0 and sha256


class TestRotateModuleSurface:
    def test_every_name_compress_file_uses_is_imported(self):
        """
        The specific failure, stated directly: the module's namespace must contain the
        helpers its own functions call. Cheap, and it names the fix.
        """
        from aqua_gateway.archive import rotate

        for name in ("hash_and_count", "finalise_file", "Manifest", "FileRecord", "Settings"):
            assert hasattr(rotate, name), f"rotate.py uses {name} but does not import it"
