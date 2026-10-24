"""
Archive rotation: finalise, compress and prune.

Runs as a separate process (``python -m aqua_gateway.archive.rotate``) from a
systemd timer at 00:05 and 00:35 America/Lima. See ``deploy/systemd/aqua-rotate.service``.

Coordination with the running gateway
-------------------------------------

Two processes must never touch the same bytes. The rule is enforced through the
manifest's ``closed_at`` column:

    the gateway writes only to records with ``closed_at IS NULL``;
    this module touches only records with ``closed_at IS NOT NULL``.

So the first job here is to get the gateway to *close* its files. That is done by
calling its API, because the gateway owns the file handles and is the only thing
that knows whether a write is in flight. If the gateway is unreachable, nothing
holds handles, so closing stale records locally is safe.

Why this matters: the previous system had no such rule, and a rotation that moved
files underneath a live writer is exactly the kind of bug that produces a
half-written CSV nobody notices for weeks.

Power-loss safety
-----------------

A file left open when the Pi browned out has ``closed_at IS NULL`` forever, so it
would never be compressed, never flushed and never pruned — a silent leak that only
appears as unexplained disk growth months later. ``finalise_stale`` closes any open
record whose day has already ended, which is unambiguous: this process starts fresh
each run and owns no handles of its own.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from .finalise import finalise_file, hash_and_count
from .manifest import FileRecord, Manifest

log = logging.getLogger("aqua.rotate")

_CHUNK = 1 << 20  # 1 MiB — sequential I/O, friendly to an SD card.


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RotateReport:
    """What one rotation pass did. Logged, and returned so tests can assert on it."""

    finalised: list[Path] = field(default_factory=list)
    compressed: list[Path] = field(default_factory=list)
    compression_failed: list[tuple[Path, str]] = field(default_factory=list)
    strays_removed: list[Path] = field(default_factory=list)
    bytes_before: int = 0
    bytes_after: int = 0
    pruned: list[Path] = field(default_factory=list)
    prune_blocked: list[Path] = field(default_factory=list)
    gateway_notified: bool = False
    gateway_available: bool = False

    @property
    def ok(self) -> bool:
        return not self.compression_failed

    def summary(self) -> str:
        return (
            f"finalised={len(self.finalised)} compressed={len(self.compressed)} "
            f"failed={len(self.compression_failed)} strays={len(self.strays_removed)} "
            f"pruned={len(self.pruned)} blocked={len(self.prune_blocked)} "
            f"freed={(self.bytes_before - self.bytes_after) / 1024**2:.1f}MiB"
        )


# ---------------------------------------------------------------------------
# Finalising
# ---------------------------------------------------------------------------
# `hash_and_count` and `finalise_file` live in `finalise.py` because the CSV writer
# needs byte-identical semantics when it closes a file at rotation. Two
# implementations would eventually disagree and make flush verification fail on an
# arbitrary subset of files.
# ---------------------------------------------------------------------------


def finalise_stale(manifest: Manifest, *, today: date, timezone: str, now: float | None = None) -> list[Path]:
    """
    Close any open record whose day has already ended.

    Only records dated *before* today are touched. Today's file is left open on
    purpose: the gateway may still be resuming it, and closing it here would let the
    writer keep appending to a record the rest of the pipeline believes is finished.
    """
    closed: list[Path] = []
    for record in manifest.open_records():
        if record.day and record.day < today.isoformat():
            if record.path.exists():
                finalise_file(manifest, record.path, now=now)
                closed.append(record.path)
            else:
                # The file is gone but the record says it is open. Mark it deleted
                # rather than closing it: there is nothing to hash, and leaving it
                # open would block nothing but would confuse the backlog metric.
                log.warning("open record has no file on disk", extra={"path": str(record.path)})
                manifest.mark_deleted(record.path)
    return closed


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------


def compress_file(record: FileRecord, *, remove_source: bool = True) -> tuple[Path, str, int]:
    """
    Gzip a closed CSV.

    Written to a temporary name and renamed on success, so a power cut mid-gzip
    leaves a ``.tmp`` rather than a file that looks like a valid archive but is
    truncated. That distinction matters at flush time: a truncated ``.gz`` would
    upload, fail verification, and — if verification were ever skipped — become the
    only copy.

    The compressed stream is re-read and its line count compared to the record
    before the caller is allowed to mark it compressed.
    """
    source = record.path
    target = source.with_suffix(source.suffix + ".gz")
    temporary = target.with_suffix(target.suffix + ".tmp")

    with source.open("rb") as src, gzip.open(temporary, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, _CHUNK)

    # Verify by decompressing: proves the archive is readable, not merely written.
    with gzip.open(temporary, "rb") as check:
        lines = sum(chunk.count(b"\n") for chunk in iter(lambda: check.read(_CHUNK), b""))
    expected = record.rows + 1  # header
    if lines != expected:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"gzip verification failed: {lines} lines, expected {expected}")

    temporary.replace(target)
    sha256, _, size = hash_and_count(target)

    if remove_source:
        source.unlink(missing_ok=True)

    log.info(
        "compressed",
        extra={
            "source": str(source),
            "target": str(target),
            "ratio": round(record.bytes / size, 1) if size else 0,
        },
    )
    return target, sha256, size


def sweep_strays(archive_root: Path, manifest: Manifest, *, today: date) -> list[Path]:
    """
    Remove uncompressed CSVs that the manifest no longer tracks.

    A crash between ``mark_compressed`` and unlinking the source leaves the ``.csv``
    behind. Conservative on purpose: a stray is only removed when a matching ``.gz``
    exists on disk **and** is recorded as compressed in the manifest. Never delete a
    ``.csv`` that has no confirmed compressed counterpart — that would be deleting
    the only copy.
    """
    tracked = {record.path.resolve() for record in manifest.all_records(include_deleted=True)}
    compressed = {
        record.path.resolve()
        for record in manifest.all_records(include_deleted=True)
        if record.compressed_at is not None
    }
    removed: list[Path] = []

    for candidate in sorted(archive_root.glob("*/*.csv")):
        if candidate.resolve() in tracked:
            continue
        # Never touch today's file; the writer may be mid-append.
        if candidate.stem == today.isoformat():
            continue
        gz = candidate.with_suffix(candidate.suffix + ".gz")
        if not gz.exists() or gz.resolve() not in compressed:
            log.warning(
                "untracked csv with no confirmed .gz — leaving in place",
                extra={"path": str(candidate)},
            )
            continue
        candidate.unlink()
        removed.append(candidate)
        log.info("removed stray csv", extra={"path": str(candidate)})

    return removed


# ---------------------------------------------------------------------------
# Gateway handshake
# ---------------------------------------------------------------------------


async def notify_gateway(api_url: str, *, timeout: float = 10.0) -> bool:
    """
    Ask the running gateway to close any file whose day has ended.

    Best effort. A response of ``False`` is not an error — it means the gateway is
    down, which also means it holds no file handles, so ``finalise_stale`` can safely
    close those records itself.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/archive/rotate",
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed local URL
            return response.status == 200
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.info("gateway not reachable, rotating locally", extra={"error": str(exc)})
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def rotate(
    settings: Settings,
    manifest: Manifest,
    *,
    now: float | None = None,
    emit: Callable[[str, dict[str, object]], None] | None = None,
) -> RotateReport:
    """
    One full rotation pass: finalise, compress, sweep, prune.

    ``emit`` is an optional callback so this module does not hard-depend on the
    events table existing. The CLI passes nothing and the results are logged; tests
    pass a collector to assert on.
    """
    moment = now if now is not None else time.time()
    timezone = ZoneInfo(settings.timezone)
    today = datetime.fromtimestamp(moment, tz=timezone).date()
    report = RotateReport()
    archive_root = settings.archive.root

    def notify(name: str, detail: dict[str, object]) -> None:
        if emit is not None:
            emit(name, detail)

    report.bytes_before = _tree_bytes(archive_root)

    # 1. Get the gateway to close its files. If it is down, close stale ones here.
    report.gateway_available = await notify_gateway(f"http://{settings.api_host}:{settings.api_port}")
    report.gateway_notified = report.gateway_available
    if not report.gateway_available:
        report.finalised = finalise_stale(manifest, today=today, timezone=settings.timezone, now=moment)

    # 2. Compress everything closed but not yet gzipped.
    for record in manifest.closed_uncompressed():
        # A record dated today is never compressed, whatever its ``closed_at`` says.
        # That flag normally means the day is over, but `CsvArchiveWriter.close_all`
        # finalises on graceful shutdown and the next start resumes the same path, so
        # a mid-day restart leaves today's still-growing file marked closed. Gzipping
        # it here unlinks the source, and the running gateway goes on appending to
        # that now nameless inode: the next buffered block is written to a file with
        # no directory entry and is gone, with nothing reporting a failure. Today's
        # files are compressed tomorrow, when they really are finished -- the rule
        # `finalise_stale` and `sweep_strays` already follow.
        if record.day and record.day >= today.isoformat():
            continue
        if not record.path.exists():
            log.warning("closed record missing on disk", extra={"path": str(record.path)})
            continue
        try:
            target, sha256, size = compress_file(record)
        except (OSError, ValueError) as exc:
            # Do NOT mark compressed. The .csv stays, the record stays eligible, and
            # verification failure is surfaced rather than papered over.
            log.error("compression failed", extra={"path": str(record.path), "error": str(exc)})
            report.compression_failed.append((record.path, str(exc)))
            notify("archive_rotate_failed", {"path": str(record.path), "error": str(exc)})
            continue

        manifest.mark_compressed(
            record.path,
            archive_path=target,
            bytes_written=size,
            sha256=sha256,
            now=moment,
        )
        report.compressed.append(target)
        notify("archive_file_closed", {"path": str(target), "rows": record.rows, "bytes": size})

    # 3. Clean up leftovers from an interrupted previous run.
    report.strays_removed = sweep_strays(archive_root, manifest, today=today)

    # 4. Retention. Only files verified present in the archive are eligible.
    from .prune import DiskUsageProbe, prune

    prune_report = await prune(
        manifest,
        policy=settings.retention,
        open_files=manifest.open_file_paths(),
        disk_usage=DiskUsageProbe(archive_root),
        now=moment,
    )
    report.pruned = list(prune_report.deleted)
    report.prune_blocked = list(prune_report.blocked)
    report.bytes_after = _tree_bytes(archive_root)

    if report.pruned:
        notify("retention_pruned", {"count": len(report.pruned), "paths": [str(p) for p in report.pruned]})
    if report.prune_blocked:
        # Space pressure with nothing safe to delete. Loud, and NOT resolved by
        # deleting anyway.
        notify("retention_blocked_unflushed", {"count": len(report.prune_blocked)})
        log.warning(
            "retention blocked: over budget but nothing is safe to delete",
            extra={"count": len(report.prune_blocked), "paths": [str(p) for p in report.prune_blocked[:10]]},
        )

    log.info("rotation complete", extra={"summary": report.summary()})
    return report


