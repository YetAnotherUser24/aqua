"""
Flush tests.

Flushing is what turns "the Pi is a rolling buffer" from a hope into a guarantee.
Retention deletes a local file only when ``flushed_at`` is set **and** ``verified``
is 1, and this module is the only thing that sets them — so the verification path is
load-bearing.

The tests that matter most are the negative ones: a corrupted upload must leave
``verified`` at 0, and a record that failed verification must still be refused by
retention. Those two together are the difference between "we have a backup" and "we
believe we have a backup".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aqua_gateway.archive.manifest import Manifest
from aqua_gateway.archive.prune import PruneMode, RetentionPolicy, Usage, prune
from aqua_gateway.backup.flush import FlushReport, archive_key, flush_once, verify_object
from aqua_gateway.config import ArchiveSettings, BackupSettings, Settings

from conftest import NOW, SECONDS_PER_DAY, FakeDisk, sha256_of, write_csv, write_gz


# ===========================================================================
# Fakes
# ===========================================================================


class FakeStorage:
    """
    In-memory object store, with knobs to break things on purpose.

    A working fake would only prove the happy path. The value here is ``corrupt`` and
    ``drop``: they let the test assert that a *bad* upload is detected and refused,
    which is the behaviour that protects the only copy.
    """

    def __init__(self, *, corrupt: bool = False, drop: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.corrupt = corrupt
        self.drop = drop

    def upload(self, key: str, source: Path) -> None:
        self.uploads.append(key)
        data = source.read_bytes()
        if self.corrupt:
            data = data[: max(1, len(data) // 2)]  # truncate: breaks the gzip
        self.objects[key] = data
        if self.drop:
            self.objects.pop(key, None)

    def download(self, key: str, destination: Path) -> None:
        destination.write_bytes(self.objects[key])

    def exists(self, key: str) -> bool:
        return key in self.objects

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        manifest_path=tmp_path / "manifest.db",
        archive=ArchiveSettings(root=tmp_path / "telemetry"),
        backup=BackupSettings(
            enabled=True,
            endpoint_url="https://example.invalid/storage/v1/s3",
            bucket="test-bucket",
            access_key_id="test-key",
            secret_access_key="test-secret",
            verify_after_upload=True,
        ),
    )


@pytest.fixture
def pending(archive_root: Path, manifest: Manifest):
    """
    A real gzipped archive file in the manifest, ready to flush.

    Mirrors the production sequence exactly, which matters: the writer opens the
    **uncompressed** path, closes it, and only then does rotation compress it and
    repoint the row. Opening the ``.gz`` first would not reproduce what the pipeline
    actually does, and ``mark_compressed`` keys off the original ``.csv`` path.

    Returns ``(record, gz_path)`` where the record's sha256 and row count describe the
    **compressed** file — which is what the flush verifies against.
    """
    day = "2026-10-09"
    csv_path = write_csv(archive_root / "aerator" / f"{day}.csv", rows=25)
    manifest.open_file(csv_path, "aerator", day)
    manifest.close_file(
        csv_path,
        rows=25,
        bytes_written=csv_path.stat().st_size,
        sha256=sha256_of(csv_path),
        now=NOW - SECONDS_PER_DAY,
    )

    gz_path = write_gz(archive_root / "aerator" / f"{day}.csv.gz", rows=25)
    manifest.mark_compressed(
        csv_path,
        archive_path=gz_path,
        bytes_written=gz_path.stat().st_size,
        sha256=sha256_of(gz_path),
        now=NOW - SECONDS_PER_DAY,
    )
    return manifest.pending_flush()[0], gz_path


# ===========================================================================
# Key derivation
# ===========================================================================


class TestArchiveKey:
    def test_derived_from_role_and_day(self, archive_root):
        day = "2026-10-09"
        path = write_gz(archive_root / "aerator" / f"{day}.csv.gz", rows=1)
        from conftest import make_record

        assert archive_key(make_record(path, role="aerator", day=day)) == f"raw/aerator/{day}.csv.gz"

    def test_not_derived_from_local_path(self, archive_root):
        """
        Rotation repoints a record from ``.csv`` to ``.csv.gz``. Deriving the key from
        the path would upload one day under two keys and silently create duplicates.
        """
        from conftest import make_record

        day = "2026-10-09"
        csv_record = make_record(archive_root / "aerator" / f"{day}.csv", role="aerator", day=day)
        gz_record = make_record(archive_root / "aerator" / f"{day}.csv.gz", role="aerator", day=day)

        assert archive_key(csv_record) == archive_key(gz_record)


# ===========================================================================
# Verification
# ===========================================================================


class TestVerifyObject:
    def test_accepts_a_faithful_copy(self, archive_root):
        path = write_gz(archive_root / "aerator" / "2026-10-09.csv.gz", rows=12)
        storage = FakeStorage()
        storage.upload("k", path)

        ok, detail = verify_object(storage, "k", expected_sha256=sha256_of(path), expected_rows=12)

        assert ok, detail

    def test_rejects_a_missing_object(self):
        ok, detail = verify_object(FakeStorage(), "absent", expected_sha256=None, expected_rows=1)

        assert not ok
        assert "not found" in detail

    def test_rejects_a_truncated_object(self, archive_root):
        path = write_gz(archive_root / "aerator" / "2026-10-09.csv.gz", rows=12)
        storage = FakeStorage(corrupt=True)
        storage.upload("k", path)

        ok, detail = verify_object(storage, "k", expected_sha256=sha256_of(path), expected_rows=12)

        assert not ok
        assert "sha256 mismatch" in detail

    def test_row_count_is_a_second_line_of_defence(self, archive_root):
        """
        When no hash is recorded, the decompressed row count is the only check left.
        It must catch a valid gzip whose content is short.
        """
        short = write_gz(archive_root / "short.csv.gz", rows=3)
        storage = FakeStorage()
        storage.upload("k", short)

        ok, detail = verify_object(storage, "k", expected_sha256=None, expected_rows=500)

        assert not ok
        assert "row mismatch" in detail

    def test_rejects_a_non_gzip_object(self, archive_root):
        bad = archive_root / "notgz.csv.gz"
        bad.write_bytes(b"plain text, not compressed")
        storage = FakeStorage()
        storage.upload("k", bad)

        ok, detail = verify_object(storage, "k", expected_sha256=None, expected_rows=1)

        assert not ok
        assert "gzip" in detail


# ===========================================================================
# Flush orchestration
# ===========================================================================


class TestFlushOnce:
    def test_uploads_and_verifies(self, settings, manifest, pending):
        record, _ = pending
        storage = FakeStorage()

        report = flush_once(settings, manifest, storage, now=NOW)

        assert report.ok
        assert report.uploaded and report.verified

        stored = manifest.all_records()[0]
        assert stored.flushed_at is not None
        assert stored.verified is True
        assert stored.archive_key == "raw/aerator/2026-10-09.csv.gz"

    def test_corrupted_upload_is_not_verified(self, settings, manifest, pending):
        """
        The core guarantee. An upload that does not survive verification must leave
        ``verified`` at 0 — otherwise the local file becomes prunable and the archive
        copy is short.
        """
        storage = FakeStorage(corrupt=True)

        report = flush_once(settings, manifest, storage, now=NOW)

        assert not report.ok
        assert report.failed

        stored = manifest.all_records()[0]
        assert stored.flushed_at is None
        assert stored.verified is False
        assert stored.verify_attempts == 1
        assert stored.last_error is not None

    def test_verification_leaves_the_record_eligible_for_retry(self, settings, manifest, pending):
        storage = FakeStorage(corrupt=True)
        flush_once(settings, manifest, storage, now=NOW)

        # Still pending, so the next run tries again rather than dropping it.
        assert len(manifest.pending_flush()) == 1

    def test_missing_local_file_never_marks_verified(self, settings, manifest, archive_root):
        day = "2026-10-09"
        csv_path = archive_root / "aerator" / f"{day}.csv"
        gz_path = archive_root / "aerator" / f"{day}.csv.gz"

        manifest.open_file(csv_path, "aerator", day)
        manifest.close_file(csv_path, rows=5, bytes_written=100, sha256="f" * 64, now=NOW)
        manifest.mark_compressed(csv_path, archive_path=gz_path, bytes_written=100, sha256="f" * 64, now=NOW)

        # Never created on disk — but the record claims it exists and is flushable.
        report = flush_once(settings, manifest, FakeStorage(), now=NOW)

        assert not report.ok
        stored = manifest.all_records()[0]
        assert stored.verified is False
        assert "missing" in report.failed[0][1]

    def test_verification_disabled_never_becomes_prunable(self, settings, manifest, pending, tmp_path):
        """
        Opting out of verification must not silently weaken retention. Records are
        uploaded but stay unverified, so they are simply never pruned — a growing local
        archive is the worst outcome here, not data loss.
        """
        settings.backup.verify_after_upload = False
        storage = FakeStorage()

        report = flush_once(settings, manifest, storage, now=NOW)

        assert report.uploaded
        assert report.verified == []
        assert manifest.all_records()[0].verified is False

    def test_already_flushed_records_are_not_reuploaded(self, settings, manifest, pending):
        storage = FakeStorage()
        flush_once(settings, manifest, storage, now=NOW)
        first_uploads = len(storage.uploads)

        flush_once(settings, manifest, storage, now=NOW)

        assert len(storage.uploads) == first_uploads

    def test_reports_backlog_before_and_after(self, settings, manifest, pending):
        report = flush_once(settings, manifest, FakeStorage(), now=NOW)

        assert report.backlog_before_s is not None
        assert report.backlog_after_s is None, "queue drained"

    def test_upload_failure_is_recorded_not_raised(self, settings, manifest, pending):
        class ExplodingStorage(FakeStorage):
            def upload(self, key: str, source: Path) -> None:
                raise OSError("connection reset")

        report = flush_once(settings, manifest, ExplodingStorage(), now=NOW)

        assert not report.ok
        assert manifest.all_records()[0].verified is False


# ===========================================================================
# Cross-module guarantee: a failed flush must not unblock retention
# ===========================================================================


class TestFlushFailureBlocksRetention:
    async def test_prune_refuses_after_a_failed_verification(self, settings, manifest, pending):
        """
        The integration that matters most.

        A file uploads, verification fails, and space is critically low. Retention must
        still refuse to delete it. This is the exact sequence that would otherwise
        destroy the only good copy of a day's data.
        """
        record, path = pending
        flush_once(settings, manifest, FakeStorage(corrupt=True), now=NOW)

        report = await prune(
            manifest,
            policy=RetentionPolicy(prune_mode=PruneMode.BOTH),
            open_files=[],
            disk_usage=FakeDisk(used_bytes=31 * 1024**3, free_bytes=1 * 1024**3),
            now=NOW + 90 * SECONDS_PER_DAY,
        )

        assert path.exists(), "unverified upload must not make the local file deletable"
        assert report.deleted == ()
        assert path in report.blocked

    async def test_prune_allows_deletion_after_a_verified_flush(self, settings, manifest, pending):
        """The mirror image: once verification succeeds, retention may proceed."""
        record, path = pending
        flush_once(settings, manifest, FakeStorage(), now=NOW)

        report = await prune(
            manifest,
            policy=RetentionPolicy(prune_mode=PruneMode.BOTH),
            open_files=[],
            disk_usage=FakeDisk(used_bytes=31 * 1024**3, free_bytes=1 * 1024**3),
            now=NOW + 90 * SECONDS_PER_DAY,
        )

        assert not path.exists()
        assert path in report.deleted
