"""
Archive manifest — the bookkeeping that makes retention safe.

A tiny SQLite database recording, for every archive file, whether the remote copy
exists and has been *verified*. Retention consults this and nothing else.

SQLite (not PostgreSQL) deliberately: this file must be readable and writable even
if the database server is down or being upgraded, and it is a single-writer,
single-file concern. Coupling it to the main DB would make the archive's safety
property depend on a service that the archive does not otherwise need.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    role            TEXT NOT NULL,
    day             TEXT NOT NULL,          -- YYYY-MM-DD in America/Lima
    rows            INTEGER NOT NULL DEFAULT 0,
    bytes           INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT,
    closed_at       REAL,                   -- epoch seconds; NULL while open
    compressed_at   REAL,
    flushed_at      REAL,                   -- set ONLY after upload
    archive_key     TEXT,                   -- object key in Supabase Storage
    verified        INTEGER NOT NULL DEFAULT 0,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    -- Set when this row now points at a compressed file: holds the original .csv
    -- path. One manifest row per LOGICAL day-file and `path` tracks where it
    -- currently lives, so compression does not create a second row that retention
    -- would then have to know about.
    source_path     TEXT,
    deleted_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_files_role_day   ON files(role, day);
CREATE INDEX IF NOT EXISTS idx_files_closed_open ON files(closed_at)
    WHERE closed_at IS NOT NULL AND compressed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_files_flushable  ON files(flushed_at, verified)
    WHERE flushed_at IS NOT NULL AND verified = 1;
CREATE INDEX IF NOT EXISTS idx_files_pending    ON files(closed_at)
    WHERE closed_at IS NOT NULL AND flushed_at IS NULL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One archive file's lifecycle state."""

    path: Path
    role: str
    day: str
    rows: int
    bytes: int
    sha256: str | None
    closed_at: float | None
    compressed_at: float | None
    flushed_at: float | None
    archive_key: str | None
    verified: bool
    verify_attempts: int
    last_error: str | None
    source_path: str | None = None
    """Original ``.csv`` path, when this record now points at a compressed file."""

    @property
    def total_bytes(self) -> int:
        return self.bytes

    @property
    def is_archived(self) -> bool:
        return bool(self.flushed_at) and self.verified


