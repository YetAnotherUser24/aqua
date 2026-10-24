"""
Finalising an archive file: hash it, count its rows, close its manifest record.

Extracted into its own module because two very different callers need identical
semantics:

* ``csv_writer`` finalises the file it has just finished writing, at rotation;
* ``rotate`` finalises files orphaned by an ungraceful shutdown.

They must agree exactly, or a file closed by one path and a file closed by the other
would produce different checksums for the same bytes, and the flush verification
would fail on some files and not others for no visible reason.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .manifest import FileRecord, Manifest

log = logging.getLogger("aqua.finalise")

CHUNK = 1 << 20  # 1 MiB — sequential reads, friendly to an SD card.


def hash_and_count(path: Path) -> tuple[str, int, int]:
    """
    Stream a file once, returning ``(sha256, rows, bytes)``.

    ``rows`` excludes the header line.

    Counting by reading rather than by tracking a live counter is deliberate: it makes
    normal close and crash recovery identical code. A counter maintained across a
    power cut is a counter that can be wrong in a way nobody can detect afterwards —
    which is precisely the class of bug that let 5 Hz fidelity disappear silently in
    the previous system.
    """
    digest = hashlib.sha256()
    size = 0
    newlines = 0
    last_byte = b""

    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            newlines += chunk.count(b"\n")
            last_byte = chunk[-1:]

    # A file not ending in a newline has a final partial record. It must still count:
    # a truncated last line is exactly what a power cut produces, and undercounting it
    # would make the flush's row check pass on data that is actually incomplete.
    if size and last_byte != b"\n":
        newlines += 1

    return digest.hexdigest(), max(0, newlines - 1), size


def find_record(manifest: Manifest, path: Path) -> FileRecord | None:
    """Look up a record by path, including soft-deleted rows."""
    for record in manifest.all_records(include_deleted=True):
        if record.path == path:
            return record
    return None


def finalise_file(manifest: Manifest, path: Path, *, now: float | None = None) -> FileRecord | None:
    """
    Hash, count and close a manifest record for ``path``.

    Idempotent: calling it twice on the same path recomputes the same values and
    writes the same result, so a retry after a crash is harmless.
    """
    if not path.exists():
        log.warning("cannot finalise, file absent", extra={"path": str(path)})
        return None

    sha256, rows, size = hash_and_count(path)
    manifest.close_file(path, rows=rows, bytes_written=size, sha256=sha256, now=now)
    log.info("finalised", extra={"path": str(path), "rows": rows, "bytes": size})
    return find_record(manifest, path)
