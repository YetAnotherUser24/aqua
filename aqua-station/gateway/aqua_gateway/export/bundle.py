"""
Experiment export — the combined, time-aligned dataset.

Assembled at *export* time, never at write time. You cannot know at write time which
nodes will take part in an experiment or how their streams should be joined, and the
ingest path stays simple and generic as a result. This module is where the joining
happens, on demand and repeatably.

Output layout (see ``docs/DATA-FORMAT.md`` §6)::

    experiments/<exp_id>/
    ├── manifest.json    nodes, schemas, clock offsets, calibration, coverage
    ├── telemetry.csv    WIDE, time-aligned across every role
    ├── events.csv       the event stream for this window
    ├── raw_index.json   archive keys of the per-role source files (references, not copies)
    └── bundle.tar.gz

Three decisions carry most of the weight:

**Last-observation-carried-forward, with a staleness bound.** A wide join needs a value
at every grid point for every role, and telemetry arrives at different rates, so LOCF is
the natural fill. Unbounded LOCF is a trap though: if a node dies at 14:00, its last
reading would be carried to the end of the experiment and an analysis would conclude the
aerator ran at 420 rpm for three hours when in truth it stopped. So a held value expires
after ``max_hold_s`` and the cell goes empty. A gap is honest; a fabricated constant is
not.

**The authoritative source wins, and the echo is kept separately.** Dissolved oxygen
arrives twice — from ``od_sensor``, and echoed on the aerator as ``peer_do_mg_l`` for its
own control loop. Averaging them would corrupt both. ``od_sensor``'s columns are emitted
unprefixed; every other role's are prefixed with the role name.

**Raw files are referenced, not copied.** ``raw_index.json`` holds the archive keys. On
the Supabase free tier that is the difference between an archive that lasts and one that
does not, and the sources are one ``aws s3 cp`` away.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from ..archive.manifest import Manifest
from ..config import Settings
from ..db.engine import Database, DatabaseUnavailable
from ..schema.registry import RoleSchema, SchemaRegistry

log = logging.getLogger("aqua.export")

#: How long a carried-forward value stays valid. Past this the cell is left empty
#: rather than reporting a reading that stopped being true.
DEFAULT_MAX_HOLD_S = 60.0

#: Rules the manifest records, so an aligned dataset can be audited later.
BUNDLE_FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BundleReport:
    experiment_id: str
    out_dir: Path
    bundle_path: Path | None = None
    grid_seconds: float = 1.0
    rows: int = 0
    columns: int = 0
    events: int = 0
    roles_present: list[str] = field(default_factory=list)
    roles_missing: list[str] = field(default_factory=list)
    sources_unavailable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rows > 0 and not self.roles_missing

    def summary(self) -> str:
        return (
            f"{self.experiment_id}: {self.rows} rows x {self.columns} cols, "
            f"{self.events} events, roles={len(self.roles_present)}/{len(self.roles_present) + len(self.roles_missing)}, "
            f"grid={self.grid_seconds:g}s" + (f", warnings={len(self.warnings)}" if self.warnings else "")
        )


class ExportError(RuntimeError):
    """The experiment is unknown, or has no data worth exporting."""


# ---------------------------------------------------------------------------
# Reading the archive
# ---------------------------------------------------------------------------


def _open_text(path: Path) -> Iterator[str]:
    """Open a plain or gzipped archive file as text."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            yield from handle
    else:
        with path.open("r", encoding="utf-8", newline="") as handle:
            yield from handle


def read_archive_csv(path: Path) -> list[dict[str, str]]:
    """
    Read one per-role archive file into dicts.

    Returns raw strings: the export's job is alignment, not coercion, and re-parsing
    numbers here would silently change values the archive deliberately preserved.
    """
    if not path.exists():
        return []
    reader = csv.DictReader(_open_text(path))
    return [row for row in reader]


