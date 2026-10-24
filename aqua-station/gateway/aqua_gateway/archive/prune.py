"""
Flush-aware, space-aware, configurable retention.

The governing rule, stated plainly:

    A local archive file may only be deleted once the remote archive is *verified*
    to hold it.

Being offline must never cause data loss. If the Pi was down for a month, or the
daily flush failed for a week, nothing becomes eligible — because eligibility keys
off ``flushed_at`` and ``verified``, never off the calendar alone.

When space runs low and nothing is eligible, this module does **not** delete
anyway. It refuses, and reports, so a human can decide. Silent data loss under
pressure is exactly the failure mode that a rolling buffer invites.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

from .manifest import FileRecord, Manifest


class PruneMode(str, Enum):
    AGE = "age"
    SPACE = "space"
    BOTH = "both"


class PruneOutcome(str, Enum):
    DELETED = "deleted"
    NOT_CLOSED = "not_closed"
    NOT_FLUSHED = "not_flushed"
    NOT_VERIFIED = "not_verified"
    TOO_YOUNG = "too_young"
    WITHIN_BUDGET = "within_budget"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    retention_days: int = 14
    max_local_bytes: int = 8 * 1024**3
    min_free_bytes: int = 2 * 1024**3
    prune_mode: PruneMode = PruneMode.BOTH

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        # Keeping the open file is not negotiable, so it is not a setting.
        if self.max_local_bytes <= 0 or self.min_free_bytes <= 0:
            raise ValueError("byte budgets must be positive")
        if self.min_free_bytes >= self.max_local_bytes:
            raise ValueError("min_free_bytes must be smaller than max_local_bytes")


@dataclass(frozen=True, slots=True)
class PruneDecision:
    path: Path
    outcome: PruneOutcome
    reason: str

    @property
    def should_delete(self) -> bool:
        return self.outcome is PruneOutcome.DELETED


@dataclass(frozen=True, slots=True)
class PruneReport:
    decisions: tuple[PruneDecision, ...]
    bytes_before: int
    bytes_after: int
    free_before: int
    free_after: int
    blocked: tuple[Path, ...]
    """Files that exceed the age budget but are not safe to delete. Alerting input."""

    @property
    def deleted(self) -> tuple[Path, ...]:
        return tuple(d.path for d in self.decisions if d.should_delete)

    @property
    def is_blocked(self) -> bool:
        """True when we wanted space but could not free it without losing data."""
        return bool(self.blocked)


def _is_closed(record: FileRecord, open_files: Sequence[Path]) -> bool:
    return record.path not in open_files and record.closed_at is not None


def evaluate(
    record: FileRecord,
    *,
    policy: RetentionPolicy,
    open_files: Sequence[Path],
    now: float,
    free_bytes: int,
    total_bytes: int,
) -> PruneDecision:
    """
    Decide whether a single file may be pruned. Pure function — testable without a filesystem.

    Order matters: correctness checks come first, so a file that is *unsafe* to
    delete is reported as unsafe rather than as "within budget".
    """
    if not _is_closed(record, open_files):
        return PruneDecision(record.path, PruneOutcome.NOT_CLOSED, "still being written")

    if not record.flushed_at:
        # The whole point: not yet archived, therefore untouchable.
        return PruneDecision(record.path, PruneOutcome.NOT_FLUSHED, "not yet in the archive")

    if not record.verified:
        return PruneDecision(
            record.path,
            PruneOutcome.NOT_VERIFIED,
            f"archive copy unverified after {record.verify_attempts} attempt(s)",
        )

    age_days = (now - record.closed_at) / 86_400 if record.closed_at else 0.0
    over_age = age_days > policy.retention_days
    over_budget = record.total_bytes > policy.max_local_bytes
    free_low = free_bytes < policy.min_free_bytes

    if policy.prune_mode is PruneMode.AGE and not over_age:
        return PruneDecision(record.path, PruneOutcome.TOO_YOUNG, f"age {age_days:.1f}d <= {policy.retention_days}d")
    if policy.prune_mode is PruneMode.SPACE and not (free_low or over_budget):
        return PruneDecision(record.path, PruneOutcome.WITHIN_BUDGET, "space budget has room")
    if policy.prune_mode is PruneMode.BOTH and not (over_age or over_budget or free_low):
        return PruneDecision(record.path, PruneOutcome.WITHIN_BUDGET, "within age and space budgets")

    triggers = []
    if over_age:
        triggers.append(f"age {age_days:.1f}d")
    if over_budget:
        triggers.append(f"archive {record.total_bytes / 1024**3:.2f}GiB > budget")
    if free_low:
        triggers.append(f"free {free_bytes / 1024**2:.0f}MiB < floor")

    return PruneDecision(record.path, PruneOutcome.DELETED, ", ".join(triggers))


async def prune(
    manifest: Manifest,
    *,
    policy: RetentionPolicy,
    open_files: Sequence[Path],
    disk_usage: "DiskUsageProbe",
    now: float | None = None,
) -> PruneReport:
    """
    Apply the retention policy, oldest-archived-first.

    Deletes only files whose archive copy is verified. Returns a report rather
    than raising, so the caller can alert on ``blocked`` without losing the
    summary of what did succeed.
    """
    moment = now if now is not None else time.time()
    usage_before = await disk_usage()
    records = sorted(
        (r for r in manifest.all_records() if r.role != "events"),
        key=lambda r: (r.closed_at or 0.0),
    )

    decisions: list[PruneDecision] = []
    blocked: list[Path] = []

    for record in records:
        decision = evaluate(
            record,
            policy=policy,
            open_files=open_files,
            now=moment,
            free_bytes=usage_before.free_bytes,
            total_bytes=usage_before.used_bytes,
        )
        decisions.append(decision)

        if decision.outcome is PruneOutcome.DELETED:
            try:
                record.path.unlink()
                manifest.mark_deleted(record.path)
            except OSError as exc:  # pragma: no cover - disk-level failure
                decisions[-1] = PruneDecision(record.path, PruneOutcome.NOT_CLOSED, f"unlink failed: {exc}")
        elif decision.outcome in (PruneOutcome.NOT_FLUSHED, PruneOutcome.NOT_VERIFIED):
            # We wanted to remove it for space reasons but must not. Surface it.
            if usage_before.free_bytes < policy.min_free_bytes or record.total_bytes > policy.max_local_bytes:
                blocked.append(record.path)

    usage_after = await disk_usage()
    return PruneReport(
        decisions=tuple(decisions),
        bytes_before=usage_before.used_bytes,
        bytes_after=usage_after.used_bytes,
        free_before=usage_before.free_bytes,
        free_after=usage_after.free_bytes,
        blocked=tuple(blocked),
    )


class DiskUsageProbe:
    """
    Callable returning current disk usage.

    Injected rather than called directly so the prune logic can be exercised in
    tests — including the case that matters most: space low, nothing eligible.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def __call__(self) -> "Usage":
        import shutil

        usage = shutil.disk_usage(self._path)
        return Usage(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free)


@dataclass(frozen=True, slots=True)
class Usage:
    total_bytes: int
    used_bytes: int
    free_bytes: int
