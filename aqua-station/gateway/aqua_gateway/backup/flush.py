"""
Daily flush to the durable archive.

Runs as a oneshot from ``aqua-flush.timer`` at 03:00 America/Lima. See
``deploy/systemd/aqua-flush.service``.

The safety property
-------------------

Retention deletes a local file only when ``flushed_at`` is set **and**
``verified`` is 1. This module is the only thing that sets those fields, so the
integrity of the whole "the Pi is just a rolling buffer" design rests here.

Two consequences drive the implementation:

1. **Verify by reading the object back.** ``HeadObject`` reports the size somebody
   claimed; it does not prove the bytes arrived. Verification downloads the object,
   hashes it, and counts rows in the decompressed stream. That is the only check
   that actually proves the archive has a usable copy. Skipping it is how a
   truncated upload becomes the only copy — the local file gets pruned, and the
   loss is invisible until someone tries to restore it.

2. **A failed verification must not be "fixed" by retrying with a weaker check.**
   The record simply stays unflushed, the local file stays put, and the backlog
   grows. Growing backlog is a warning; deleted-only-copy is data loss.

Object keys come from ``role`` and ``day``, never from the local path. The day is
the authoritative field, and a path is free to change (it does, when rotation
repoints a record from ``.csv`` to ``.csv.gz``).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from ..config import Settings, get_settings
from ..archive.manifest import FileRecord, Manifest

log = logging.getLogger("aqua.flush")

_CHUNK = 1 << 20


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FlushReport:
    uploaded: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped_missing: list[str] = field(default_factory=list)
    bytes_uploaded: int = 0
    backlog_before_s: float | None = None
    backlog_after_s: float | None = None

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        return (
            f"uploaded={len(self.uploaded)} verified={len(self.verified)} "
            f"failed={len(self.failed)} missing={len(self.skipped_missing)} "
            f"bytes={self.bytes_uploaded / 1024**2:.2f}MiB"
        )


# ---------------------------------------------------------------------------
# Storage abstraction
# ---------------------------------------------------------------------------


class RemoteStorage(Protocol):
    """
    Minimal object-store surface.

    A Protocol rather than a concrete class so tests can inject a fake that
    deliberately corrupts an upload — which is the only way to prove the
    verification path actually rejects a bad object.
    """

    def upload(self, key: str, source: Path) -> None: ...

    def download(self, key: str, destination: Path) -> None: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str = "") -> list[str]: ...


class SupabaseStorage:
    """
    S3-compatible client for Supabase Storage.

    boto3 is imported lazily so the module can be imported (and tested) on a machine
    without it installed.
    """

    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        backup = settings.backup
        if not (backup.endpoint_url and backup.access_key_id and backup.secret_access_key):
            raise ValueError("storage endpoint or credentials missing")

        self.bucket = backup.bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=backup.endpoint_url,
            aws_access_key_id=backup.access_key_id,
            aws_secret_access_key=backup.secret_access_key,
            region_name=backup.region,
            config=Config(
                # The Pi has one uplink and a small backlog; parallelism here buys
                # nothing and risks starving the gateway of bandwidth.
                max_pool_connections=4,
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=15,
                read_timeout=120,
            ),
        )

    def upload(self, key: str, source: Path) -> None:
        self._client.upload_file(str(source), self.bucket, key)

    def download(self, key: str, destination: Path) -> None:
        self._client.download_file(self.bucket, key, str(destination))

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def list_keys(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def archive_key(record: FileRecord) -> str:
    """
    Deterministic object key, derived from role and day.

    Not from the local path: rotation repoints a record from ``.csv`` to ``.csv.gz``,
    and deriving the key from the path would mean an interrupted rotation uploaded to
    a different key and silently produced two objects for one day.
    """
    if record.role in ("events",) or record.path.name.startswith("events"):
        month = record.day[:7] if len(record.day) >= 7 else record.day
        return f"events/{month}.csv.gz"
    return f"raw/{record.role}/{record.day}.csv.gz"


def verify_object(
    storage: RemoteStorage,
    key: str,
    *,
    expected_sha256: str | None,
    expected_rows: int | None,
) -> tuple[bool, str]:
    """
    Download the object back and prove it is usable.

    Checks, in order of cost:

    * the object exists at all
    * its sha256 matches the local file's
    * its decompressed row count matches the record's

    The row-count check is the one that catches a truncated gzip which happens to
    have a matching header — a hash mismatch alone would catch most corruption, but
    the row count is what proves the *content* is complete, which is the thing
    retention is about to rely on.
    """
    tmp: Path | None = None
    try:
        if not storage.exists(key):
            return False, "object not found in archive"

        with tempfile.NamedTemporaryFile(prefix="aqua-verify-", suffix=".gz", delete=False) as handle:
            tmp = Path(handle.name)

        storage.download(key, tmp)

        if expected_sha256:
            digest = hashlib.sha256()
            with tmp.open("rb") as sink:
                while chunk := sink.read(_CHUNK):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != expected_sha256:
                return False, f"sha256 mismatch: remote {actual[:16]}… local {expected_sha256[:16]}…"

        if expected_rows is not None:
            try:
                with gzip.open(tmp, "rb") as check:
                    lines = sum(chunk.count(b"\n") for chunk in iter(lambda: check.read(_CHUNK), b""))
            except (OSError, EOFError) as exc:
                return False, f"remote object is not a readable gzip: {exc}"
            if lines != expected_rows + 1:  # + header
                return False, f"row mismatch: remote {lines - 1}, manifest {expected_rows}"

        return True, "ok"
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def flush_once(
    settings: Settings,
    manifest: Manifest,
    storage: RemoteStorage,
    *,
    now: float | None = None,
) -> FlushReport:
    """
    Upload and verify every eligible record.

    Synchronous on purpose: this is a oneshot process, the SDK is synchronous, and
    wrapping a handful of blocking calls in asyncio would add ceremony without
    concurrency worth having.
    """
    moment = now if now is not None else time.time()
    report = FlushReport()
    report.backlog_before_s = manifest.backlog_age_seconds(now=moment)

    for record in manifest.pending_flush(limit=settings.backup.max_uploads_per_run):
        key = archive_key(record)

        if not record.path.exists():
            # The record promised a file that is gone. Never mark flushed — that
            # would make retention treat a missing local file as safely archived.
            log.error("flush candidate missing on disk", extra={"path": str(record.path), "key": key})
            manifest.mark_flushed(record.path, archive_key=key, verified=False, error="local file missing")
            report.skipped_missing.append(key)
            report.failed.append((key, "local file missing"))
            continue

        try:
            storage.upload(key, record.path)
        except Exception as exc:  # noqa: BLE001 - SDK surfaces many exception types
            log.error("upload failed", extra={"key": key, "error": str(exc)})
            manifest.mark_flushed(record.path, archive_key=key, verified=False, error=f"upload: {exc}")
            report.failed.append((key, f"upload: {exc}"))
            continue

        report.uploaded.append(key)
        report.bytes_uploaded += record.bytes

        if not settings.backup.verify_after_upload:
            # Explicitly opted out. Nothing becomes prunable, because retention
            # requires `verified`, which stays 0. The data is simply never pruned.
            log.warning("verification disabled; %s will never be prunable", key)
            manifest.mark_flushed(record.path, archive_key=key, verified=False, error="verification disabled")
            continue

        ok, detail = verify_object(
            storage,
            key,
            expected_sha256=record.sha256,
            expected_rows=record.rows,
        )
        if ok:
            manifest.mark_flushed(record.path, archive_key=key, verified=True)
            report.verified.append(key)
            log.info("flushed and verified", extra={"key": key, "rows": record.rows, "bytes": record.bytes})
        else:
            # Uploaded but not trusted. The local file stays, the record stays
            # eligible, and the next run retries. This is the branch that protects
            # the only copy.
            log.error("verification failed", extra={"key": key, "detail": detail})
            manifest.mark_flushed(record.path, archive_key=key, verified=False, error=detail)
            report.failed.append((key, detail))

    report.backlog_after_s = manifest.backlog_age_seconds(now=moment)
    log.info("flush complete", extra={"summary": report.summary()})
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flush the AquaControl archive to object storage")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--list", action="store_true", help="show the pending work list and exit")
    parser.add_argument("--limit", type=int, default=None, help="cap uploads this run")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = get_settings()
    from ..archive.manifest import Manifest

    manifest = Manifest(settings.manifest_path)
    try:
        if args.list:
            pending = manifest.pending_flush(limit=args.limit or 500)
            print(
                json.dumps(
                    [
                        {
                            "key": archive_key(r),
                            "path": str(r.path),
                            "rows": r.rows,
                            "bytes": r.bytes,
                            "attempts": r.verify_attempts,
                            "last_error": r.last_error,
                        }
                        for r in pending
                    ],
                    indent=2,
                )
            )
            return 0

        if not settings.backup.enabled:
            print("backup disabled (AQUA_BACKUP__ENABLED=false); nothing to do", file=sys.stderr)
            return 0

        storage = SupabaseStorage(settings)
        if args.limit:
            settings.backup.max_uploads_per_run = args.limit
        report = flush_once(settings, manifest, storage)
    finally:
        manifest.close()

    if args.json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "uploaded": report.uploaded,
                    "verified": report.verified,
                    "failed": report.failed,
                    "missing": report.skipped_missing,
                    "bytes_uploaded": report.bytes_uploaded,
                    "backlog_before_s": report.backlog_before_s,
                    "backlog_after_s": report.backlog_after_s,
                },
                indent=2,
            )
        )
    else:
        print(report.summary())

    # Non-zero on any failure so the oneshot unit shows as failed and monitoring
    # sees it. A verification failure is a real failure: the archive may be short a
    # file, and retention must not proceed on the assumption that it is not.
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