class Manifest:
    """Thin, synchronous SQLite wrapper. The gateway calls it from the event loop via a thread executor."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` is required, not optional: finalisation runs in a worker
        # thread (`csv_writer.on_rotated` -> `asyncio.to_thread(finalise_file, manifest, ...)`)
        # while every other caller runs on the event loop. Without it, *every* rotation fails
        # with "SQLite objects created in a thread can only be used in that same thread", so
        # `closed_at`, `bytes` and `sha256` are never recorded — and an unfinalised record is
        # never eligible for compression, flush or prune. That is the silent disk leak this
        # module exists to prevent, and it fails quietly at 00:05 rather than at start-up.
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=10.0, check_same_thread=False)
        # Serialises transactions across threads. `BEGIN IMMEDIATE ... COMMIT` spans a `yield`,
        # so two threads interleaving would let one thread's ROLLBACK discard the other's work.
        self._lock = threading.RLock()
        self._connection.row_factory = sqlite3.Row
        # WAL keeps a crash from corrupting bookkeeping. NORMAL sync is right here:
        # this file is metadata, and losing the last write costs a re-verification,
        # not data. FULL would add SD writes for no benefit.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """
        Additive migrations for manifests written by an earlier version.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists, so
        a newly added column has to be applied explicitly. Without this the file
        silently keeps the old shape and the first query mentioning the new column
        fails at runtime — which would surface during rotation, at midnight.
        """
        existing = {row["name"] for row in self._connection.execute("PRAGMA table_info(files)")}
        additions = {"source_path": "TEXT"}
        for column, ddl in additions.items():
            if column not in existing:
                self._connection.execute(f"ALTER TABLE files ADD COLUMN {column} {ddl}")

    # -- lifecycle ---------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- writes ------------------------------------------------------------

    def open_file(self, path: Path, role: str, day: str) -> None:
        """Register a new archive file. Idempotent — reopening after restart is safe."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO files (path, role, day) VALUES (?, ?, ?) ON CONFLICT(path) DO NOTHING",
                (str(path), role, day),
            )

    def record_write(self, path: Path, *, rows: int, bytes_written: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET rows = ?, bytes = ? WHERE path = ?",
                (rows, bytes_written, str(path)),
            )

    def close_file(self, path: Path, *, rows: int, bytes_written: int, sha256: str, now: float | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET rows = ?, bytes = ?, sha256 = ?, closed_at = ? WHERE path = ?",
                (rows, bytes_written, sha256, now or time.time(), str(path)),
            )

    def mark_compressed(
        self,
        path: Path,
        *,
        archive_path: Path,
        bytes_written: int,
        sha256: str,
        now: float | None = None,
    ) -> None:
        """
        Repoint a record at its compressed form.

        One row per *logical* day-file: after gzip the row's ``path`` becomes the
        ``.gz``, and ``sha256``/``bytes`` are updated to match the compressed bytes.
        That matters — the flush verifies the remote object against this hash, and
        checking a ``.gz`` against the uncompressed hash would fail every time.
        ``source_path`` keeps the original ``.csv`` path for provenance.

        Idempotent: if the row no longer exists it was already repointed by a run
        that crashed after this step, so re-running after a power cut is safe.
        """
        moment = now or time.time()
        with self._tx() as conn:
            source = conn.execute("SELECT role, day FROM files WHERE path = ?", (str(path),)).fetchone()
            if source is None:
                return
            conn.execute(
                "UPDATE files SET path = ?, source_path = ?, compressed_at = ?, bytes = ?, sha256 = ? WHERE path = ?",
                (str(archive_path), str(path), moment, bytes_written, sha256, str(path)),
            )

    def mark_flushed(self, path: Path, *, archive_key: str, verified: bool, error: str | None = None) -> None:
        """
        Record the result of an upload.

        ``verified`` must reflect an actual checksum/row-count comparison on the
        remote side. Marking flushed without verifying would let retention delete
        the only good copy of a truncated upload.
        """
        with self._tx() as conn:
            if verified:
                conn.execute(
                    "UPDATE files SET flushed_at = ?, archive_key = ?, verified = 1, "
                    "verify_attempts = verify_attempts + 1, last_error = NULL WHERE path = ?",
                    (time.time(), archive_key, str(path)),
                )
            else:
                conn.execute(
                    "UPDATE files SET archive_key = ?, verify_attempts = verify_attempts + 1, "
                    "last_error = ? WHERE path = ?",
                    (archive_key, error or "verification failed", str(path)),
                )

    def mark_deleted(self, path: Path, now: float | None = None) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE files SET deleted_at = ? WHERE path = ?", (now or time.time(), str(path)))

    # -- reads -------------------------------------------------------------

    def all_records(self, *, include_deleted: bool = False) -> list[FileRecord]:
        sql = "SELECT * FROM files" + ("" if include_deleted else " WHERE deleted_at IS NULL")
        return [self._to_record(row) for row in self._connection.execute(sql)]

    def pending_flush(self, *, limit: int = 200) -> list[FileRecord]:
        """Closed, compressed, not yet successfully flushed. The daily flush work list."""
        rows = self._connection.execute(
            "SELECT * FROM files WHERE closed_at IS NOT NULL AND flushed_at IS NULL "
            "AND deleted_at IS NULL AND compressed_at IS NOT NULL ORDER BY closed_at ASC LIMIT ?",
            (limit,),
        )
        return [self._to_record(row) for row in rows]

    def closed_uncompressed(self, *, limit: int = 500) -> list[FileRecord]:
        """
        Closed but not yet gzipped. The rotation work list.

        The coordination invariant that keeps two processes off the same bytes:

            the gateway writes only to records with ``closed_at IS NULL``;
            every other process touches only records with ``closed_at IS NOT NULL``.

        This query is the second half of that contract, so rotation can never race
        the writer.
        """
        rows = self._connection.execute(
            "SELECT * FROM files WHERE closed_at IS NOT NULL AND compressed_at IS NULL "
            "AND deleted_at IS NULL ORDER BY closed_at ASC LIMIT ?",
            (limit,),
        )
        return [self._to_record(row) for row in rows]

    def open_records(self) -> list[FileRecord]:
        """Records the writer may still be appending to. Reconciliation input."""
        rows = self._connection.execute("SELECT * FROM files WHERE closed_at IS NULL AND deleted_at IS NULL")
        return [self._to_record(row) for row in rows]

    def open_file_paths(self) -> list[Path]:
        """Files still being written. Retention must never touch these."""
        rows = self._connection.execute("SELECT path FROM files WHERE closed_at IS NULL AND deleted_at IS NULL")
        return [Path(row["path"]) for row in rows]

    def backlog_age_seconds(self, *, now: float | None = None) -> float | None:
        """
        Age of the oldest unflushed file, or ``None`` when the queue is empty.

        This is the number the health endpoint reports as "last successful flush".
        With a 14-day local window it is the only warning before data loss becomes
        permanent.
        """
        row = self._connection.execute(
            "SELECT MIN(closed_at) AS oldest FROM files "
            "WHERE closed_at IS NOT NULL AND flushed_at IS NULL AND deleted_at IS NULL"
        ).fetchone()
        oldest = row["oldest"] if row else None
        if oldest is None:
            return None
        return (now or time.time()) - oldest

    def inconsistent(self) -> Sequence[FileRecord]:
        """Rows claiming to be archived but missing the fields that prove it."""
        rows = self._connection.execute(
            "SELECT * FROM files WHERE (flushed_at IS NOT NULL AND (sha256 IS NULL OR archive_key IS NULL)) "
            "OR (verified = 1 AND flushed_at IS NULL)"
        )
        return [self._to_record(row) for row in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            path=Path(row["path"]),
            role=row["role"],
            day=row["day"],
            rows=row["rows"],
            bytes=row["bytes"],
            sha256=row["sha256"],
            closed_at=row["closed_at"],
            compressed_at=row["compressed_at"],
            flushed_at=row["flushed_at"],
            archive_key=row["archive_key"],
            verified=bool(row["verified"]),
            verify_attempts=row["verify_attempts"],
            last_error=row["last_error"],
            source_path=row["source_path"],
        )