def role_files(
    root: Path,
    role: str,
    start: datetime,
    end: datetime,
    tz_name: str = "America/Lima",
) -> list[Path]:
    """
    Archive files for a role overlapping a window, oldest first.

    Prefers the compressed form when both exist, because that is the one rotation
    keeps and the one the archive holds.

    The day boundaries are taken in the *archive* timezone, not UTC. A file is named
    by the day of the device timestamps it holds (see `csv_writer`), and those are
    filed in `settings.timezone`. Taking `start.date()` in UTC shifts the lookup by a
    day for any window that begins between 19:00 and midnight Lima — the file exists,
    but under the previous day's name, so the export reports "no telemetry found" for
    data that is plainly on disk.
    """
    directory = root / role
    if not directory.is_dir():
        return []

    tz = ZoneInfo(tz_name)
    found: dict[str, Path] = {}
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    while day <= last:
        stem = f"{day.isoformat()}.csv"
        gz = directory / f"{stem}.gz"
        plain = directory / stem
        if gz.exists():
            found[day.isoformat()] = gz
        elif plain.exists():
            found[day.isoformat()] = plain
        day += timedelta(days=1)

    return [found[key] for key in sorted(found)]


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def effective_ms(row: Mapping[str, Any], offset_ms: int) -> int | None:
    """Device timestamp shifted by the node's measured clock offset."""
    raw = row.get("t_dev_ms")
    if raw in (None, ""):
        return None
    try:
        return int(float(raw)) + offset_ms
    except (TypeError, ValueError):
        return None


def resample_locf(
    rows: Sequence[Mapping[str, Any]],
    grid_ms: Sequence[int],
    *,
    offset_ms: int = 0,
    max_hold_ms: int = int(DEFAULT_MAX_HOLD_S * 1000),
) -> list[Mapping[str, Any] | None]:
    """
    One value per grid point: the most recent sample at or before it.

    Values expire after ``max_hold_ms``. Without that bound a node that died mid-run
    would appear to hold its last reading for the rest of the experiment, which turns a
    gap in the record into a confident, wrong constant.

    Pure and index-based, so it is O(n + m) and testable without files or a database.
    """
    if not grid_ms:
        return []

    timed: list[tuple[int, Mapping[str, Any]]] = []
    for row in rows:
        moment = effective_ms(row, offset_ms)
        if moment is not None:
            timed.append((moment, row))
    timed.sort(key=lambda item: item[0])

    out: list[Mapping[str, Any] | None] = []
    index = 0
    current: tuple[int, Mapping[str, Any]] | None = None

    for point in grid_ms:
        while index < len(timed) and timed[index][0] <= point:
            current = timed[index]
            index += 1
        if current is None or point - current[0] > max_hold_ms:
            out.append(None)
        else:
            out.append(current[1])

    return out


def wide_column(role: str, field_name: str, authoritative_role: str | None) -> str:
    """
    Column name in the wide file.

    The authoritative role's columns stay bare (``od_do_mg_l``); every other role is
    prefixed with its name. That does produce ``pump_pump_dosed_ml``, which reads
    redundantly — accepted deliberately, because a predictable, collision-free rule is
    worth more in a data format than a pretty one, and a rule with exceptions is a rule
    somebody will get wrong.
    """
    if authoritative_role is not None and role == authoritative_role:
        return field_name
    return f"{role}_{field_name}"


def build_grid(start: datetime, end: datetime, grid_seconds: float) -> list[int]:
    """Epoch-ms grid points from start to end inclusive."""
    if grid_seconds <= 0:
        raise ValueError("grid_seconds must be positive")
    step = int(grid_seconds * 1000)
    first = int(start.timestamp() * 1000)
    last = int(end.timestamp() * 1000)
    if last < first:
        return []
    return list(range(first, last + 1, step))


# ---------------------------------------------------------------------------
# Clock offsets
# ---------------------------------------------------------------------------

_OFFSET_SQL = """
SELECT node_id, event_type, ts, detail
  FROM events
 WHERE event_type IN ('clock_resynced', 'clock_offset_exceeded')
   AND ts BETWEEN $1 AND $2
 ORDER BY ts ASC
"""


