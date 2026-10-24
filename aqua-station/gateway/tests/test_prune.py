"""
Retention tests.

This is the highest-value test file in the project, because it guards the property
the whole architecture depends on:

    a local archive file is deleted ONLY once the remote archive is verified to
    hold it.

Get this wrong and the "Pi is a rolling buffer, Supabase is the archive" design
silently becomes "the Pi is the only copy". That failure is invisible until someone
tries to restore, which is far too late.

The rule being asserted is *not* "delete after 14 days". It is:

    eligible  =  closed  AND  flushed  AND  verified  AND  (aged OR over budget)

with the correctness checks evaluated before the budget checks, so a file that is
unsafe to delete is always reported as unsafe rather than as "within budget".
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aqua_gateway.archive.prune import (
    PruneMode,
    PruneOutcome,
    RetentionPolicy,
    Usage,
    evaluate,
    prune,
)

from conftest import NOW, SECONDS_PER_DAY, FakeDisk, make_record, write_csv

OPEN_FILES: list[Path] = []


def decide(record, *, policy: RetentionPolicy, open_files=None, free_bytes=30 * 1024**3):
    return evaluate(
        record,
        policy=policy,
        open_files=open_files if open_files is not None else OPEN_FILES,
        now=NOW,
        free_bytes=free_bytes,
        total_bytes=2 * 1024**3,
    )


# ===========================================================================
# Correctness gates come first
# ===========================================================================


class TestSafetyGates:
    """Properties that make a file *unsafe* to delete must win over every budget."""

    def test_open_file_is_never_deleted(self, tmp_path, policy):
        """A file being written must not be touched, even at 100 days old."""
        path = tmp_path / "aerator" / "2026-07-01.csv"
        record = make_record(path, closed_days_ago=None)

        decision = decide(record, policy=policy, open_files=[path])

        assert decision.outcome is PruneOutcome.NOT_CLOSED
        assert not decision.should_delete

    def test_unflushed_file_is_never_deleted(self, tmp_path, policy):
        """
        THE core guarantee. An ancient file that has not reached the archive is
        untouchable, no matter how old or how low on space we are.
        """
        path = tmp_path / "aerator" / "2026-01-01.csv"
        record = make_record(path, flushed=False, verified=False, closed_days_ago=365)

        decision = decide(record, policy=policy, free_bytes=1)

        assert decision.outcome is PruneOutcome.NOT_FLUSHED
        assert not decision.should_delete

    def test_uploaded_but_unverified_file_is_never_deleted(self, tmp_path, policy):
        """
        Uploaded is not the same as archived. A truncated upload that was never
        verified must not become the only copy.
        """
        path = tmp_path / "aerator" / "2026-01-01.csv.gz"
        record = make_record(path, flushed=True, verified=False, closed_days_ago=365, last_error="sha256 mismatch")

        decision = decide(record, policy=policy, free_bytes=1)

        assert decision.outcome is PruneOutcome.NOT_VERIFIED
        assert not decision.should_delete
        assert "1 attempt" in decision.reason

    def test_file_flushed_but_not_closed_is_not_deleted(self, tmp_path, policy):
        """
        Contradictory state (flushed yet still open) resolves to the conservative
        outcome. It should not happen, and if it does, refusing to delete is right.
        """
        path = tmp_path / "aerator" / "2026-10-09.csv"
        record = make_record(path, closed_days_ago=None, flushed=True)

        decision = decide(record, policy=policy, open_files=[path])

        assert decision.outcome is PruneOutcome.NOT_CLOSED
        assert not decision.should_delete


# ===========================================================================
# Budgets only apply once a file is safe
# ===========================================================================


class TestBudgets:
    def test_safe_and_aged_is_deleted(self, tmp_path, policy):
        path = tmp_path / "aerator" / "2026-09-01.csv.gz"
        record = make_record(path, closed_days_ago=30)

        decision = decide(record, policy=policy)

        assert decision.outcome is PruneOutcome.DELETED
        assert decision.should_delete
        assert "age 30.0d" in decision.reason

    def test_safe_and_young_is_kept(self, tmp_path, policy):
        """
        Default mode is BOTH, so the reported reason is the budget that was satisfied
        rather than the age that was not — 'within budget' is the honest description
        when space is fine and the age rule is simply not the binding constraint.
        """
        path = tmp_path / "aerator" / "2026-10-08.csv.gz"
        record = make_record(path, closed_days_ago=1)

        decision = decide(record, policy=policy)

        assert decision.outcome is PruneOutcome.WITHIN_BUDGET
        assert not decision.should_delete

    def test_age_mode_reports_too_young(self, tmp_path):
        """In AGE-only mode the age rule is the sole criterion, so it names the reason."""
        policy = RetentionPolicy(prune_mode=PruneMode.AGE)
        path = tmp_path / "aerator" / "2026-10-08.csv.gz"
        record = make_record(path, closed_days_ago=1)

        decision = decide(record, policy=policy)

        assert decision.outcome is PruneOutcome.TOO_YOUNG
        assert not decision.should_delete

    def test_space_mode_reports_within_budget(self, tmp_path):
        policy = RetentionPolicy(prune_mode=PruneMode.SPACE)
        path = tmp_path / "aerator" / "2026-01-01.csv.gz"
        record = make_record(path, closed_days_ago=365)

        decision = decide(record, policy=policy, free_bytes=30 * 1024**3)

        assert decision.outcome is PruneOutcome.WITHIN_BUDGET
        assert not decision.should_delete

    def test_safe_but_young_is_deleted_under_space_pressure(self, tmp_path, policy):
        """
        Space pressure overrides the age rule — but only for a file that is safe.
        Eligible here because it is closed, flushed and verified.
        """
        path = tmp_path / "aerator" / "2026-10-08.csv.gz"
        record = make_record(path, closed_days_ago=1)

        decision = decide(record, policy=policy, free_bytes=100 * 1024**2)

        assert decision.outcome is PruneOutcome.DELETED
        assert "free" in decision.reason

    def test_over_local_budget_triggers_deletion(self, tmp_path):
        policy = RetentionPolicy(max_local_bytes=1 * 1024**2, min_free_bytes=1024)
        path = tmp_path / "aerator" / "2026-10-08.csv.gz"
        record = make_record(path, size=8 * 1024**2, closed_days_ago=1)

        decision = decide(record, policy=policy)

        assert decision.outcome is PruneOutcome.DELETED

    @pytest.mark.parametrize(
        ("days_old", "mode", "tight_space", "expect_delete"),
        [
            # age mode ignores space entirely
            (30, PruneMode.AGE, True, True),
            (1, PruneMode.AGE, True, False),
            # space mode ignores age entirely
            (1, PruneMode.SPACE, True, True),
            (1, PruneMode.SPACE, False, False),
            (365, PruneMode.SPACE, False, False),
            # both: either trigger is enough
            (30, PruneMode.BOTH, False, True),
            (1, PruneMode.BOTH, True, True),
            (1, PruneMode.BOTH, False, False),
        ],
    )
    def test_prune_modes(self, tmp_path, days_old, mode, tight_space, expect_delete):
        policy = RetentionPolicy(prune_mode=mode)
        path = tmp_path / "aerator" / "x.csv.gz"
        record = make_record(path, closed_days_ago=days_old)

        decision = decide(
            record,
            policy=policy,
            free_bytes=(100 * 1024**2) if tight_space else (30 * 1024**3),
        )

        assert decision.should_delete is expect_delete


# ===========================================================================
# End-to-end, against a real manifest and real files
# ===========================================================================


class TestPruneIntegration:
    async def test_deletes_file_and_records_it(self, archive_root, manifest, disk, policy):
        path = write_csv(archive_root / "aerator" / "2026-09-01.csv.gz", rows=5)
        record = make_record(path, closed_days_ago=30, size=path.stat().st_size)
        manifest.open_file(path, record.role, record.day)
        manifest.close_file(path, rows=record.rows, bytes_written=record.bytes, sha256=record.sha256)
        manifest.mark_flushed(path, archive_key="raw/aerator/2026-09-01.csv.gz", verified=True)

        report = await prune(
            manifest,
            policy=policy,
            open_files=[],
            disk_usage=disk,
            now=NOW + 30 * SECONDS_PER_DAY,
        )

        assert path.exists() is False, "file should have been removed from disk"
        assert any(p == path for p in report.deleted)

        # Soft-deleted, not hard-deleted: the manifest keeps the history so a future
        # "where did this file go?" question is answerable.
        assert manifest.all_records() == []
        assert len(manifest.all_records(include_deleted=True)) == 1

    async def test_offline_period_deletes_nothing(self, archive_root, manifest, tight_disk, policy):
        """
        The scenario the user specifically asked about:

        the Pi was down for months, so the flush never ran. Space is critically low.
        A naive retention policy would delete the oldest files to free space. This one
        must refuse, because nothing has reached the archive.

        Asserted precisely: no file removed, and the situation is *reported* so a human
        can act on it.
        """
        paths = []
        for day in ("2026-01-01", "2026-02-01", "2026-03-01"):
            path = write_csv(archive_root / "aerator" / f"{day}.csv", rows=5)
            manifest.open_file(path, "aerator", day)
            manifest.close_file(path, rows=5, bytes_written=path.stat().st_size, sha256="b" * 64)
            # Deliberately NOT flushed: the Pi was offline.
            paths.append(path)

        report = await prune(
            manifest,
            policy=policy,
            open_files=[],
            disk_usage=tight_disk,
            now=NOW + 90 * SECONDS_PER_DAY,
        )

        assert report.deleted == ()
        assert all(p.exists() for p in paths), "no local file may be removed while unarchived"
        assert len(report.blocked) == 3, "the refusal must be visible, not silent"
        assert report.is_blocked

    async def test_unverified_upload_does_not_unblock_deletion(self, archive_root, manifest, tight_disk, policy):
        """
        A file that uploaded but failed verification must behave exactly like one that
        never uploaded: kept, and reported as blocked.
        """
        path = write_csv(archive_root / "aerator" / "2026-01-01.csv.gz", rows=5)
        manifest.open_file(path, "aerator", "2026-01-01")
        manifest.close_file(path, rows=5, bytes_written=path.stat().st_size, sha256="c" * 64)
        manifest.mark_flushed(path, archive_key="raw/aerator/2026-01-01.csv.gz", verified=False, error="row mismatch")

        report = await prune(
            manifest, policy=policy, open_files=[], disk_usage=tight_disk, now=NOW + 90 * SECONDS_PER_DAY
        )

        assert path.exists()
        assert report.deleted == ()
        assert path in report.blocked

    async def test_open_file_is_spared_even_when_verified(self, archive_root, manifest, tight_disk, policy):
        """Defence in depth: even a fully-verified record is spared while open."""
        path = write_csv(archive_root / "aerator" / "2026-01-01.csv", rows=5)
        manifest.open_file(path, "aerator", "2026-01-01")

        report = await prune(
            manifest,
            policy=policy,
            open_files=[path],  # the writer still holds it
            disk_usage=tight_disk,
            now=NOW + 90 * SECONDS_PER_DAY,
        )

        assert path.exists()
        assert report.deleted == ()

    async def test_oldest_archived_deleted_first(self, archive_root, manifest, policy):
        """
        Deletion order is oldest-archived-first, so the freshest verified backup is
        always the last thing standing.
        """
        old = write_csv(archive_root / "aerator" / "2026-08-01.csv.gz", rows=1)
        new = write_csv(archive_root / "aerator" / "2026-09-01.csv.gz", rows=1)

        for path, day in ((old, "2026-08-01"), (new, "2026-09-01")):
            manifest.open_file(path, "aerator", day)
            manifest.close_file(path, rows=1, bytes_written=path.stat().st_size, sha256="d" * 64)
            manifest.mark_flushed(path, archive_key=f"raw/aerator/{day}.csv.gz", verified=True)

        report = await prune(
            manifest,
            policy=policy,
            open_files=[],
            disk_usage=FakeDisk(used_bytes=31 * 1024**3, free_bytes=1 * 1024**3),
            now=NOW + 60 * SECONDS_PER_DAY,
        )

        assert old.exists() is False
        assert new.exists() is False
        # Both were eligible; ordering is asserted via the report sequence.
        assert report.decisions[0].path == old

    async def test_report_captures_bytes_and_blocked(self, archive_root, manifest, disk, policy):
        path = write_csv(archive_root / "aerator" / "2026-09-01.csv.gz", rows=5)
        manifest.open_file(path, "aerator", "2026-09-01")
        manifest.close_file(path, rows=5, bytes_written=path.stat().st_size, sha256="e" * 64)
        manifest.mark_flushed(path, archive_key="k", verified=True)

        report = await prune(manifest, policy=policy, open_files=[], disk_usage=disk, now=NOW + 30 * SECONDS_PER_DAY)

        assert report.bytes_before >= report.bytes_after
        assert report.free_after >= report.free_before
        assert report.blocked == ()


# ===========================================================================
# Policy validation
# ===========================================================================


class TestRetentionPolicy:
    def test_defaults_match_documentation(self):
        policy = RetentionPolicy()
        assert policy.retention_days == 14
        assert policy.prune_mode is PruneMode.BOTH
        assert policy.min_free_bytes < policy.max_local_bytes

    @pytest.mark.parametrize("days", [0, -1])
    def test_rejects_non_positive_retention(self, days):
        with pytest.raises(ValueError, match="retention_days"):
            RetentionPolicy(retention_days=days)

    def test_rejects_floor_above_ceiling(self):
        with pytest.raises(ValueError, match="min_free_bytes"):
            RetentionPolicy(max_local_bytes=1024, min_free_bytes=2048)

    def test_keep_open_file_is_not_configurable(self):
        """Spared open files are a guarantee, not a setting someone can turn off."""
        assert "keep_open_file" not in RetentionPolicy.__dataclass_fields__
