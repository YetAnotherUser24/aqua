"""
Database access.

One deliberate architectural decision runs through this module:

    PostgreSQL is NOT on the telemetry critical path.

The durable record of telemetry is the CSV archive, written by a buffered writer that
shares nothing with the database. Rollups and events are *derived* — useful, but
re-derivable, and losing a minute of them costs a re-aggregation, not data. So a
database outage must never stop ingest, and it must never make a node's QoS 1 publish
fail.

That is why:

* ``connect()`` never raises. It marks the pool unavailable and a background task keeps
  retrying with backoff.
* Every query method raises :class:`DatabaseUnavailable` rather than returning ``None``.
  Callers *must* decide what to do, and silently getting a falsy value is how a failed
  write becomes an invisible data loss.
* Callers that can degrade (rollups, events) buffer instead of propagating.

The pool is deliberately tiny — two to eight connections — because the tuned
``max_connections`` on this host is 12 and PostgreSQL's per-connection overhead is the
main reason an untuned instance sits at ~200 MB instead of ~65 MB.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

from ..config import Settings

log = logging.getLogger("aqua.db")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

#: Applied migrations are tracked here, not by re-parsing the SQL. ``CREATE TABLE IF
#: NOT EXISTS`` throughout the migrations makes replaying harmless, but the tracking
#: table is what lets a genuinely destructive change be added later without a rewrite.
_TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER
);
"""


class DatabaseUnavailable(RuntimeError):
    """
    Raised when a query is attempted with no usable connection.

    Explicit rather than returning ``None``: a caller that treats "the database is
    down" as "no rows" will make a decision on false information, and in this system
    that decision is usually about whether data is safe to delete.
    """


class _Transaction:
    """Async context manager wrapping an asyncpg transaction on a pooled connection."""

    def __init__(self, db: "Database") -> None:
        self._db = db
        self._acquire: Any = None
        self._connection: Any = None

    async def __aenter__(self) -> Any:
        # Same normalisation as `Database._connection`: a pool that dies between ``_require``
        # and ``acquire`` raises the raw driver error, which must not reach a caller as a
        # 500. See that method for the full reasoning. Starting the transaction is inside the
        # guard too — it opens a server-side transaction, so it fails for exactly the same
        # reason and must be reported the same way.
        try:
            pool = self._db._require()
            self._acquire = pool.acquire()
            self._connection = await self._acquire.__aenter__()
            self._tx = self._connection.transaction()
            await self._tx.__aenter__()
        except DatabaseUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - the driver surfaces many types
            self._db._mark_down(exc)
            raise DatabaseUnavailable(str(exc)) from exc
        return self._connection

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            return await self._tx.__aexit__(exc_type, exc, tb)
        finally:
            await self._acquire.__aexit__(exc_type, exc, tb)