async def load_clock_offsets(
    db: Database,
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """
    Per-role clock offset applied in this window, in milliseconds.

    Read from the ``clock_resynced`` / ``clock_offset_exceeded`` events the gateway
    emits when it measures a node's offset against its own clock. Storing them as events
    rather than in a dedicated table means no schema change was needed, and the offsets
    become part of the auditable record alongside everything else.

    The *latest* offset in the window wins: it reflects the node's clock as it stood
    when the data was collected, which is the correction the data needs.
    """
    if not db.available:
        return {}
    try:
        rows = await db.fetch(_OFFSET_SQL, start, end)
    except (DatabaseUnavailable, Exception):  # noqa: BLE001
        log.warning("could not read clock offsets; exporting uncorrected")
        return {}

    offsets: dict[str, int] = {}
    for row in rows:
        detail = row.get("detail") or {}
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError:
                continue
        role = detail.get("role")
        offset = detail.get("offset_ms")
        if isinstance(role, str) and isinstance(offset, (int, float)):
            offsets[role] = int(offset)
    return offsets


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def load_events(db: Database, experiment_id: str) -> list[dict[str, Any]]:
    """
    Every event belonging to this experiment, oldest first, so they can be overlaid on the
    series.

    By ``experiment_id``, not by time window: the two differ. See the note in
    ``flat._load_events`` -- a window loses the experiment's own late events (a
    ``command_expired`` fires well after the run ends) and admits foreign ones.
    """
    if not db.available:
        return []
    query = """
        SELECT ts, t_dev_ms, event_type, severity, scope, source, node_id,
               correlation_id, action, detail
          FROM events
         WHERE experiment_id = $1
         ORDER BY ts ASC
    """
    try:
        return await db.fetch(query, experiment_id)
    except (DatabaseUnavailable, Exception):  # noqa: BLE001
        log.warning("could not read events for export")
        return []


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


async def build_bundle(
    settings: Settings,
    manifest: Manifest,
    db: Database,
    registry: SchemaRegistry,
    experiment_id: str,
    *,
    out_dir: Path,
    grid_seconds: float = 1.0,
    max_hold_s: float = DEFAULT_MAX_HOLD_S,
    compress: bool = True,
) -> BundleReport:
    """
    Build one experiment bundle.

    :raises ExportError: when the experiment is unknown or has no telemetry at all.
    """
    experiment = await _load_experiment(db, experiment_id)
    if experiment is None:
        raise ExportError(f"unknown experiment {experiment_id!r}")

    started = _as_datetime(experiment.get("started_at"))
    ended = _as_datetime(experiment.get("ended_at")) or datetime.now(tz=timezone.utc)
    if started is None:
        raise ExportError(f"experiment {experiment_id!r} has no start time; nothing to align")

    report = BundleReport(experiment_id=experiment_id, out_dir=out_dir, grid_seconds=grid_seconds)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Which roles take part: declared roles, plus any registered roles with active archive data,
    # so an actuator that was operated outside the recipe (or during manual steps) is included.
    declared_raw = experiment.get("roles") or []
    declared_roles = [role for role in declared_raw if role in registry.roles]
    roles = list(dict.fromkeys(declared_roles + list(registry.roles)))

    offsets = await load_clock_offsets(db, started, ended)
    grid = build_grid(started, ended, grid_seconds)

    # -- per-role series ---------------------------------------------------
    series: dict[str, list[Mapping[str, Any]]] = {}
    raw_index: dict[str, list[str]] = {}

    for role in roles:
        files = role_files(settings.archive.root, role, started, ended, settings.timezone)
        rows: list[Mapping[str, Any]] = []
        for path in files:
            rows.extend(read_archive_csv(path))

        if not rows:
            if not declared_roles or role in declared_roles:
                report.roles_missing.append(role)
                report.sources_unavailable.append(role)
            log.warning("no local archive data for role", extra={"role": role})
            continue

        series[role] = rows
        report.roles_present.append(role)
        raw_index[role] = _archive_keys(manifest, role, started, ended)

    if not series:
        raise ExportError(
            f"no telemetry found for {experiment_id!r}: every role's local archive window is empty. "
            "Retrieve the compressed files from object storage and retry."
        )

    # -- wide join ---------------------------------------------------------
    header, wide_rows = _build_wide(registry, series, grid, offsets, max_hold_ms=int(max_hold_s * 1000))
    report.columns = len(header)
    report.rows = len(wide_rows)

    telemetry_path = out_dir / "telemetry.csv"
    _write_csv(telemetry_path, header, wide_rows)

    # -- events ------------------------------------------------------------
    events = await load_events(db, experiment_id)
    report.events = len(events)
    events_path = out_dir / "events.csv"
    _write_events(events_path, events)

    # -- manifest ----------------------------------------------------------
    coverage = _coverage(registry, series, started, ended)
    bundle_manifest = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "experiment_id": experiment_id,
        "title": experiment.get("title"),
        "status": experiment.get("status"),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "gateway_schema_version": registry.version,
        "grid_seconds": grid_seconds,
        "max_hold_s": max_hold_s,
        # Recorded because a dose cannot be reconstructed without it, and because a
        # constant that changed mid-series is otherwise invisible in the numbers.
        "calibration": experiment.get("calibration") or registry.calibration,
        "time_range": {"start": started.isoformat(), "end": ended.isoformat()},
        "clock_offsets_ms": offsets,
        "roles_present": report.roles_present,
        "roles_missing": report.roles_missing,
        "sources_unavailable": report.sources_unavailable,
        "coverage": coverage,
        "warnings": report.warnings,
        "notes": [
            "Values are last-observation-carried-forward and expire after max_hold_s; "
            "an empty cell means no reading was current, not a reading of zero.",
            "Dissolved oxygen comes from the authoritative od_sensor role. Other roles' "
            "peer_* columns are that node's own control-loop input and must not be averaged with it.",
            "t_dev_ms in the source archive is preserved verbatim; clock_offsets_ms were "
            "applied only to build this aligned view.",
        ],
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(bundle_manifest, indent=2, default=str), encoding="utf-8")

    raw_index_path = out_dir / "raw_index.json"
    raw_index_path.write_text(json.dumps(raw_index, indent=2), encoding="utf-8")

    # -- bundle ------------------------------------------------------------
    if compress:
        report.bundle_path = out_dir / f"{experiment_id}.tar.gz"
        with tarfile.open(report.bundle_path, "w:gz") as archive:
            for path in (manifest_path, telemetry_path, events_path, raw_index_path):
                archive.add(path, arcname=path.name)

    log.info("bundle built", extra={"summary": report.summary()})
    return report


