"""
HTTP API.

Served on loopback; Caddy proxies ``/api/*`` and ``/events`` on the same origin as
the static dashboard, so the existing UI's relative ``/api/...`` URLs keep working
with no code change.

The one architectural change visible from the browser: the four polling endpoints
(dashboard summary, history, events, alerts) collapse into a single long-lived SSE
stream plus a handful of request/response endpoints that are only touched on user
action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Sequence
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..archive.rotate import finalise_stale
from ..archive.reader import parse_metric_lines, read_metric_window
from ..commands.outbox import CommandError
from ..db.engine import DatabaseUnavailable
from ..events.model import Event, EventType, Scope, Severity, Source
from ..export.bundle import ExportError, build_bundle
from ..export.flat import FlatExportError, build_flat_export, list_exports
from ..orchestration.recipes import RECIPES
from ..rollup.aggregator import AGG_COLUMNS, METRIC_SPECS

if TYPE_CHECKING:  # pragma: no cover - avoids a circular import at runtime
    from ..main import Gateway

log = logging.getLogger("aqua.api")


class CommandRequest(BaseModel):
    """Operator command. Published to ``aqua/cmd/<role>`` and recorded in the outbox."""

    role: str
    action: str
    target: dict[str, Any] = Field(default_factory=dict)
    experiment_id: str | None = None
    # A stale command must never execute. A node waking from deep sleep after a day
    # has to refuse rather than act on a setpoint from a finished experiment.
    deadline_s: int = Field(default=300, ge=5, le=3600)
    requested_by: str = "operator:web"


class StateRequest(BaseModel):
    """Orchestrator transition: IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE."""

    state: str
    experiment_id: str | None = None
    requested_by: str = "operator:web"


#: field name -> aggregate suffixes that actually exist as rollup columns.
#: Derived from the aggregator's specs, so the dashboard's chart picker cannot offer a
#: metric that no column backs. Duplicating this list in TypeScript is how a UI ends up
#: requesting a column nobody writes.
_AGGREGATES_BY_FIELD: dict[str, list[str]] = {spec.field: [agg.suffix for agg in spec.aggs] for spec in METRIC_SPECS}


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

#: Above this, charts read the hourly table. A 30-day chart at minute resolution is
#: 43,200 points for a phone screen, and the extra precision is invisible — while the
#: cost of reading it is very visible on this hardware.
_MINUTE_RESOLUTION_LIMIT_MINUTES = 60 * 24


#: A node whose clock runs slightly ahead of the gateway must not have its newest rows
#: filtered out for being "in the future". Small enough that it cannot admit real future
#: data, large enough to absorb ordinary NTP skew.
CLOCK_SKEW_TOLERANCE_MS = 5_000


class ExperimentCreateRequest(BaseModel):
    """Create an experiment so commands can reference it by id."""

    title: str
    #: Optional. Generated as ``EXP-YYYY-MM-DD-NN``, the format the table documents.
    id: str | None = None
    roles: list[str] = Field(default_factory=list)
    hypothesis: str | None = None
    notes: str | None = None
    #: Which recipe this run follows. Persisted into ``notes`` as ``recipe:<id>`` because the
    #: experiments table is shared with the legacy flow; the orchestrator reads it back to
    #: seed the phase plan. Defaults to the step protocol when omitted.
    recipe_id: str | None = None
    #: Per-phase mode chosen *before* launch, keyed by ``phase_id`` (auto | hibrido | manual).
    #: The operator prepares the plan in the UI, and the choice has to reach the row the run
    #: will read, or it is lost the moment ``_begin`` seeds from the recipe. Only phases the
    #: operator actually changed need appear; the rest default to the recipe's own mode.
    phase_modes: dict[str, str] = Field(default_factory=dict)
    #: Per-phase parameters chosen *before* launch, keyed by ``phase_id``.
    phase_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_by: str = "operator:web"


class DOControlRequest(BaseModel):
    """Manual DO closed-loop control launch request."""

    setpoint_mg_l: float = Field(default=6.00, ge=0.0, le=14.0, description="DO target in mg/L")
    kp: float = Field(default=221.65, ge=0.0, description="Proportional gain")
    ki: float = Field(default=1.915, ge=0.0, description="Integral gain")
    kd: float = Field(default=0.0, ge=0.0, description="Derivative gain")
    beta: float = Field(default=0.35, ge=0.0, le=1.0, description="Setpoint weighting factor (2-DOF)")
    tt: float = Field(default=100.0, ge=0.1, description="Anti-windup tracking time constant (s)")
    tau_lag: float = Field(default=30.0, ge=0.0, description="Predictive coasting horizon (s)")
    mode: str = Field(default="pid", description="Control mode: 'pid' or 'fuzzy'")
    experiment_id: str | None = None
    requested_by: str = "operator:manual"


class MilestoneRequest(BaseModel):
    """An operator's documented point on the run timeline."""

    #: One of ``sulfite`` | ``step`` | ``winkler`` | ``note`` — matched by the UI's chip.
    kind: str = "note"
    label: str
    detail: str = ""
    #: Device clock at the moment of the annotation, when the client has one. Optional:
    #: the gateway stamps its own arrival time regardless, and a browser without a synced
    #: clock must still be able to record a milestone.
    at_t_dev_ms: int | None = None
    requested_by: str = "operator:web"


def _next_experiment_id(day: date, existing_ids: Sequence[str]) -> str:
    """
    Next free ``EXP-<day>-NN`` for that day.

    Derived from the ids already present rather than a sequence, because the id is
    documented as human-readable and is quoted in exports and notes. The caller inserts with
    ``ON CONFLICT DO NOTHING``, so a concurrent create loses the race cleanly instead of
    overwriting an experiment -- which is the failure that would actually matter, and it is
    why this does not need to be atomic.
    """
    prefix = f"EXP-{day.isoformat()}-"
    highest = 0
    for existing in existing_ids:
        suffix = str(existing)[len(prefix) :] if str(existing).startswith(prefix) else ""
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1:02d}"


