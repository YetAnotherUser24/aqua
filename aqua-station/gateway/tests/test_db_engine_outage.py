"""
Regression: a mid-query driver failure must become ``DatabaseUnavailable``.

``Database._require`` consults a *cached* health flag. A pool that looked healthy when that
flag was last written, and then lost its server, raises the raw driver error (``asyncpg``'s
``CannotConnectNowError``, ``ConnectionDoesNotExistError``, or an ``OSError`` for a closed
socket) straight out of ``acquire`` or the query.

Before the fix that escaped uncaught, so every endpoint turned an infrastructure outage into a
500 "Internal Server Error". TEST-PLAN §D4 asserts 503, and the reason matters as much as the
code: a 500 blames the gateway for a dependency being down and hides the fact that the CSV
archive tier is still healthy.

These tests drive the real `Database` against a stub pool, so the normalisation in
``_connection`` and ``_Transaction.__aenter__`` is exercised rather than assumed.
"""

from __future__ import annotations

import asyncio

import pytest

from aqua_gateway.db.engine import Database, DatabaseUnavailable


class _Boom(RuntimeError):
    """Stands in for a raw driver error: not a DatabaseUnavailable."""


class _StubConnection:
    async def execute(self, *_a, **_k) -> str:
        raise _Boom("server closed the connection unexpectedly")

    async def fetch(self, *_a, **_k):
        raise _Boom("server closed the connection unexpectedly")

    def transaction(self):
        raise _Boom("server closed the connection unexpectedly")


class _StubAcquire:
    def __init__(self, connection: _StubConnection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *_exc) -> bool:
        return False


class _StubPool:
    """A pool that hands out connections which fail on use."""

    def __init__(self) -> None:
        self.released = 0

    def acquire(self) -> _StubAcquire:
        return _StubAcquire(_StubConnection())


def _database_with(pool) -> Database:
    db = object.__new__(Database)
    db._pool = pool
    db._last_error = None
    db._connected_at = 123.0
    return db


def test_query_failure_becomes_database_unavailable() -> None:
    """A raw driver error during a query must not escape as something other than 503."""

    async def run() -> None:
        db = _database_with(_StubPool())
        with pytest.raises(DatabaseUnavailable):
            await db.fetch("SELECT 1")

    asyncio.run(run())


def test_a_failed_query_marks_the_pool_down() -> None:
    """The outage must drop the pool so the retry loop rebuilds it and healthz reports false."""

    async def run() -> None:
        db = _database_with(_StubPool())
        with pytest.raises(DatabaseUnavailable):
            await db.execute("SELECT 1")
        assert db._pool is None
        assert db.available is False

    asyncio.run(run())


def test_transaction_failure_becomes_database_unavailable() -> None:
    """The transaction path has the same narrowing, and a supersession depends on it."""

    async def run() -> None:
        db = _database_with(_StubPool())
        with pytest.raises(DatabaseUnavailable):
            async with db.transaction():
                pass  # pragma: no cover - the enter fails first

    asyncio.run(run())