async def _load_experiment(db: Database, experiment_id: str) -> Mapping[str, Any] | None:
    if not db.available:
        raise ExportError("database unavailable; cannot read the experiment record")
    try:
        return await db.fetchrow(
            "SELECT id, title, status, roles, started_at, ended_at, calibration FROM experiments WHERE id = $1",
            experiment_id,
        )
    except (DatabaseUnavailable, Exception) as exc:  # noqa: BLE001
        raise ExportError(f"could not read experiment {experiment_id!r}: {exc}") from exc


def _build_wide(
    registry: SchemaRegistry,
    series: Mapping[str, Sequence[Mapping[str, Any]]],
    grid: Sequence[int],
    offsets: Mapping[str, int],
    *,
    max_hold_ms: int,
) -> tuple[list[str], list[list[Any]]]:
    """
    Join every role onto the grid.

    Columns keep the registry's declaration order per role, and roles appear in the order
    given, so two exports of the same experiment produce identical columns.

    Each role is resampled **once** over the whole grid. Resampling per grid point would
    be O(grid x rows) per role — for a three-hour experiment at 1 s resolution that is
    billions of operations, which is a difference between seconds and never finishing.
    """
    do_owner = registry.authoritative_role_for("dissolved_oxygen")
    authoritative_role = do_owner.role if do_owner else None

    header = ["grid_ts", "timestamp_utc"]
    columns: list[tuple[str, str]] = []  # (role, source field), parallel to header[2:]

    for role in series:
        schema: RoleSchema = registry.for_role(role)
        for spec in schema.fields:
            header.append(wide_column(role, spec.name, authoritative_role))
            columns.append((role, spec.name))

    sampled: dict[str, list[Mapping[str, Any] | None]] = {
        role: resample_locf(rows, grid, offset_ms=offsets.get(role, 0), max_hold_ms=max_hold_ms)
        for role, rows in series.items()
    }

    rows_out: list[list[Any]] = []
    for index, point in enumerate(grid):
        stamp = datetime.fromtimestamp(point / 1000, tz=timezone.utc).isoformat()
        row_out: list[Any] = [point, stamp]
        for role, field_name in columns:
            source = sampled[role][index]
            # An empty cell means no reading was current — never a zero.
            row_out.append("" if source is None else source.get(field_name, ""))
        rows_out.append(row_out)

    return header, rows_out