def _tree_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate, compress and prune the AquaControl archive")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen, change nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = get_settings()
    if not settings.archive.root.exists():
        print(f"archive root does not exist: {settings.archive.root}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("dry run: no changes will be made", file=sys.stderr)

    manifest = Manifest(settings.manifest_path)
    try:
        if args.dry_run:
            pending = manifest.closed_uncompressed()
            print(json.dumps({"would_compress": [str(r.path) for r in pending]}, indent=2))
            return 0
        report = asyncio.run(rotate(settings, manifest))
    finally:
        manifest.close()

    if args.json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "finalised": [str(p) for p in report.finalised],
                    "compressed": [str(p) for p in report.compressed],
                    "failed": [(str(p), e) for p, e in report.compression_failed],
                    "strays_removed": [str(p) for p in report.strays_removed],
                    "pruned": [str(p) for p in report.pruned],
                    "prune_blocked": [str(p) for p in report.prune_blocked],
                    "freed_mib": round((report.bytes_before - report.bytes_after) / 1024**2, 2),
                },
                indent=2,
            )
        )
    else:
        print(report.summary())

    # Non-zero on compression failure so the unit shows as failed and monitoring
    # notices. Prune-blocked is a warning, not a failure: it is the system correctly
    # refusing to delete unarchived data.
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
