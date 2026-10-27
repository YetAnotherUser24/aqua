"""
Native-rate export: every sample as it was recorded, one row per sample.

``bundle.py`` answers a different question. It resamples every role onto one grid so a
single time column lines up across nodes — invaluable for plotting multi-node data against a
shared axis, and lossy by construction: a 5 Hz actuator signal on a 1 s grid is 4 of every 5
samples gone, replaced by a carried-forward value.

This module is the other half. It writes what the archive actually holds — one row per sample,
per role, at the rate the node produced it — alongside the events, so a thesis dataset is the
raw record rather than a reconstruction of it. Nothing is resampled, nothing is carried
forward, and no value is ever substituted.

**Long format, not wide.** A wide join needs a shared time base, and producing one at native
rate would mean inventing rows for the role that was not sampling at that instant. Instead each
row carries its own ``role`` and ``t_dev_ms``; the reader pivots if and how it likes. That
keeps the export faithful: a gap stays a gap, and a 5 Hz row is not padded to line up with a
1 Hz one.

The one column that does not exist in the source is ``t_rel_ms``: milliseconds since the run's
``started_at``. It is added here because aligning a dose to a DO rise is the whole point of an
experiment export, and making every downstream tool re-derive ``t0`` from the manifest is how
two analyses end up disagreeing about when the run began.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..archive.manifest import Manifest
from ..db.engine import Database
from ..schema.registry import SchemaRegistry
from .bundle import role_files, wide_column

log = logging.getLogger("aqua.export.flat")


class FlatExportError(ValueError):
    """The export cannot be produced. Carries a human-readable reason."""


@dataclass
class FlatReport:
    """What the export produced, for the caller to render and the manifest to record."""

    experiment_id: str
    out_dir: Path
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    events: int = 0
    roles_present: list[str] = field(default_factory=list)
    roles_missing: list[str] = field(default_factory=list)
    per_role_rows: dict[str, int] = field(default_factory=dict)
    bundle_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


#: Fixed prefix columns of the wide file. The role's own registry fields follow, in registry
#: order. ``t_rel_ms`` is derived; the rest are verbatim from the archive.
_PREFIX_COLUMNS = ("role", "t_dev_ms", "t_rel_ms", "t_src", "seq")

#: Columns that describe the *group* rather than a single reading, so they never overwrite
#: each other when rows merge.
_GROUP_COLUMNS = ("role", "t_dev_ms", "t_rel_ms", "t_src", "seq", "merge_spread_ms")

#: How far apart two samples' stamps may be and still count as one instant.
#:
#: Under the fastest role's own interval, on purpose. The aerator reports every 200 ms, so a
#: window of 200 ms or more would merge two *distinct* aerator readings into one row and drop
#: one of them -- the merge would silently destroy data at exactly the cadence that matters
#: most. 150 ms groups the two nodes' coincident stamps (measured: 124 of the sensor's 125
#: fall inside it) while staying strictly below 200 ms, so no role's consecutive samples can
#: ever collapse. Raising this above the fastest role's period is a data-loss bug, not a
#: tuning choice.
DEFAULT_MERGE_MS = 150


def _read_role_rows(
    root: Path,
    role: str,
    start: datetime,
    end: datetime,
    tz_name: str = "America/Lima",
) -> tuple[list[str], Iterator[dict[str, str]]]:
    """
    Yield the role's raw rows, and the field column order to use.

    Streaming rather than materialising: a 3-hour experiment at 5 Hz is over 50,000 rows per
    role, and holding every role in memory at once is exactly the kind of allocation this
    905 MB box cannot afford. The header of the first non-empty file defines the columns.
    """
    files = role_files(root, role, start, end, tz_name)
    header: list[str] = []
    for path in files:
        for row in _stream_csv(path):
            if not header:
                header = list(row.keys())
            yield row


def _stream_csv(path: Path) -> Iterator[dict[str, str]]:
    """Yield rows from a plain or gzipped CSV, one at a time."""
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        yield from reader


def _write_wide_csv(
    path: Path,
    *,
    root: Path,
    roles: Sequence[str],
    registry: SchemaRegistry,
    columns: Sequence[str],
    started: datetime,
    ended: datetime,
    tz_name: str = "America/Lima",
    t0_ms: int | None,
    merge_ms: int = DEFAULT_MERGE_MS,
    declared: Sequence[str] | None = None,
    authoritative_role: str | None = None,
) -> tuple[dict[str, int], list[str], int]:
    """
    Write one wide row per *instant*, every role's fields as columns.

    Returns ``(rows_per_role, missing_roles, total_rows)``.

    Why wide rather than the long format this replaced: a long row carries its own ``role``
    and the union of every field, so each row is mostly empty and a reader who filters on
    ``role`` never sees the other role's columns at all. Wide puts one timestamp on one row
    with a column per field, which is what an analysis wants: `df["drv_rpm"]` beside
    `df["od_do_mg_l"]` on the same line of the same table.

    **Instants are merged within ``merge_ms``.** Two nodes do not share a clock, so the same
    physical instant arrives as two timestamps a few milliseconds apart; keeping them apart
    made two rows for one moment. Samples whose stamps fall within ``merge_ms`` of the group's
    first stamp are one row. The window must stay below the fastest role's own interval or it
    would merge two *distinct* samples of that role into one row and lose one: the aerator
    reports every 200 ms, and ``DEFAULT_MERGE_MS`` is 150 ms, deliberately under it. Measured
    on a real 12-minute run, 124 of the sensor's 125 stamps fall inside 150 ms of an aerator
    stamp and are grouped; the outlier is 3.2 s away and correctly gets its own row.

    Cells are **not** filled. A role that did not report within the window leaves its columns
    empty rather than repeating its previous value. Carrying a value forward invents a reading
    that was never taken -- it is what makes an aerator look like it ran at 420 rpm after it
    stopped -- and the aligned bundle already offers that trade, with a staleness bound, for
    readers who want a dense grid. Here every non-empty cell is a real sample.

    ``merge_spread_ms`` records how far apart the grouped stamps were, so a merged row is
    visibly merged rather than silently averaged. It is empty on a row that is a single sample.

    Memory: rows are collected as ``(stamp, role, fields)`` tuples and sorted once. Sorting is
    done in Python rather than by merging the per-role streams because the archive is not
    globally sorted -- the 131k-row aerator file for 2026-10-10 has 3 out-of-order timestamps
    -- and a heap merge would emit those out of order without saying so. A 12-minute run is
    3 210 samples; this is a few MB.
    """
    per_role: dict[str, int] = {}
    missing: list[str] = []

    # (stamp, role, {field: value}) for every sample, then sorted and windowed.
    samples: list[tuple[int, str, dict[str, Any]]] = []

    start_ms = int(started.timestamp() * 1000) if started else None
    end_ms = int(ended.timestamp() * 1000) if ended else None

    if authoritative_role is None:
        if hasattr(registry, "authoritative_role_for"):
            do_owner = registry.authoritative_role_for("dissolved_oxygen")
            authoritative_role = do_owner.role if do_owner else None
        elif hasattr(registry, "roles") and "od_sensor" in registry.roles:
            authoritative_role = "od_sensor"

    for role in roles:
        schema = registry.roles.get(role)
        if schema is None:
            continue
        count = 0
        for row in _read_role_rows(root, role, started, ended, tz_name):
            raw_ts = row.get("t_dev_ms")
            if raw_ts in (None, ""):
                # A row with no node timestamp cannot be placed on a time axis. Dropping it is
                # the honest choice: guessing a time would put a real reading at a false instant.
                continue
            try:
                ts = int(float(raw_ts))
            except (TypeError, ValueError):
                continue

            if start_ms is not None and ts < (start_ms - 5_000):
                continue
            if end_ms is not None and ts > (end_ms + 5_000):
                continue

            fields: dict[str, Any] = {_PREFIX_COLUMNS[1]: raw_ts}
            # The relative column is derived from t0, so it is computed per row before the
            # grouping rather than after: a merged row's t_rel belongs to its own instant.
            fields["t_rel_ms"] = _relative_ms(row, t0_ms)
            for key in ("t_src", "seq"):
                fields[key] = row.get(key)
            for field_def in schema.fields:
                value = row.get(field_def.name)
                if value not in (None, ""):
                    # Non-authoritative roles are prefixed (e.g. `aerator_drv_rpm`, `mixer_drv_rpm`)
                    # to prevent cross-node collisions when distinct actuators share field names.
                    # The authoritative DO sensor's fields stay unprefixed (`od_do_mg_l`).
                    col_name = wide_column(role, field_def.name, authoritative_role)
                    fields[col_name] = value
            samples.append((ts, role, fields))
            count += 1

        if count:
            per_role[role] = count
        else:
            if declared is None or role in declared:
                missing.append(role)
            log.warning("no native rows for role", extra={"role": role})

    # One pass over the sorted samples, opening a new group whenever the next stamp is more
    # than `merge_ms` past the group's first. Linear, and it needs no index: the list is
    # already in order, so the group's first stamp is the only bound to compare against --
    # which is exactly the rule "within 150 ms of the earliest".
    samples.sort(key=lambda item: item[0])
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(samples):
        first_ts, first_role, first_fields = samples[i]
        group = dict(first_fields)
        group["role"] = first_role
        spread = 0
        merged_any = False
        j = i + 1
        while j < len(samples) and samples[j][0] - first_ts <= merge_ms:
            ts, role, fields = samples[j]
            if role not in str(group["role"]).split("+"):
                group["role"] = f"{group['role']}+{role}"
            for key, value in fields.items():
                # The group's first sample owns the columns that describe the instant; a later
                # role's fields only fill columns that are still empty, so nothing overwrites a
                # real reading. Field names do not collide across roles (the registry enforces
                # it), but keeping the rule explicit means the merge cannot start dropping data
                # if one ever does.
                if key in _GROUP_COLUMNS:
                    continue
                if key not in group or group[key] in (None, ""):
                    group[key] = value
            merged_any = True
            spread = max(spread, ts - first_ts)
            j += 1
        # A merged row always reports its spread, including zero. `if spread:` was the bug
        # here: two roles reporting the *same* millisecond give spread 0, and the cell stayed
        # empty, so a row that combined two samples was indistinguishable from one that was a
        # single reading. Zero is a measurement -- `0` means "both nodes stamped this instant
        # exactly" -- and only a row that merged nothing leaves it empty.
        if merged_any:
            group["merge_spread_ms"] = spread
        rows.append(group)
        i = j

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return per_role, missing, len(rows)

    return per_role, missing


def _started_ms(experiment: Mapping[str, Any]) -> int | None:
    started = experiment.get("started_at")
    if isinstance(started, datetime):
        return int(started.timestamp() * 1000)
    if isinstance(started, str):
        try:
            return int(datetime.fromisoformat(started).timestamp() * 1000)
        except ValueError:
            return None
    return None


async def build_flat_export(
    settings: Any,
    manifest: Manifest,  # noqa: ARG001 — kept for signature parity with build_bundle
    db: Database,
    registry: SchemaRegistry,
    experiment_id: str,
    *,
    out_dir: Path,
    compress: bool = True,
) -> FlatReport:
    """
    Write every sample of the experiment, at native rate, plus the events.

    :raises FlatExportError: when the experiment is unknown, has no start time, or every
        role's local archive window is empty.
    """
    experiment = await _load_experiment(db, experiment_id)
    if experiment is None:
        raise FlatExportError(f"unknown experiment {experiment_id!r}")

    started = _as_datetime(experiment.get("started_at"))
    ended = _as_datetime(experiment.get("ended_at")) or datetime.now(tz=UTC)
    if started is None:
        raise FlatExportError(f"experiment {experiment_id!r} has no start time; nothing to align")
    t0_ms = _started_ms(experiment)

    report = FlatReport(experiment_id=experiment_id, out_dir=out_dir)

    declared_raw = experiment.get("roles") or []
    declared_roles = [role for role in declared_raw if role in registry.roles]
    # Check declared roles first, and also check all registry roles so telemetry from
    # unlisted or manually-commanded actuators (e.g. aerator or mixer) is never silently dropped.
    roles = list(dict.fromkeys(declared_roles + list(registry.roles)))

    authoritative_role = None
    if hasattr(registry, "authoritative_role_for"):
        do_owner = registry.authoritative_role_for("dissolved_oxygen")
        authoritative_role = do_owner.role if do_owner else None
    elif hasattr(registry, "roles") and "od_sensor" in registry.roles:
        authoritative_role = "od_sensor"

    # The union of every candidate role's column names, in a stable order: registry order for known
    # roles, then any extra columns the archive carries. Built first so the header is
    # written once and every row can be emitted without knowing what came before.
    # Non-authoritative roles are prefixed (e.g. aerator_drv_rpm, mixer_drv_rpm) using wide_column
    # to eliminate cross-role collisions while keeping od_sensor's authoritative fields bare.
    columns = list(_PREFIX_COLUMNS)
    # How far apart the stamps of a merged row were, empty for a single sample. Last of the
    # group columns so the readings stay adjacent, and present unconditionally so a reader
    # can tell a merged row from a lonely one without consulting the manifest.
    columns.insert(len(_PREFIX_COLUMNS), "merge_spread_ms")
    for role in roles:
        schema = registry.roles.get(role)
        if schema is None:
            continue
        for field_def in schema.fields:
            col_name = wide_column(role, field_def.name, authoritative_role)
            if col_name not in columns:
                columns.append(col_name)

    telemetry_path = out_dir / "telemetry_native.csv"

    # The archive read and the CSV write are blocking, so they run in one sync helper rather
    # than inside the coroutine: this is the same shape as ``bundle.read_archive_csv``, and
    # keeping the blocking work behind a plain function is what stops it silently becoming
    # an await point that stalls the event loop under a large export.
    per_role_rows, missing, total_rows = _write_wide_csv(
        telemetry_path,
        root=settings.archive.root,
        roles=roles,
        registry=registry,
        columns=columns,
        started=started,
        ended=ended,
        tz_name=settings.timezone,
        t0_ms=t0_ms,
        declared=declared_roles if declared_roles else None,
        authoritative_role=authoritative_role,
    )

    if not per_role_rows:
        telemetry_path.unlink(missing_ok=True)
        raise FlatExportError(
            f"no telemetry found for {experiment_id!r}: every role's local archive window is empty. "
            "Retrieve the compressed files from object storage and retry."
        )

    # `rows` is the number of rows written, which for a wide table is the number of distinct
    # instants -- not the sum of the per-role samples. The sum is kept separately in
    # `per_role_rows`, because the two answer different questions: "how big is the file" and
    # "how much did each node contribute". They used to be the same number when the format was
    # long, and conflating them here would overstate the row count.
    report.rows = total_rows
    report.per_role_rows = per_role_rows
    report.roles_present = list(per_role_rows)
    report.roles_missing = missing
    report.warnings = [f"role {role!r} produced no rows in the window" for role in missing]
    report.columns = columns

    # -- events ------------------------------------------------------------
    events = await _load_events(db, experiment_id)
    report.events = len(events)
    events_path = out_dir / "events.csv"
    _write_events(events_path, events)

    # -- manifest ----------------------------------------------------------
    write_manifest(
        out_dir / "manifest.json",
        report=report,
        experiment=experiment,
        registry=registry,
        started=started,
        ended=ended,
        t0_ms=t0_ms,
    )

    if compress:
        report.bundle_path = out_dir / f"{experiment_id}_native.tar.gz"
        with tarfile.open(report.bundle_path, "w:gz") as archive:
            for path in (telemetry_path, events_path, out_dir / "manifest.json"):
                archive.add(path, arcname=path.name)

    log.info(
        "native export built",
        extra={"experiment_id": experiment_id, "rows": report.rows, "roles": len(report.roles_present)},
    )
    return report


def _relative_ms(row: Mapping[str, Any], t0_ms: int | None) -> str:
    """Milliseconds from ``t0`` to this row, or empty when either end is unknown."""
    if t0_ms is None:
        return ""
    raw = row.get("t_dev_ms")
    if raw in (None, ""):
        return ""
    try:
        return str(int(float(raw)) - t0_ms)
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Manifest & events
# ---------------------------------------------------------------------------


def write_manifest(
    path: Path,
    *,
    report: FlatReport,
    experiment: Mapping[str, Any],
    registry: SchemaRegistry,
    started: datetime,
    ended: datetime,
    t0_ms: int | None,
) -> None:
    """Record what the export contains, so the file is self-describing without the reader."""
    document = {
        "export_format": "aqua-native-1",
        "experiment_id": report.experiment_id,
        "title": experiment.get("title"),
        "status": experiment.get("status"),
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "gateway_schema_version": registry.version,
        "time_range": {"start": started.isoformat(), "end": ended.isoformat()},
        "t0_ms": t0_ms,
        "rows": report.rows,
        "columns": report.columns,
        "events": report.events,
        "roles_present": report.roles_present,
        "roles_missing": report.roles_missing,
        "per_role_rows": report.per_role_rows,
        "calibration": experiment.get("calibration") or registry.calibration,
        "warnings": report.warnings,
        "notes": [
            "Wide: one row per *instant*, sorted ascending. Every role's fields are columns on "
            "that row, so od_do_mg_l and aerator_drv_rpm sit on the same line when the two nodes "
            "reported the same moment.",
            f"Instants are merged within {DEFAULT_MERGE_MS} ms: the nodes do not share a clock, "
            "so the same physical moment arrives as stamps a few ms apart. When a row combines "
            "more than one sample, `merge_spread_ms` holds how far the grouped stamps were "
            "apart -- 0 when they were identical, which is the common case. An empty "
            "`merge_spread_ms` means the row is a single sample.",
            f"The window stays below the fastest role's own interval ({DEFAULT_MERGE_MS} ms vs "
            "the aerator's 200 ms), so two consecutive samples of one role can never collapse "
            "into one row.",
            "Nothing is resampled and no value is carried forward: a cell is empty when that "
            "role did not report within the row's window, and a gap is a gap. Every non-empty "
            "cell is a real reading. For a dense grid with values carried forward under a "
            "staleness bound, use the aligned bundle instead.",
            "The roles sample at different rates -- od_sensor at 0.2 Hz, aerator at 5 Hz while "
            "the run is active and 0.0167 Hz idle, per roles.yaml -- so most rows have the fast "
            "role's columns filled and the slow role's empty. That is the measurement, not a "
            "defect.",
            "The `role` column names the node that produced the row's timestamp. `a+b` means "
            "both roles reported the same (merged) instant.",
            "t_rel_ms is milliseconds since the experiment's started_at; the source "
            "t_dev_ms is preserved verbatim and no clock offset was applied.",
            "Dissolved oxygen and water temperature come from the authoritative od_sensor role "
            "and stay bare (e.g. od_do_mg_l); actuator and auxiliary roles are prefixed with their "
            "role name (e.g. aerator_drv_rpm, mixer_drv_rpm, pump_pump_dosed_ml) to guarantee "
            "unambiguous, collision-free columns.",
            "peer_* columns on other roles are their own control-loop input and must not be averaged with it.",
        ],
    }
    path.write_text(json.dumps(document, indent=2, default=str), encoding="utf-8")


async def _load_events(db: Database, experiment_id: str) -> list[dict[str, Any]]:
    """
    Every event belonging to this experiment, oldest first.

    Selected by ``experiment_id``, deliberately not by a time window. The window version was
    wrong in both directions at once, and measurably so on the Pi's own data: of 21 events on
    `EXP-2026-10-11-02`, an export bounded by `[started_at, ended_at]` returned 20 -- it
    dropped 5 of the experiment's own events that happened after `ended_at` (three
    `command_expired` at +79 s, which are the delayed consequence of commands issued at the
    final second of the run) and pulled in 4 that belonged to something else (three with a
    null `experiment_id`, one from another experiment that overlapped). A row knows which
    run it is part of; asking when it happened instead is a lossy proxy for asking which run
    it belongs to.
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
    except Exception:
        log.warning("could not read events for native export")
        return []


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
        writer = csv.writer(handle)
        writer.writerow(header)
        for event in events:
            detail = event.get("detail")
            writer.writerow(
                [
                    event.get("ts"),
                    event.get("t_dev_ms"),
                    event.get("event_type"),
                    event.get("severity"),
                    event.get("scope"),
                    event.get("source"),
                    event.get("node_id"),
                    event.get("correlation_id"),
                    event.get("action"),
                    json.dumps(detail, default=str) if isinstance(detail, (dict, list)) else detail,
                ]
            )


