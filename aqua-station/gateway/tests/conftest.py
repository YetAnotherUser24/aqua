"""
Shared test fixtures.

The retention and flush behaviour being tested here is the safety property the whole
"the Pi is just a rolling buffer" design rests on, so the fixtures are deliberately
explicit about creating *real* files on disk and a *real* SQLite manifest. Mocking
those away would test the mock, not the guarantee.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest

from aqua_gateway.archive.manifest import FileRecord, Manifest
from aqua_gateway.archive.prune import Usage

DAY = "2026-10-09"
NOW = 1_791_548_022.0  # a fixed epoch, so age arithmetic is deterministic
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class FakeDisk:
    """Deterministic disk-usage probe, so space-pressure tests are exact."""

    total_bytes: int = 32 * 1024**3
    used_bytes: int = 2 * 1024**3
    free_bytes: int = 30 * 1024**3

    async def __call__(self) -> Usage:
        return Usage(
            total_bytes=self.total_bytes,
            used_bytes=self.used_bytes,
            free_bytes=self.free_bytes,
        )


def make_record(
    path: Path,
    *,
    role: str = "aerator",
    day: str = DAY,
    rows: int = 100,
    size: int = 4096,
    sha256: str | None = "a" * 64,
    closed_days_ago: float | None = 0.0,
    compressed: bool = True,
    flushed: bool = True,
    verified: bool = True,
    verify_attempts: int = 1,
    last_error: str | None = None,
    now: float = NOW,
) -> FileRecord:
    """
    Build a :class:`FileRecord` with a coherent lifecycle.

    Defaults describe a file that is *safe to prune* — closed, compressed, flushed and
    verified. Each test then breaks exactly one property, so a failure points at the
    property that matters rather than at a fixture that was wrong to begin with.
    """
    return FileRecord(
        path=path,
        role=role,
        day=day,
        rows=rows,
        bytes=size,
        sha256=sha256,
        closed_at=None if closed_days_ago is None else now - closed_days_ago * SECONDS_PER_DAY,
        compressed_at=(now - closed_days_ago * SECONDS_PER_DAY) if compressed and closed_days_ago is not None else None,
        flushed_at=(now - closed_days_ago * SECONDS_PER_DAY) if flushed and closed_days_ago is not None else None,
        archive_key=f"raw/{role}/{day}.csv.gz" if flushed else None,
        verified=verified,
        verify_attempts=verify_attempts,
        last_error=last_error,
    )


def write_csv(path: Path, *, rows: int = 10, header: str = "t_dev_ms,drv_rpm") -> Path:
    """Create a real CSV on disk with the given number of data rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(header + "\n")
        for index in range(rows):
            handle.write(f"{1_791_548_022_000 + index},{400 + index}\n")
    return path


def write_gz(path: Path, *, rows: int = 10) -> Path:
    """Create a real gzipped CSV, matching what rotation produces."""
    import gzip

    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        handle.write("t_dev_ms,drv_rpm\n")
        for index in range(rows):
            handle.write(f"{1_791_548_022_000 + index},{400 + index}\n")
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    root = tmp_path / "telemetry"
    root.mkdir()
    return root


@pytest.fixture
def manifest(tmp_path: Path) -> Iterator[Manifest]:
    """A real SQLite manifest, closed after the test."""
    instance = Manifest(tmp_path / "manifest.db")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def disk() -> FakeDisk:
    """Plenty of free space: no space-based pruning will trigger."""
    return FakeDisk()


@pytest.fixture
def tight_disk() -> FakeDisk:
    """Below the default 2 GiB floor, so space-based pruning engages."""
    return FakeDisk(used_bytes=31 * 1024**3, free_bytes=1 * 1024**3)


@pytest.fixture
def policy():
    from aqua_gateway.archive.prune import RetentionPolicy

    return RetentionPolicy()