def _iso_utc(t_dev_ms: int) -> str:
    """
    Epoch milliseconds as ISO-8601 UTC.

    Millisecond precision matters here in a way it does not for rollups: at 5 Hz two
    samples are 200 ms apart, and a seconds-only timestamp would render them on top of
    each other -- which looks like fewer points rather than a formatting loss.
    """
    return (
        datetime.fromtimestamp(t_dev_ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _resolve_range(spec: str) -> tuple[str, timedelta]:
    """
    Map a range like ``24h`` to ``(table, window)``.

    Both values are constructed from a validated integer and a fixed unit, never from
    the raw string, so the pattern check plus this mapping is what keeps the range out
    of the query text.

    The window is a ``timedelta``, not a string. asyncpg binds a ``::interval`` parameter
    by encoding a Python ``timedelta``; handing it ``"1 hours"`` fails in the codec with
    ``'str' object has no attribute 'days'``. String intervals only ever appear to work
    when they are pasted into a query as literals, which is exactly what a psql
    reproduction does — so a literal test can pass while the bound version 500s.
    """
    amount = int(spec[:-1])
    unit = spec[-1]

    if amount <= 0 or amount > 3_650:
        raise HTTPException(status_code=400, detail="range out of bounds")

    # Seconds are a first-class unit, not a scaled minute: the console's custom window
    # offers "Segundos (s)", and accepting it here keeps that control from 422-ing. The
    # result stays a timedelta built from a validated integer and a fixed unit, so the
    # raw string still never reaches the query text.
    window = {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(minutes=amount * 60),
        "d": timedelta(minutes=amount * 1_440),
    }[unit]
    table = "rollup_1min" if window <= timedelta(minutes=_MINUTE_RESOLUTION_LIMIT_MINUTES) else "rollup_1h"
    return table, window


def _accounting_columns(table: str) -> str:
    """
    Sample-accounting columns for a rollup table, in a shape both tables can produce.

    The two rollup tables do not carry the same accounting columns. `rollup_1min` tracks
    `samples` / `samples_expected` / `uncertain_frac`; `rollup_1h` tracks `minutes_with_data`
    and nothing else. Selecting one fixed column list works for every range up to 24 h and then
    fails with a 500 for anything beyond — the worst shape for a bug, because the short ranges
    anyone tests by hand all pass.

    The absent values are returned as NULL rather than substituted. `minutes_with_data` is not a
    sample count, and reporting a completeness figure the hourly rollup never computed would be
    an invented number — exactly the failure this schema was redesigned to remove. The dashboard
    renders unknown coverage as "expected sample count not declared".
    """
    if table == "rollup_1min":
        return "samples, samples_expected, uncertain_frac"
    return "minutes_with_data AS samples, NULL::integer AS samples_expected, NULL::double precision AS uncertain_frac"


def _resolve_metric_column(metric: str) -> str:
    """
    Validate a metric name against the declared aggregate columns.

    ``metric`` arrives from a query string, so it is matched against the whitelist
    rather than interpolated. Accepting either the exact column (``od_do_mg_l_avg``)
    or the bare field (``od_do_mg_l``, defaulting to its average) keeps the frontend
    simple without loosening the check.
    """
    if metric in AGG_COLUMNS:
        return metric
    candidate = f"{metric}_avg"
    if candidate in AGG_COLUMNS:
        return candidate
    raise HTTPException(
        status_code=400,
        detail=f"unknown metric {metric!r}; available: {sorted(AGG_COLUMNS)}",
    )


def create_app(gateway: "Gateway") -> FastAPI:
    """Build the ASGI app around a live gateway instance."""
    app = FastAPI(
        title="aqua-gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    settings = gateway.settings

    # A database outage must read as "the database is down" (503), never as a server bug
    # (500) or an empty list. `gateway.db.available` is a cached flag that only flips when a
    # query has already failed, so the per-endpoint `if not gateway.db.available` guards miss
    # the very first request after the database goes away — which then raised a bare
    # DatabaseUnavailable and became a 500. One handler covers every endpoint, including any
    # added later, and keeps the guard as a fast path rather than the only defence.
    @app.exception_handler(DatabaseUnavailable)
    async def _database_unavailable(_request: Request, exc: DatabaseUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": f"database offline: {exc}"},
        )

    # -- health ------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """
        Health for Caddy, the install script and the runbook.

        Three numbers carry the operational meaning:

        * ``flush_backlog_s`` — age of the oldest unflushed archive file. With a 14-day
          local window this is the only warning before data loss becomes permanent.
        * ``spilled`` events — climbing means the database is behind and the event log
          is accumulating on disk instead of in the table.
        * ``database.available`` — false is survivable (telemetry still archives), but
          it means rollups and events are degraded.
        """
        backlog = gateway.manifest.backlog_age_seconds()
        db_health = await gateway.db.health()
        db_size_mb = db_health.get("database_size_mb") or 0
        db_quota_mb = 15360  # 15 GB quota
        try:
            total_b, used_b, free_b = shutil.disk_usage("/")
            total_gb = round(total_b / (1024**3), 1)
            used_gb = round(used_b / (1024**3), 1)
            free_gb = round(free_b / (1024**3), 1)
            percent_used = round((used_b / total_b) * 100, 1) if total_b > 0 else 0
        except Exception:
            total_gb, used_gb, free_gb, percent_used = 0, 0, 0, 0

        storage_info = {
            "database": {
                "used_mb": db_size_mb,
                "quota_mb": db_quota_mb,
                "display": f"{db_size_mb} MB / 15 GB",
                "percent_used": round((db_size_mb / db_quota_mb) * 100, 1) if db_quota_mb > 0 else 0,
            },
            "system": {
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "percent_used": percent_used,
                "display": f"{used_gb} GB / {total_gb} GB ({free_gb} GB libres)",
            },
        }

        return {
            "status": "ok",
            "ingest": gateway.ingest.stats(),
            "archive": gateway.archive.snapshot(),
            "rollup": gateway.rollup.stats(),
            "events": gateway.events.stats(),
            "commands": gateway.commands.stats(),
            "database": db_health,
            "storage": storage_info,
            "hardware": gateway.hardware.snapshot(),
            "flush_backlog_s": int(backlog) if backlog is not None else None,
            "live_subscribers": gateway.live.subscriber_count,
        }

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        """JSON metrics. Block-write count is the SD-health canary."""
        return {
            "archive": gateway.archive.snapshot(),
            "ingest": gateway.ingest.stats(),
            "rollup": gateway.rollup.stats(),
            "events": gateway.events.stats(),
            "derived": gateway.derived.stats(),
            "hardware": gateway.hardware.snapshot(),
            "live_subscribers": gateway.live.subscriber_count,
        }

    # -- archive -----------------------------------------------------------

    @app.post("/api/archive/rotate")
    async def archive_rotate() -> dict[str, Any]:
        """
        Close any open archive file whose day has ended.

        Called by the rotation process before it compresses anything. The coordination
        rule is that the gateway writes only to records with ``closed_at IS NULL`` and
        every other process touches only records with ``closed_at IS NOT NULL``, so
        rotation cannot begin until this has run.

        The rotation process asks rather than acting directly because the gateway owns
        the file handles and is the only thing that knows whether a write is in flight.

        Two complementary halves:

        * ``rotate_if_needed`` closes handles this process currently holds;
        * ``finalise_stale`` closes records whose day ended but which have no open
          handle here — a role that received no traffic since midnight, for instance.
        """
        today = datetime.now(tz=ZoneInfo(settings.timezone)).date()
        closed_handles = await gateway.archive.rotate_if_needed(today=today)
        closed_records = finalise_stale(gateway.manifest, today=today, timezone=settings.timezone)

        return {
            "today": today.isoformat(),
            "rotated": [str(handle.path) for handle in closed_handles],
            "finalised": [str(path) for path in closed_records],
        }

    # -- live --------------------------------------------------------------

    @app.get("/api/live")
    async def live() -> dict[str, Any]:
        """Current state of every node. Reads memory only."""
        return gateway.live.snapshot()

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        """
        Server-sent events at the configured cadence (default 1 Hz).

        Replaces four polling endpoints per open tab. The user confirmed 1 Hz is
        sufficient for the dashboard, which is what makes this cheap: no per-sample
        fan-out, no buffering beyond one snapshot.
        """
        interval = 1.0 / max(settings.live_sse_hz, 0.01)

        async def event_source() -> AsyncIterator[str]:
            yield f"retry: {int(interval * 4000)}\n\n"
            async for frame in gateway.live.stream(interval_s=interval):
                if await request.is_disconnected():
                    break
                yield frame

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",  # belt and braces alongside Caddy's flush_interval -1
                "Connection": "keep-alive",
            },
        )

    # -- registry ----------------------------------------------------------

    @app.get("/api/roles")
    async def roles() -> dict[str, Any]:
        """
        The schema registry, exposed so the UI renders columns from data.

        This is what makes a fifth node type a YAML edit rather than a frontend change.
        Two additions exist purely for the dashboard and are derived, never hand-written:

        * per-field ``aggregates`` — which rollup columns this field contributes to, so
          the chart picker is built from what the database can actually answer;
        * per-role ``expected_rate_hz`` — the declared cadence per mode, which is what
          lets the UI decide whether a node's silence is normal or a fault.
        """
        return {
            "schema_version": gateway.registry.version,
            "max_batch": gateway.registry.max_batch,
            "roles": {
                schema.role: {
                    "display_name": schema.display_name,
                    "node_id": schema.node_id,
                    "topic": schema.topic,
                    "authoritative_for": list(schema.authoritative_for),
                    "expected_rate_hz": dict(schema.expected_rate_hz or {}),
                    "fields": [
                        {
                            "name": f.name,
                            "type": f.type.value,
                            "unit": f.unit,
                            "precision": f.precision,
                            "values": list(f.values),
                            "authoritative": f.authoritative,
                            "note": f.note,
                            "aggregates": _AGGREGATES_BY_FIELD.get(f.name, []),
                        }
                        for f in schema.fields
                    ],
                }
                for schema in gateway.registry.all_roles()
            },
        }

    # -- commands ----------------------------------------------------------

    @app.post("/api/commands")
    async def post_command(body: CommandRequest) -> dict[str, Any]:
        """
        Issue an operator command.

        Recorded in the outbox *before* publishing, so the intent survives a broker
        restart. Issuing a setpoint atomically supersedes any earlier pending setpoint
        for the same role — the fix for the previous system's silent supersession bug,
        where a queued orchestration command could undo a manual override.

        An ``emergency_stop`` bypasses the database requirement entirely: blocking a
        stop command on a database write would be the wrong trade by a wide margin.
        """
        if body.role not in gateway.registry.roles:
            raise HTTPException(status_code=404, detail=f"unknown role {body.role!r}")

        schema = gateway.registry.for_role(body.role)
        try:
            record = await gateway.commands.issue(
                role=body.role,
                action=body.action,
                target=body.target,
                experiment_id=body.experiment_id,
                requested_by=body.requested_by,
                deadline_s=body.deadline_s,
                node_id=schema.node_id,
            )
        except CommandError as exc:
            # 409 for a state conflict, 400 for a malformed command — the distinction
            # matters to a UI deciding whether to retry.
            status = 409 if exc.code == "database_unavailable" else 400
            raise HTTPException(status_code=status, detail=f"{exc.code}: {exc.detail}") from exc

        return record.as_dict()

    @app.get("/api/commands")
    async def list_commands(
        role: str | None = None,
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> dict[str, Any]:
        """Recent outbox entries, newest first. Answers 'did my command land?'"""
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="command history unavailable: database offline")
        return {
            "commands": await gateway.commands.recent(limit=limit, role=role),
            "stats": gateway.commands.stats(),
        }

    # -- events ------------------------------------------------------------

    @app.get("/api/events")
    async def events(
        experiment_id: str | None = None,
        node_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        scope: str | None = None,
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> dict[str, Any]:
        """
        Unified event stream, newest first.

        Filter keys and values are bound as parameters — the whitelist lives in
        ``events/stream.py``. A query assembled from user input is how a read endpoint
        quietly becomes a write endpoint.
        """
        if not gateway.db.available:
            # Degraded, not empty. Returning [] would look like "nothing happened",
            # which is a very different claim from "we cannot tell you".
            raise HTTPException(status_code=503, detail="event stream unavailable: database offline")

        filters = {
            "experiment_id": experiment_id,
            "node_id": node_id,
            "event_type": event_type,
            "severity": severity,
            "scope": scope,
        }
        try:
            rows = await gateway.events.recent(limit=limit, filters=filters)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"events": rows, "limit": limit, "filters": {k: v for k, v in filters.items() if v is not None}}

    @app.get("/api/events/types")
    async def event_types() -> dict[str, Any]:
        """
        The catalogue, for UI pickers.

        Validating event types at the boundary is why an unknown type becomes a
        ``protocol_error`` rather than a silently stored string.
        """
        return {
            "types": [t.value for t in EventType],
            "severities": [s.value for s in Severity],
            "scopes": [s.value for s in Scope],
        }

    @app.get("/api/events/levels")
    async def event_levels() -> dict[str, Any]:
        """
        Latched rule levels, plus the thresholds behind them.

        The levels answer 'why has this alert gone quiet?' — a latched rule stays quiet
        until it recovers past the deadband, so silence is not the same as health.

        The thresholds ride along because the dashboard draws its oxygen gauge bands from
        the same numbers the rules fire on. Duplicating them in the frontend is exactly
        how a gauge ends up disagreeing with the alert sitting next to it.
        """
        thresholds = gateway.derived.thresholds
        return {
            "levels": gateway.derived.current_levels(),
            "thresholds": {
                "do_warning_mg_l": thresholds.do_warning_mg_l,
                "do_critical_mg_l": thresholds.do_critical_mg_l,
                "do_recovery_deadband_mg_l": thresholds.do_recovery_deadband_mg_l,
                "do_drop_rate_mg_l_per_min": thresholds.do_drop_rate_mg_l_per_min,
                "do_rate_window_ms": thresholds.do_rate_window_ms,
                "do_jump_mg_l": thresholds.do_jump_mg_l,
                "temp_warning_c": thresholds.temp_warning_c,
                "battery_warning_v": thresholds.battery_warning_v,
            },
        }

    # -- history -----------------------------------------------------------

    @app.get("/api/history")
    async def history(
        role: str,
        metric: str,
        range: str = Query(default="24h", pattern=r"^\d+(s|m|h|d)$"),
    ) -> dict[str, Any]:
        """
        Chart data, read from rollups only — never from raw telemetry.

        A 24-hour chart over 1 Hz raw data is 86,400 rows; over 1-minute rollups it is
        1,440. On a Pi 3B+ that difference is CPU, and on an SD card it is read
        amplification. That is the entire reason the rollup tier exists.

        The metric name is validated against the declared aggregate columns rather than
        interpolated, so a crafted ``metric`` cannot reach the query text.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="history unavailable: database offline")
        if role not in gateway.registry.roles:
            raise HTTPException(status_code=404, detail=f"unknown role {role!r}")

        column = _resolve_metric_column(metric)
        table, window = _resolve_range(range)
        accounting = _accounting_columns(table)

        query = (
            f"SELECT bucket, {column} AS value, {accounting} "
            f"FROM {table} WHERE role = $1 AND bucket > now() - $2::interval "
            f"AND bucket <= now() + interval '5 minutes' "
            f"AND {column} IS NOT NULL ORDER BY bucket ASC"
        )
        rows = await gateway.db.fetch(query, role, window)
        return {
            "role": role,
            "metric": metric,
            "column": column,
            "range": range,
            "source": table,
            "data": rows,
        }

    # -- raw samples -------------------------------------------------------

    @app.get("/api/samples")
    async def samples(
        role: str,
        metric: str,
        range: str = Query(default="10m", pattern=r"^\d+(s|m|h|d)$"),
        limit: int = Query(default=20_000, ge=1, le=200_000),
    ) -> dict[str, Any]:
        """
        Raw samples from the archive: every point, not one per minute.

        ``/api/history`` deliberately answers from the rollup tier, downsampled to one
        row per minute, which is the right answer for a 24-hour chart and the wrong one
        for a run. A 5 Hz signal reduced to 60 points an hour is not a coarser view of
        the waveform, it is a different signal.

        Points come from two places and both are needed. The day's CSV holds everything
        already written; the writer's in-memory buffer holds the last ~34 seconds, which
        it will not write until 64 KB accumulate. Reading only the file would drop the
        newest samples -- and data missing from the end of a series is indistinguishable
        from a node that has gone quiet.

        Each point carries the same ``bucket``/``value`` keys ``/api/history`` returns,
        so a chart can move between the two without a second parser. ``truncated`` is
        reported rather than hidden: a caller asking for more points than the limit
        should be told it got a subset, not left to infer it from the count.
        """
        if role not in gateway.registry.roles:
            raise HTTPException(status_code=404, detail=f"unknown role {role!r}")

        schema = gateway.registry.for_role(role)
        if metric not in schema.field_names:
            raise HTTPException(status_code=400, detail=f"unknown metric {metric!r} for {role!r}")

        # Reused for its bounds check (and it is what makes the range a known quantity).
        _, window = _resolve_range(range)

        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        # The window is in the node's clock, and a node slightly ahead of the gateway
        # would otherwise have its newest rows filtered out as "in the future".
        until_ms = now_ms + CLOCK_SKEW_TOLERANCE_MS
        since_ms = until_ms - int(window.total_seconds() * 1000)

        buffered = await gateway.archive.buffered_rows(role)
        buffered_points = parse_metric_lines(buffered, schema, metric, since_ms=since_ms, until_ms=until_ms)

        file_points = read_metric_window(
            gateway.archive.root,
            schema,
            metric=metric,
            since_ms=since_ms,
            until_ms=until_ms,
            limit=limit,
            timezone_name=gateway.settings.timezone,
        )

        # A flush landing between the two reads would put the same rows in both. Cutting
        # the file at the oldest buffered sample makes the join exact without depending
        # on when the flush happened.
        if buffered_points:
            cutoff = buffered_points[0][0]
            file_points = [point for point in file_points if point[0] < cutoff]

        merged = sorted(file_points + buffered_points)
        truncated = len(merged) > limit

        return {
            "role": role,
            "metric": metric,
            "range": range,
            "source": "archive",
            "count": min(len(merged), limit),
            "truncated": truncated,
            "data": [{"bucket": _iso_utc(t_dev_ms), "value": value} for t_dev_ms, value in merged[-limit:]],
        }

    # -- experiments -------------------------------------------------------

    @app.get("/api/experiments")
    async def list_experiments(
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        """
        Experiment registry, newest first.

        Promoted out of the old JSONB settings blob into a real table, so the export can
        join on it and retention can reference it by key.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="experiments unavailable: database offline")
        query = """
            SELECT id, title, status, roles, started_at, ended_at, created_at
              FROM experiments
             WHERE ($1::text IS NULL OR status = $1)
             ORDER BY COALESCE(started_at, created_at) DESC
             LIMIT $2
        """
        return {"experiments": await gateway.db.fetch(query, status, limit)}

    @app.post("/api/experiments")
    async def create_experiment(body: ExperimentCreateRequest) -> dict[str, Any]:
        """
        Create an experiment.

        Needed because ``control_commands.experiment_id`` is a foreign key and nothing in
        the gateway could insert into ``experiments`` -- the table existed, the export joined
        on it, and it was permanently empty. So ``start_experiment`` could not be issued
        through the API at all: every attempt raised a foreign-key violation and returned a
        bare 500. Creating the experiment is the missing first half of that flow.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="experiments unavailable: database offline")

        for role in body.roles:
            if role not in gateway.registry.roles:
                raise HTTPException(status_code=404, detail=f"unknown role {role!r}")

        recipe_id = body.recipe_id or "planta_2_step"
        if recipe_id not in RECIPES:
            raise HTTPException(status_code=400, detail=f"unknown recipe {recipe_id!r}; valid: {sorted(RECIPES)}")

        # The recipe id rides in ``notes`` as a token rather than a dedicated column. The
        # experiments table predates recipes and is shared with the legacy flow; adding a
        # column for one reader would be a migration whose only beneficiary is this line.
        notes = body.notes or ""
        if "recipe:" not in notes:
            notes = f"{notes} recipe:{recipe_id}".strip()

        day = datetime.now(tz=ZoneInfo(gateway.settings.timezone)).date()
        if body.id is None:
            rows = await gateway.db.fetch("SELECT id FROM experiments WHERE id LIKE $1", f"EXP-{day.isoformat()}-%")
            experiment_id = _next_experiment_id(day, [str(row["id"]) for row in rows])
        else:
            experiment_id = body.id
            existing = await gateway.db.fetchval("SELECT 1 FROM experiments WHERE id = $1", experiment_id)
            if existing:
                now_suffix = datetime.now(tz=ZoneInfo(gateway.settings.timezone)).strftime("%H%M%S")
                experiment_id = f"{experiment_id}-{now_suffix}"

        row = await gateway.db.fetchrow(
            """
            INSERT INTO experiments (id, title, roles, hypothesis, notes, created_by)
                 VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id) DO NOTHING
              RETURNING id, title, status, roles, started_at, ended_at, created_at
            """,
            experiment_id,
            body.title,
            body.roles,
            body.hypothesis,
            notes,
            body.created_by,
        )
        if row is None:
            # CONFLICT DO NOTHING rather than an exception, so a duplicate id is a clear 409
            # instead of the driver's error text leaking through as a 500.
            raise HTTPException(status_code=409, detail=f"experiment {experiment_id!r} already exists")

        # Seed the phase plan up front, so the dashboard can show what the run *will* do
        # before it starts, and so a run that never starts still records its intended phases.
        # A phase the operator pre-set before launch uses their mode, not the recipe default;
        # that choice arriving here is what makes a pre-launch edit survive to the run.
        recipe = RECIPES[recipe_id]
        valid_modes = {"auto", "hibrido", "manual"}
        for phase_id, mode in body.phase_modes.items():
            if mode not in valid_modes:
                raise HTTPException(
                    status_code=400,
                    detail=f"phase {phase_id!r}: bad mode {mode!r}; valid: {sorted(valid_modes)}",
                )
        await gateway.db.executemany(
            """
            INSERT INTO experiment_phases (experiment_id, phase_id, phase_num, mode, status, params)
            VALUES ($1, $2, $3, $4, 'pending', $5::jsonb)
            ON CONFLICT (experiment_id, phase_id) DO NOTHING
            """,
            [
                (
                    experiment_id,
                    phase.phase_id,
                    phase.num,
                    body.phase_modes.get(phase.phase_id, phase.mode),
                    json.dumps({**dict(phase.params), **body.phase_params.get(phase.phase_id, {})}),
                )
                for phase in recipe.phases
            ],
        )

        await gateway.events.emit(
            Event.make(
                EventType.EXPERIMENT_CREATED,
                source=Source.UI,
                experiment_id=experiment_id,
                detail={"recipe_id": recipe_id, "created_by": body.created_by},
            ),
            live=gateway.live,
        )
        return dict(row)

    @app.post("/api/experiments/{experiment_id}/milestones")
    async def record_milestone(experiment_id: str, body: MilestoneRequest) -> dict[str, Any]:
        """
        Record an operator-documented point on the run timeline.

        Stored as an event, not its own table. That choice buys three things at once: the
        milestone appears in the unified event stream the dashboard already reads, the export
        bundle — which merges events — carries it with no extra code, and there is no second
        store to keep consistent with the first.

        ``t_rel_s`` is computed here and stored, so the chart can place the milestone on the
        run's relative-time axis without every client having to know ``started_at``. That is
        the one piece of derived data a milestone needs, and computing it once at the point
        where ``started_at`` is known is cheaper and less error-prone than in each browser.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="milestones unavailable: database offline")

        row = await gateway.db.fetchrow("SELECT started_at, status FROM experiments WHERE id = $1", experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")

        t_rel_s: float | None = None
        started_at = row.get("started_at")
        if started_at is not None:
            # ``started_at`` may come back as a tz-aware datetime (asyncpg) or a string,
            # depending on the codec; normalise before subtracting.
            if isinstance(started_at, datetime):
                t_rel_s = round((datetime.now(tz=timezone.utc) - started_at).total_seconds(), 1)

        detail: dict[str, Any] = {
            "kind": body.kind,
            "label": body.label,
            "detail": body.detail,
            "t_rel_s": t_rel_s,
        }
        # The oxygen value at the moment of the annotation is what makes the dot on the
        # chart sit on the curve rather than at an arbitrary height.
        do_now = gateway.live.snapshot().get("nodes", {}).get("od_sensor", {}).get("metrics", {})
        if isinstance(do_now.get("od_do_mg_l"), (int, float)):
            detail["do_mg_l"] = float(do_now["od_do_mg_l"])

        event = Event.make(
            EventType.MILESTONE_RECORDED,
            source=Source.UI,
            experiment_id=experiment_id,
            t_dev_ms=body.at_t_dev_ms,
            action=body.kind,
            detail=detail,
        )
        await gateway.events.emit(event, live=gateway.live)
        return {"event_id": event.event_id, "experiment_id": experiment_id, **detail}

    @app.get("/api/experiments/{experiment_id}/milestones")
    async def list_milestones(experiment_id: str, limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
        """
        The run's milestones, oldest first, for the chart overlay and the timeline list.

        A thin filter over the event stream rather than a table of its own — the convenience
        is the ordering and the shape, not a second source of truth.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="milestones unavailable: database offline")
        rows = await gateway.events.recent(
            limit=limit,
            filters={"experiment_id": experiment_id, "event_type": EventType.MILESTONE_RECORDED.value},
        )
        # ``recent`` returns newest-first; the timeline reads oldest-first.
        rows.reverse()
        return {"experiment_id": experiment_id, "milestones": rows}

    @app.get("/api/experiments/{experiment_id}/preview")
    async def experiment_preview(experiment_id: str) -> dict[str, Any]:
        """
        Lightweight preview of an experiment's DO trendline.

        Used by the export modal and experiment picker so the operator can inspect the
        oxygen trajectory before downloading or running an analysis.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="database offline")

        exp = await gateway.db.fetchrow(
            "SELECT id, title, status, started_at, ended_at, created_at, notes FROM experiments WHERE id = $1",
            experiment_id,
        )
        if exp is None:
            raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")

        pts: list[dict[str, Any]] = []
        started = exp["started_at"] or exp["created_at"]
        ended = exp["ended_at"] or datetime.now(tz=timezone.utc)
        start_ms = int(started.timestamp() * 1000) if started and hasattr(started, "timestamp") else None
        end_ms = int(ended.timestamp() * 1000) if ended and hasattr(ended, "timestamp") else None

        # 1. Check if an export CSV is already on disk (fastest, pre-aligned)
        for target_name in (experiment_id, f"{experiment_id}_native"):
            target_dir = gateway.settings.export_root / target_name
            if target_dir.is_dir():
                csv_file = target_dir / "telemetry.csv"
                if not csv_file.is_file():
                    csv_file = target_dir / "telemetry_native.csv"
                if not csv_file.is_file():
                    csv_file = next((f for f in target_dir.glob("*.csv") if "telemetry" in f.name), None)
                if csv_file and csv_file.is_file():
                    try:
                        import csv
                        with open(csv_file, "r", encoding="utf-8", errors="replace") as f:
                            reader = csv.DictReader(f)
                            matching_rows = []
                            for r in reader:
                                val = r.get("od_do_mg_l")
                                if val in (None, ""):
                                    continue
                                try:
                                    do_f = float(val)
                                except (TypeError, ValueError):
                                    continue

                                # Resolve sample timestamp
                                ts_ms = None
                                if r.get("t_dev_ms"):
                                    try:
                                        ts_ms = int(float(r["t_dev_ms"]))
                                    except (TypeError, ValueError):
                                        pass
                                elif r.get("timestamp_utc"):
                                    try:
                                        ts_ms = int(datetime.fromisoformat(r["timestamp_utc"]).timestamp() * 1000)
                                    except Exception:
                                        pass

                                # Filter strictly within the experiment window if timestamp is present
                                if ts_ms is not None:
                                    if start_ms is not None and ts_ms < (start_ms - 5_000):
                                        continue
                                    if end_ms is not None and ts_ms > (end_ms + 5_000):
                                        continue
                                    t_rel = max(0, int((ts_ms - start_ms) / 1000)) if start_ms else len(matching_rows)
                                    matching_rows.append({
                                        "t": r.get("timestamp_utc") or _iso_utc(ts_ms),
                                        "t_rel_s": t_rel,
                                        "do": round(do_f, 3),
                                    })
                                else:
                                    matching_rows.append({
                                        "t": str(len(matching_rows)),
                                        "t_rel_s": len(matching_rows),
                                        "do": round(do_f, 3),
                                    })

                            if matching_rows:
                                step = max(1, len(matching_rows) // 100)
                                pts = matching_rows[::step]
                                break
                    except Exception:
                        pts = []

        # 2. Check rollups by experiment_id
        if not pts:
            rollup_rows = await gateway.db.fetch(
                """
                SELECT bucket, od_do_mg_l_avg
                  FROM rollup_1min
                 WHERE experiment_id = $1 AND role = 'od_sensor' AND od_do_mg_l_avg IS NOT NULL
                 ORDER BY bucket ASC
                """,
                experiment_id,
            )
            if rollup_rows:
                t0 = exp["started_at"] or rollup_rows[0]["bucket"]
                t0_ts = t0.timestamp() if isinstance(t0, datetime) else 0.0
                for r in rollup_rows:
                    b = r["bucket"]
                    b_ts = b.timestamp() if isinstance(b, datetime) else 0.0
                    pts.append({
                        "t": b.isoformat() if hasattr(b, "isoformat") else str(b),
                        "t_rel_s": max(0, int(b_ts - t0_ts)),
                        "do": round(float(r["od_do_mg_l_avg"]), 3),
                    })

        # 3. Check rollups by time window (for runs where od_sensor logged with exp=NULL)
        if not pts:
            started = exp["started_at"] or exp["created_at"]
            ended = exp["ended_at"] or datetime.now(tz=timezone.utc)
            if started:
                time_rollup_rows = await gateway.db.fetch(
                    """
                    SELECT bucket, od_do_mg_l_avg
                      FROM rollup_1min
                     WHERE role = 'od_sensor'
                       AND bucket >= $1 AND bucket <= $2
                       AND od_do_mg_l_avg IS NOT NULL
                     ORDER BY bucket ASC
                    """,
                    started,
                    ended,
                )
                if time_rollup_rows:
                    t0_ts = started.timestamp() if isinstance(started, datetime) else 0.0
                    for r in time_rollup_rows:
                        b = r["bucket"]
                        b_ts = b.timestamp() if isinstance(b, datetime) else 0.0
                        pts.append({
                            "t": b.isoformat() if hasattr(b, "isoformat") else str(b),
                            "t_rel_s": max(0, int(b_ts - t0_ts)),
                            "do": round(float(r["od_do_mg_l_avg"]), 3),
                        })

        # 4. Fall back to raw archive window (.csv and .csv.gz)
        if not pts:
            started = exp["started_at"] or exp["created_at"]
            ended = exp["ended_at"] or datetime.now(tz=timezone.utc)
            if started and hasattr(started, "timestamp"):
                since_ms = int(started.timestamp() * 1000)
                until_ms = int(ended.timestamp() * 1000)
                schema = gateway.registry.roles.get("od_sensor")
                if schema and "od_do_mg_l" in schema.field_names:
                    raw_pts = read_metric_window(
                        gateway.archive.root,
                        schema,
                        metric="od_do_mg_l",
                        since_ms=since_ms,
                        until_ms=until_ms,
                        limit=2000,
                        timezone_name=gateway.settings.timezone,
                    )
                    step = max(1, len(raw_pts) // 100)
                    for t_ms, val in raw_pts[::step]:
                        pts.append({
                            "t": _iso_utc(t_ms),
                            "t_rel_s": max(0, int((t_ms - since_ms) / 1000)),
                            "do": round(float(val), 3),
                        })

        do_vals = [p["do"] for p in pts if p.get("do") is not None]
        min_do = min(do_vals) if do_vals else None
        max_do = max(do_vals) if do_vals else None
        duration_s = pts[-1]["t_rel_s"] if pts else 0

        return {
            "experiment_id": experiment_id,
            "title": exp["title"],
            "status": exp["status"],
            "started_at": exp["started_at"].isoformat() if exp["started_at"] else None,
            "ended_at": exp["ended_at"].isoformat() if exp["ended_at"] else None,
            "points": pts,
            "count": len(pts),
            "min_do": min_do,
            "max_do": max_do,
            "duration_s": duration_s,
        }

    @app.post("/api/experiments/{experiment_id}/export")
    async def export_experiment(
        experiment_id: str,
        grid_seconds: float = Query(default=1.0, gt=0, le=60),
        compress: bool = True,
    ) -> dict[str, Any]:
        """
        Build the combined, time-aligned bundle.

        Assembled on demand rather than at write time: you cannot know which nodes will
        participate or how they should be joined until the experiment is over. Re-running
        it is safe and produces identical columns.

        Returned inline as a report rather than streamed — a bundle for a three-hour
        experiment is a few megabytes, and the caller (the export CLI, or the dashboard)
        reads the file from disk or syncs it to object storage itself.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="export unavailable: database offline")

        out_dir = gateway.settings.export_root / experiment_id
        try:
            report = await build_bundle(
                gateway.settings,
                gateway.manifest,
                gateway.db,
                gateway.registry,
                experiment_id,
                out_dir=out_dir,
                grid_seconds=grid_seconds,
                compress=compress,
            )
        except ExportError as exc:
            detail = str(exc)
            status = 404 if detail.startswith("unknown experiment") else 422
            raise HTTPException(status_code=status, detail=detail) from exc

        return {
            "experiment_id": report.experiment_id,
            "out_dir": str(report.out_dir),
            "bundle": str(report.bundle_path) if report.bundle_path else None,
            "rows": report.rows,
            "columns": report.columns,
            "events": report.events,
            "grid_seconds": report.grid_seconds,
            "roles_present": report.roles_present,
            "roles_missing": report.roles_missing,
            "warnings": report.warnings,
        }

    @app.post("/api/experiments/{experiment_id}/export/native")
    async def export_experiment_native(
        experiment_id: str,
        compress: bool = True,
    ) -> dict[str, Any]:
        """
        Build the native-rate export: every sample, as recorded, plus the events.

        Distinct from the aligned bundle above on purpose. That one resamples every role
        onto a shared grid — the right shape for plotting several nodes on one time axis,
        and lossy by construction. This one keeps the raw cadence: a 5 Hz signal stays 5 Hz,
        nothing is carried forward, and a gap stays a gap. For a thesis dataset, this is the
        record of what happened; the aligned bundle is one interpretation of it.

        JSON in long format, not a pivot: each row carries its own role and t_dev_ms.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="export unavailable: database offline")

        out_dir = gateway.settings.export_root / f"{experiment_id}_native"
        try:
            report = await build_flat_export(
                gateway.settings,
                gateway.manifest,
                gateway.db,
                gateway.registry,
                experiment_id,
                out_dir=out_dir,
                compress=compress,
            )
        except FlatExportError as exc:
            detail = str(exc)
            status = 404 if detail.startswith("unknown experiment") else 422
            raise HTTPException(status_code=status, detail=detail) from exc

        return {
            "experiment_id": report.experiment_id,
            "out_dir": str(report.out_dir),
            "bundle": str(report.bundle_path) if report.bundle_path else None,
            "format": "native",
            "rows": report.rows,
            "columns": report.columns,
            "events": report.events,
            "per_role_rows": report.per_role_rows,
            "roles_present": report.roles_present,
            "roles_missing": report.roles_missing,
            "warnings": report.warnings,
        }

    @app.get("/api/experiments/{experiment_id}/exports")
    async def list_experiment_exports(experiment_id: str) -> dict[str, Any]:
        """
        Exports already on disk for an experiment, newest first.

        Reads the filesystem, not a table: an export is derived and re-buildable, so a row
        recording it would be bookkeeping that can disagree with what is actually there. The
        directory is the source of truth, and an unreadable manifest degrades to an entry with
        unknown metadata rather than hiding the export.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="exports unavailable: database offline")
        row = await gateway.db.fetchrow("SELECT 1 FROM experiments WHERE id = $1", experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")
        exports = list_exports(gateway.settings.export_root, experiment_id)
        return {"experiment_id": experiment_id, "exports": exports}

    @app.api_route("/api/experiments/{experiment_id}/download", methods=["GET", "HEAD"])
    async def download_experiment(
        experiment_id: str,
        format: str = Query(default="native", pattern="^(native|aligned)$"),
    ) -> FileResponse:
        """
        Download the export bundle (.tar.gz) or CSV for an experiment.

        Returns the compressed bundle with Content-Disposition attachment header
        so the browser saves the file directly. If not yet generated, it builds it on the fly.
        """
        if not gateway.db.available:
            raise HTTPException(status_code=503, detail="database offline")

        suffix = "_native" if format == "native" else ""
        target_dir = gateway.settings.export_root / f"{experiment_id}{suffix}"

        bundle = next(iter(sorted(target_dir.glob("*.tar.gz"))), None) if target_dir.is_dir() else None
        csv_file = next(iter(sorted(target_dir.glob("*.csv"))), None) if target_dir.is_dir() else None

        if bundle is None and csv_file is None:
            # Build on the fly if not already on disk
            try:
                if format == "native":
                    report = await build_flat_export(
                        gateway.settings,
                        gateway.manifest,
                        gateway.db,
                        gateway.registry,
                        experiment_id,
                        out_dir=target_dir,
                        compress=True,
                    )
                    if report.bundle_path and report.bundle_path.is_file():
                        bundle = report.bundle_path
                else:
                    report = await build_export_bundle(
                        gateway.settings,
                        gateway.manifest,
                        gateway.db,
                        gateway.registry,
                        experiment_id,
                        out_dir=target_dir,
                        grid_seconds=1.0,
                        compress=True,
                    )
                    if report.bundle_path and report.bundle_path.is_file():
                        bundle = report.bundle_path
            except (FlatExportError, ExportError) as exc:
                detail = str(exc)
                status = 404 if detail.startswith("unknown experiment") else 422
                raise HTTPException(status_code=status, detail=detail) from exc

        if bundle is not None and bundle.is_file():
            return FileResponse(
                path=bundle,
                filename=bundle.name,
                media_type="application/gzip",
            )

        if csv_file is not None and csv_file.is_file():
            return FileResponse(
                path=csv_file,
                filename=csv_file.name,
                media_type="text/csv",
            )

        raise HTTPException(status_code=404, detail=f"no download file found in export for {experiment_id!r}")

    # -- orchestration -----------------------------------------------------

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        """
        Current orchestrator state, as the dashboard header renders it.

        Reads the engine's live view rather than ``system_settings`` so the phase and the
        relative timer are current. ``system_state`` is the durable record the engine
        reconciles into; this is the fast path the UI polls.
        """
        return await gateway.orchestrator.current_state()

    @app.post("/api/state")
    async def post_state(body: StateRequest) -> dict[str, Any]:
        """
        Orchestrator transition: IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE.

        The route delegates to the engine rather than writing ``experiments.status``
        itself, so there is exactly one writer for a run's lifecycle. A button and the
        engine's own tick can never race: the engine serialises both.
        """
        try:
            return await gateway.orchestrator.request_state(
                state=body.state,
                experiment_id=body.experiment_id,
                requested_by=body.requested_by,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/state/confirm")
    async def post_state_confirm(requested_by: str = Query(default="operator:web")) -> dict[str, Any]:
        """
        Operator's visto bueno on a HIBRIDO phase.

        Advancing a supervised phase is a distinct action from flipping the global
        state, because the operator is answering a specific prompt rather than starting
        or stopping the run.
        """
        return await gateway.orchestrator.confirm_phase(requested_by=requested_by)

    @app.post("/api/state/phase-action")
    async def post_phase_action(
        request: Request,
        requested_by: str = Query(default="operator:web"),
    ) -> dict[str, Any]:
        """
        Operator fires the active MANUAL phase's entry commands ("Disparar Acción").

        A MANUAL phase no longer issues its commands on entry -- that would be the automatic
        action the mode forbids -- so they are held until the operator asks for them here. A
        no-op unless a manual phase is actually waiting, so a stray click is harmless.
        """
        params_override: dict[str, Any] | None = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                params_override = body.get("params")
        except Exception:
            pass
        return await gateway.orchestrator.trigger_phase_action(
            requested_by=requested_by,
            params_override=params_override,
        )

    @app.post("/api/state/phase-params")
    async def post_phase_params(request: Request) -> dict[str, Any]:
        """Override parameters for a phase (e.g. mixer_rpm, step_rpm)."""
        body = await request.json()
        if not isinstance(body, dict) or "phase_id" not in body:
            raise HTTPException(status_code=400, detail="missing phase_id")
        phase_id = str(body["phase_id"])
        params = body.get("params") or {}
        if isinstance(params, dict):
            gateway.orchestrator.set_phase_params(phase_id, params)
        return await gateway.orchestrator.current_state()

    @app.post("/api/state/pause")
    async def post_state_pause(requested_by: str = Query(default="operator:web")) -> dict[str, Any]:
        """
        Pause the run: hold the timer and stop phase advancement, without disarming.

        Distinct from ``POST /api/state {IDLE}``, which is a stop and does disarm. Keeping
        them separate is deliberate — pause is supervisory, stop is a physical shutdown.
        """
        return await gateway.orchestrator.pause(requested_by=requested_by)

    @app.post("/api/state/resume")
    async def post_state_resume(requested_by: str = Query(default="operator:web")) -> dict[str, Any]:
        """Resume a paused run; the relative timer excludes the paused span."""
        return await gateway.orchestrator.resume(requested_by=requested_by)

    @app.post("/api/state/phase-mode")
    async def post_phase_mode(
        phase_id: str,
        mode: str,
        experiment_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """
        Override a phase's mode (auto | hibrido | manual).

        Without ``experiment_id`` this targets the running experiment, as before. With it, the
        call prepares a phase plan *before* launch: the mode is written to
        ``experiment_phases.mode`` and read back by ``_begin`` when the run starts. That is the
        pre-launch edit the UI used to disable.

        Persisting is best-effort but the mode is validated first, so a typo is a 400 rather than
        a silently dropped write.
        """
        try:
            return await gateway.orchestrator.set_phase_mode(
                phase_id=phase_id,
                mode=mode,
                experiment_id=experiment_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/recipes")
    async def list_recipes() -> dict[str, Any]:
        """
        The recipe catalogue, for the phase-plan picker.

        Declarative data, so the picker is built from what the engine can actually run
        rather than from a second copy of the phase plans in the UI.
        """
        return {
            "recipes": [
                {
                    "recipe_id": recipe.recipe_id,
                    "title": recipe.title,
                    "filename": recipe.filename,
                    "roles": list(recipe.roles),
                    "phases": [
                        {
                            "phase_id": phase.phase_id,
                            "num": phase.num,
                            "title": phase.title,
                            "desc": phase.desc,
                            "mode": phase.mode,
                            "condition_text": phase.condition_text,
                            "timeout_s": phase.timeout_s,
                            "params": dict(phase.params),
                        }
                        for phase in recipe.phases
                    ],
                }
                for recipe in RECIPES.values()
            ]
        }

    # -- DO closed-loop manual control -------------------------------------

    @app.post("/api/control/do")
    async def post_control_do(body: DOControlRequest) -> dict[str, Any]:
        """
        Manually launch closed-loop DO control with configurable parameters.

        Issues set_config (mode, target_do, kp, ki, kd, beta, tt, tau_lag)
        and arms the aerator motor in closed-loop control mode.
        """
        if "aerator" not in gateway.registry.roles:
            raise HTTPException(status_code=404, detail="aerator role not in registry")

        schema = gateway.registry.for_role("aerator")
        valid_modes = {"pid", "fuzzy"}
        if body.mode not in valid_modes:
            raise HTTPException(status_code=400, detail=f"invalid mode {body.mode!r}; valid: {sorted(valid_modes)}")

        config_payload = {
            "mode": body.mode,
            "target_do": body.setpoint_mg_l,
            "setpoint_mg_l": body.setpoint_mg_l,
            "kp": body.kp,
            "ki": body.ki,
            "kd": body.kd,
            "beta": body.beta,
            "tt": body.tt,
            "tau_lag": body.tau_lag,
        }
        cmd_config = await gateway.commands.issue(
            role="aerator",
            action="set_config",
            target=config_payload,
            experiment_id=body.experiment_id,
            requested_by=body.requested_by,
            node_id=schema.node_id,
        )

        await asyncio.sleep(0.05)

        cmd_start = await gateway.commands.issue(
            role="aerator",
            action="start",
            target={},
            experiment_id=body.experiment_id,
            requested_by=body.requested_by,
            node_id=schema.node_id,
        )

        await gateway.events.emit(
            Event.make(
                EventType.COMMAND_ISSUED,
                source=Source.UI,
                node_id=schema.node_id,
                experiment_id=body.experiment_id,
                detail={
                    "control": "do_closed_loop",
                    "mode": body.mode,
                    "setpoint_mg_l": body.setpoint_mg_l,
                    "kp": body.kp,
                    "ki": body.ki,
                    "kd": body.kd,
                    "beta": body.beta,
                    "tt": body.tt,
                    "tau_lag": body.tau_lag,
                },
            ),
            live=gateway.live,
        )

        return {
            "status": "running",
            "control": "do_closed_loop",
            "mode": body.mode,
            "setpoint_mg_l": body.setpoint_mg_l,
            "params": {
                "kp": body.kp,
                "ki": body.ki,
                "kd": body.kd,
                "beta": body.beta,
                "tt": body.tt,
                "tau_lag": body.tau_lag,
            },
            "commands": {
                "set_config": cmd_config.id,
                "start": cmd_start.id,
            },
        }

    @app.post("/api/control/do/stop")
    async def post_control_do_stop(
        requested_by: str = Query(default="operator:manual"),
        experiment_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Stop and disarm the closed-loop DO control, returning the aerator to manual mode."""
        if "aerator" not in gateway.registry.roles:
            raise HTTPException(status_code=404, detail="aerator role not in registry")

        schema = gateway.registry.for_role("aerator")
        cmd_stop = await gateway.commands.issue(
            role="aerator",
            action="stop",
            target={},
            experiment_id=experiment_id,
            requested_by=requested_by,
            node_id=schema.node_id,
        )
        await asyncio.sleep(0.05)
        cmd_mode = await gateway.commands.issue(
            role="aerator",
            action="set_mode",
            target={"mode": "manual"},
            experiment_id=experiment_id,
            requested_by=requested_by,
            node_id=schema.node_id,
        )
        return {
            "status": "stopped",
            "mode": "manual",
            "commands": {"stop": cmd_stop.id, "set_mode": cmd_mode.id},
        }

    @app.get("/api/control/do")
    async def get_control_do() -> dict[str, Any]:
        """Get the current DO control status, live readings, and default parameter set."""
        snap = gateway.live.snapshot()
        nodes = snap.get("nodes") or {}
        aerator = nodes.get("aerator") or {}
        aerator_metrics = aerator.get("metrics") or {}
        od_sensor = nodes.get("od_sensor") or {}
        od_metrics = od_sensor.get("metrics") or {}

        live_do = od_metrics.get("od_do_mg_l")
        if live_do is None:
            live_do = aerator_metrics.get("peer_do_mg_l")

        return {
            "defaults": {
                "setpoint_mg_l": 6.00,
                "kp": 221.65,
                "ki": 1.915,
                "kd": 0.0,
                "beta": 0.35,
                "tt": 100.0,
                "tau_lag": 30.0,
                "mode": "pid",
            },
            "current": {
                "do_mg_l": live_do,
                "rpm": aerator_metrics.get("drv_rpm"),
                "ctl_mode": aerator_metrics.get("ctl_mode", "manual"),
                "running": aerator_metrics.get("drv_running", False),
                "failsafe": aerator_metrics.get("ctl_failsafe", False),
            },
        }

    return app