# ---------------------------------------------------------------------------
# Helpers shared in spirit with bundle.py
# ---------------------------------------------------------------------------


async def _load_experiment(db: Database, experiment_id: str) -> Mapping[str, Any] | None:
    if not db.available:
        raise FlatExportError("database unavailable; cannot read the experiment record")
    try:
        return await db.fetchrow(
            "SELECT id, title, status, roles, started_at, ended_at, calibration FROM experiments WHERE id = $1",
            experiment_id,
        )
    except Exception as exc:
        raise FlatExportError(f"could not read experiment {experiment_id!r}: {exc}") from exc


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


# Re-export for callers that want to stream the file without loading it whole.
__all__ = [
    "FlatExportError",
    "FlatReport",
    "build_flat_export",
    "list_exports",
    "write_manifest",
]


def list_exports(export_root: Path, experiment_id: str) -> list[dict[str, Any]]:
    """
    Every export on disk for an experiment, newest first.

    Scans the filesystem rather than tracking exports in a table. An export is a derived
    artifact: it can be rebuilt at any time from the archive, so a database row would be
    bookkeeping that can disagree with the disk. The directory *is* the record.

    Two directories are checked — the aligned bundle's (``<id>``) and the native export's
    (``<id>_native``) — and each ``manifest.json`` is read for its metadata. A directory with
    no manifest is still listed, with ``format: "unknown"``, because a half-written or older
    export should be visible rather than silently hidden.
    """
    out: list[dict[str, Any]] = []

    for suffix, default_format in (("", "aligned"), ("_native", "native")):
        directory = export_root / f"{experiment_id}{suffix}"
        if not directory.is_dir():
            continue

        manifest_path = directory / "manifest.json"
        meta: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                meta = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}

        # The bundle is the archive if there is one; otherwise the largest CSV is the artifact.
        bundle = next(iter(sorted(directory.glob("*.tar.gz"))), None)
        size_bytes = 0
        for path in directory.iterdir():
            try:
                size_bytes += path.stat().st_size
            except OSError:
                continue

        out.append(
            {
                "experiment_id": experiment_id,
                "format": meta.get("export_format", default_format),
                "dir": str(directory),
                "bundle": str(bundle) if bundle else None,
                "generated_at": meta.get("generated_at"),
                "rows": meta.get("rows", 0),
                "events": meta.get("events", 0),
                "roles_present": meta.get("roles_present", []),
                "roles_missing": meta.get("roles_missing", []),
                "size_bytes": size_bytes,
                # Modification time is the fallback ordering key when the manifest predates
                # `generated_at`, which the earliest exports do.
                "modified_at": datetime.fromtimestamp(directory.stat().st_mtime, tz=UTC).isoformat(),
            }
        )

    out.sort(key=lambda item: item.get("generated_at") or item["modified_at"], reverse=True)
    return out
