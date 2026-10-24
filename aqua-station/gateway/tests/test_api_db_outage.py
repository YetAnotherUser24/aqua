"""
Regression: a database outage must be reported as 503, not 500.

`gateway.db.available` is a *cached* flag — it only flips once a query has already failed.
The per-endpoint `if not gateway.db.available: raise HTTPException(503)` guards therefore miss
the very first request after the database goes away, and that request raised a bare
`DatabaseUnavailable` which FastAPI turned into a plain 500 "Internal Server Error".

A 500 is the wrong signal twice over: it reads as a bug in the gateway rather than a down
dependency, and it hides the fact that the CSV archive tier is still healthy and still
recording. TEST-PLAN §D4 asserts the 503 explicitly.

No `TestClient` here: starlette's needs `httpx`, which this project does not depend on, and
pulling in a dependency to assert one status code is not worth it. The handler is exercised
directly instead, which is the piece that has to exist for the request to be a 503 at all.
"""

from __future__ import annotations

import asyncio

from aqua_gateway.api.app import create_app
from aqua_gateway.db.engine import DatabaseUnavailable
from aqua_gateway.main import Gateway


class _DownDatabase:
    """A handle whose `available` flag is stale-true while every query fails."""

    available = True

    async def fetch(self, *_args, **_kwargs):
        raise DatabaseUnavailable("no pool")


def _app_with_db_down():
    gateway = object.__new__(Gateway)
    gateway.db = _DownDatabase()  # type: ignore[assignment]
    gateway.settings = type("S", (), {"timezone": "America/Lima"})()
    return create_app(gateway)


def test_database_unavailable_is_registered_as_503() -> None:
    """The handler must exist: without it FastAPI turns the exception into a 500."""
    gateway = object.__new__(Gateway)
    gateway.db = _DownDatabase()  # type: ignore[assignment]
    gateway.settings = type("S", (), {"timezone": "America/Lima"})()
    app = create_app(gateway)

    handler = app.exception_handlers.get(DatabaseUnavailable)
    assert handler is not None, "no handler: a DB outage would surface as a bare 500"

    response = asyncio.run(handler(None, DatabaseUnavailable("no pool")))
    assert response.status_code == 503
    assert b"database offline" in response.body