def _coverage(
    registry: SchemaRegistry,
    series: Mapping[str, Sequence[Mapping[str, Any]]],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """
    Per-role sample counts and time ranges, so a shortfall is visible in the manifest.

    ``observed_hz`` is samples divided by the span the samples actually cover, *not* by the
    experiment window. ``series`` holds every row read for the role -- the whole day's file, not
    just the rows inside ``[start, end]`` -- so dividing by the window counts hours of samples
    against minutes of window. Measured on a real export this reported 149 Hz for the aerator and
    5.8 Hz for a 0.2 Hz sensor; dividing by each role's own stamp span gives 5.0004 and 0.2004,
    which are the declared rates.

    ``truncated`` asks whether a role's coverage falls short of the window, so it compares the
    stamp span against the window -- that is the one place the window *is* the right denominator.
    """
    window_s = max((end - start).total_seconds(), 1.0)
    coverage: dict[str, Any] = {}
    for role, rows in series.items():
        stamps = [ms for ms in (effective_ms(row, 0) for row in rows) if ms is not None]
        first = datetime.fromtimestamp(min(stamps) / 1000, tz=timezone.utc).isoformat() if stamps else None
        last = datetime.fromtimestamp(max(stamps) / 1000, tz=timezone.utc).isoformat() if stamps else None
        span_s = (max(stamps) - min(stamps)) / 1000 if stamps else 0.0
        schema = registry.roles.get(role)
        declared = None
        if schema and schema.expected_rate_hz:
            declared = min(schema.expected_rate_hz.values())
        coverage[role] = {
            "samples": len(rows),
            "first": first,
            "last": last,
            # Guard the denominator: one sample has no span, and reporting a rate from a zero
            # span is the division that produced the absurd numbers above.
            "observed_hz": round(len(rows) / span_s, 4) if span_s > 0 else None,
            "lowest_declared_hz": declared,
            "truncated": bool(stamps) and span_s < window_s * 0.9,
        }
    return coverage


def _archive_keys(manifest: Manifest, role: str, start: datetime, end: datetime) -> list[str]:
    """Object keys of the source files, taken from the manifest rather than guessed."""
    keys: list[str] = []
    day = start.date()
    wanted = set()
    while day <= end.date():
        wanted.add(day.isoformat())
        day += timedelta(days=1)

    for record in manifest.all_records(include_deleted=True):
        if record.role != role or record.day not in wanted:
            continue
        key = record.archive_key or f"raw/{role}/{record.day}.csv.gz"
        if key not in keys:
            keys.append(key)
    return sorted(keys)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_events(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    header = [
        "ts",
        "t_dev_ms",
        "event_type",
        "severity",
        "scope",
        "source",
        "node_id",
        "correlation_id",
        "action",
        "detail",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for event in events:
            writer.writerow(
                [
                    event.get("ts").isoformat() if isinstance(event.get("ts"), datetime) else event.get("ts", ""),
                    event.get("t_dev_ms", ""),
                    event.get("event_type", ""),
                    event.get("severity", ""),
                    event.get("scope", ""),
                    event.get("source", ""),
                    event.get("node_id", ""),
                    event.get("correlation_id", ""),
                    event.get("action", ""),
                    json.dumps(event.get("detail") or {}, default=str),
                ]
            )


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None