class Database:
    """Async connection pool with a never-fail connect and a background retry loop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pool: Any | None = None
        self._connect_lock = asyncio.Lock()
        self._retry_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_error: str | None = None
        self._connected_at: float | None = None
        self._consecutive_failures = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._pool is not None and not getattr(self._pool, "_closed", False)

    async def connect(self) -> bool:
        """
        Try once to establish the pool. Returns success; never raises.

        Callers are allowed to ignore the result: ingest must proceed whether or not
        this succeeds.
        """
        async with self._connect_lock:
            if self.available:
                return True
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover - packaging error
                self._last_error = f"asyncpg not installed: {exc}"
                log.error("asyncpg missing; database features disabled")
                return False

            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self.settings.database_url,
                    min_size=self.settings.db_pool_min,
                    max_size=self.settings.db_pool_max,
                    # A statement timeout protects the whole box: a runaway query on a
                    # 905 MB host takes PostgreSQL down with it, and PostgreSQL going
                    # down takes the dashboard with it.
                    command_timeout=15.0,
                    max_inactive_connection_lifetime=300.0,
                    server_settings={"application_name": "aqua-gateway"},
                )
                self._connected_at = time.time()
                self._consecutive_failures = 0
                self._last_error = None
                log.info(
                    "database pool ready",
                    extra={"min": self.settings.db_pool_min, "max": self.settings.db_pool_max},
                )
                return True
            except Exception as exc:  # noqa: BLE001 - driver surfaces many types
                self._last_error = str(exc)
                self._consecutive_failures += 1
                log.warning("database unavailable, continuing without it", extra={"error": str(exc)})
                self._pool = None
                return False

    def start_retry_loop(self) -> None:
        """
        Keep trying to reach the database in the background.

        A down database degrades the system; it does not stop it. Restarting the
        gateway to recover a connection would take the dashboard down for a problem
        that only affects derived data.
        """
        if self._retry_task is None or self._retry_task.done():
            self._stopping.clear()
            self._retry_task = asyncio.create_task(self._retry_forever(), name="db-retry")

    async def _retry_forever(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            if self.available:
                backoff = 1.0
                await asyncio.sleep(5.0)
                continue
            if await self.connect():
                log.info("database reconnected after %d failure(s)", self._consecutive_failures)
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def close(self) -> None:
        self._stopping.set()
        if self._retry_task is not None:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- queries -----------------------------------------------------------

    def _require(self) -> Any:
        if not self.available:
            raise DatabaseUnavailable(self._last_error or "no pool")
        return self._pool

    def _mark_down(self, exc: BaseException) -> None:
        """Record an outage seen mid-query and drop the pool so the retry loop rebuilds it."""
        self._last_error = str(exc)
        self._pool = None
        self._connected_at = None

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """
        Yield a pooled connection, normalising any driver failure to DatabaseUnavailable.

        ``_require`` only consults the *cached* health flag, which is set by ``connect()``
        and by a previous failure. A pool that was healthy when the flag was last updated and
        then loses its server raises the raw driver error — ``CannotConnectNowError``,
        ``ConnectionDoesNotExistError``, an ``OSError`` for a closed socket — straight out of
        ``acquire`` or the query. Letting that escape turns an infrastructure outage into a
        500 "Internal Server Error" at every endpoint, which is both the wrong status
        (TEST-PLAN §D4 asserts 503) and the wrong story: it blames the gateway for a
        dependency being down. Catching it here fixes all callers at once.
        """
        pool = self._require()
        try:
            async with pool.acquire() as connection:
                yield connection
        except DatabaseUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - the driver surfaces many types
            self._mark_down(exc)
            raise DatabaseUnavailable(str(exc)) from exc

    def transaction(self) -> Any:
        """
        Async context manager yielding a connection inside a transaction.

        Needed where two statements must be atomic — superseding a pending command and
        inserting its replacement, for instance. Doing those as two independent calls
        leaves a window in which a node polling at that instant could pick up the old
        command and execute an instruction the operator has already replaced.
        """
        return _Transaction(self)

    async def execute(self, query: str, *args: Any) -> str:
        async with self._connection() as connection:
            return await connection.execute(query, *args)

    async def executemany(self, query: str, rows: Sequence[Sequence[Any]]) -> None:
        """
        Batch insert. One round trip for a whole minute's rollups.

        The batch is what keeps the write count low: at 1-minute buckets the gateway
        does 4 inserts a minute rather than 5 per second, which matters on an SD-backed
        filesystem.
        """
        if not rows:
            return
        async with self._connection() as connection:
            await connection.executemany(query, rows)

    async def fetch(self, query: str, *args: Any) -> list[Mapping[str, Any]]:
        async with self._connection() as connection:
            return [dict(row) for row in await connection.fetch(query, *args)]

    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(query, *args)
            return dict(row) if row is not None else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        pool = self._require()
        async with pool.acquire() as connection:
            return await connection.fetchval(query, *args)

    # -- migrations --------------------------------------------------------

    async def apply_migrations(self) -> list[str]:
        """
        Apply any unapplied ``migrations/*.sql`` in filename order.

        Returns the filenames applied. Raises on failure — a half-migrated schema is
        worse than an unmigrated one, and this runs from the install script where
        failing loudly is correct.
        """
        applied: list[str] = []
        pool = self._require()

        async with pool.acquire() as connection:
            await connection.execute(_TRACKING_DDL)
            done = {row["filename"] for row in await connection.fetch("SELECT filename FROM schema_migrations")}

            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                started = time.perf_counter()
                # Each migration file runs in its own transaction: an all-or-nothing
                # schema change, so a failure leaves the database on the previous
                # version rather than somewhere in between.
                async with connection.transaction():
                    await connection.execute(sql)
                    await connection.execute(
                        "INSERT INTO schema_migrations (filename, duration_ms) VALUES ($1, $2)",
                        path.name,
                        int((time.perf_counter() - started) * 1000),
                    )
                applied.append(path.name)
                log.info("applied migration", extra={"file": path.name})

        return applied

    async def ensure_event_partition(self, months_ahead: int = 1) -> str | None:
        """
        Create next month's events partition.

        Called on startup and daily. A missing partition rejects writes, so this is the
        one schema operation that must not fail quietly — but it also must not stop the
        gateway, so failures are logged and retried on the next pass.
        """
        try:
            return await self.fetchval("SELECT ensure_event_partition($1)", months_ahead)
        except (DatabaseUnavailable, Exception) as exc:  # noqa: BLE001
            log.warning("could not ensure events partition", extra={"error": str(exc)})
            return None

    # -- health ------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Reported by ``/healthz``. Never raises."""
        if not self.available:
            return {
                "available": False,
                "error": self._last_error,
                "consecutive_failures": self._consecutive_failures,
            }
        try:
            version = await self.fetchval("SELECT version()")
            size_mb = await self.fetchval("SELECT pg_database_size(current_database()) / 1024 / 1024")
            connections = await self.fetchval("SELECT count(*) FROM pg_stat_activity")
            return {
                "available": True,
                "server": str(version).split(" ")[1] if version else None,
                "database_size_mb": int(size_mb) if size_mb is not None else None,
                "connections": connections,
                "connected_for_s": round(time.time() - self._connected_at, 1) if self._connected_at else None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": str(exc)}
