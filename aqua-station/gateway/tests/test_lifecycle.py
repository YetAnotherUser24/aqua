"""
Experiment lifecycle and native-export tests.

What is asserted here is the behaviour an unattended bench depends on:

* **Pause is not stop.** Pausing holds the timer and stops phase advancement but does not
  disarm; stopping disarms. Conflating them would silently turn a pause into a shutdown.
* **The timer excludes paused spans.** A two-minute interruption must not read as two
  minutes of experiment.
* **Milestones reuse the event stream.** They list oldest-first, and they carry ``t_rel_s``
  so the chart can place them without recomputing ``t0``.
* **The native export is faithful.** Every row of every role is present, at native rate,
  with the values verbatim — no resampling and no carried-forward value.

The database is faked (see ``test_commands.py`` for the same pattern); the archive in the
export test is real CSV on disk, because the thing under test is reading real files.
"""

from __future__ import annotations

import csv
import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aqua_gateway.export.flat import FlatExportError, build_flat_export, list_exports
from aqua_gateway.orchestration.engine import STATE_ACTIVE, STATE_IDLE, Orchestrator

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _Record:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class SpyCommands:
    def __init__(self):
        self.calls: list[dict] = []

    async def issue(self, **kwargs):
        self.calls.append(kwargs)
        return _Record(status="published", id="x", role=kwargs["role"], action=kwargs["action"])


class FakeDB:
    def __init__(self):
        self.available = True
        self.executed: list[tuple] = []
        self.notes = "recipe:planta_2_step"
        self.phase_inserts: list[tuple] = []
        self.events_rows: list[dict] = []
        self.experiment: dict | None = {
            "id": "EXP-2026-10-10-01",
            "title": "Step test",
            "status": "planned",
            "roles": ["aerator", "od_sensor"],
            "started_at": None,
            "ended_at": None,
            "calibration": {},
        }

    async def execute(self, query, *_args):
        self.executed.append((query, _args))
        return "OK"

    async def executemany(self, _query, rows):
        for row in rows:
            self.phase_inserts.append(row)

    async def fetchrow(self, query, *_args):
        if "FROM experiments" in query:
            return {"notes": self.notes, "calibration": {}, "started_at": None, "status": "active"}
        return None

    async def fetch(self, query, *_args):
        if "experiment_phases" in query:
            return []
        return []


class FakeEvents:
    def __init__(self):
        self.emitted: list[object] = []

    async def emit(self, event, **_kwargs):
        self.emitted.append(event)

    def types(self):
        return [getattr(e, "event_type", None).value for e in self.emitted]


class FakeLive:
    def __init__(self, metrics=None):
        self._metrics = metrics or {}

    def snapshot(self):
        return {"nodes": {role: {"metrics": m} for role, m in self._metrics.items()}}


class FakeGateway:
    def __init__(self, metrics=None):
        self.commands = SpyCommands()
        self.db = FakeDB()
        self.events = FakeEvents()
        self.live = FakeLive(metrics)
        self.registry = None
        self.settings = None
        self.manifest = None


def _orch(metrics=None):
    gw = FakeGateway(metrics)
    return Orchestrator(gw), gw


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


async def test_pause_holds_but_does_not_disarm() -> None:
    """
    A pause must not issue a stop. That is the whole difference between pause and stop, and
    if it did, an operator pausing to take a sample would kill the aeration.
    """
    orch, gw = _orch()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    gw.commands.calls.clear()

    state = await orch.pause()

    assert state["paused"] is True
    assert state["state"] == STATE_ACTIVE
    # No disarm was issued.
    assert gw.commands.calls == []
    # The experiment row was marked paused.
    assert any("UPDATE experiments" in q and a[1] == "paused" for q, a in gw.db.executed)
    # And an EXPERIMENT_PAUSED event was emitted.
    assert "experiment_paused" in gw.events.types()


async def test_resume_clears_the_pause_and_logs_a_resume() -> None:
    orch, gw = _orch()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.pause()
    state = await orch.resume()

    assert state["paused"] is False
    assert "experiment_resumed" in gw.events.types()
    assert any("UPDATE experiments" in q and a[1] == "active" for q, a in gw.db.executed)


async def test_pausing_twice_is_harmless() -> None:
    orch, gw = _orch()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.pause()
    before = len(gw.events.emitted)
    await orch.pause()  # no-op
    assert len(gw.events.emitted) == before


async def test_paused_run_does_not_advance_a_phase() -> None:
    """The tick must be inert while paused, regardless of the live condition."""
    orch, _gw = _orch(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    orch._phase_entered_s = 0  # every timeout has long since passed
    await orch.pause()

    phase_before = orch.active_phase.phase_id
    await orch._tick()
    assert orch.active_phase.phase_id == phase_before


async def test_the_timer_excludes_paused_time() -> None:
    """
    A pause must not inflate ``t_rel_s``. Simulate a completed one-second pause and confirm
    the reported elapsed time is roughly the run time minus the pause, not including it.
    """
    orch, _gw = _orch()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    # Backdate the start so a real interval exists.
    orch._started_s -= 10.0
    baseline = await orch.current_state()
    assert baseline["t_rel_s"] is not None and baseline["t_rel_s"] >= 9.0

    # Record a completed 4-second pause.
    orch._paused_accum_s = 4.0
    after = await orch.current_state()
    assert after["t_rel_s"] < baseline["t_rel_s"]
    assert abs((baseline["t_rel_s"] - after["t_rel_s"]) - 4.0) < 0.2


# ---------------------------------------------------------------------------
# Stop still disarms (regression guard for the pause change)
# ---------------------------------------------------------------------------


async def test_stop_still_disarms_and_completes() -> None:
    orch, gw = _orch()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    gw.commands.calls.clear()

    await orch.request_state(state=STATE_IDLE, experiment_id=None)

    actions = {(c["role"], c["action"]) for c in gw.commands.calls}
    assert ("aerator", "stop") in actions
    assert ("mixer", "stop_mixer") in actions
    assert ("pump", "force_off") in actions
    assert any("UPDATE experiments" in q and a[1] == "completed" for q, a in gw.db.executed)


# ---------------------------------------------------------------------------
# Native export — real files on disk
# ---------------------------------------------------------------------------


def _write_archive(root: Path, role: str, day: str, rows: list[dict], *, gzipped: bool = False) -> Path:
    directory = root / role
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"{day}.csv.gz" if gzipped else f"{day}.csv")
    fields = list(rows[0].keys())
    opener = gzip.open if gzipped else open
    with opener(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


class _Settings:
    def __init__(self, root: Path):
        self.archive = type("A", (), {"root": root})()
        # The export names archive day-files in this zone, so the stub has to carry it.
        self.timezone = "America/Lima"


class _Registry:
    """Minimal registry stand-in: field names per role, in order."""

    def __init__(self):
        from aqua_gateway.schema.registry import load_registry

        self._real = load_registry()

    @property
    def roles(self):
        return self._real.roles

    @property
    def calibration(self):
        return {}

    @property
    def version(self):
        return self._real.version

    def authoritative_role_for(self, quantity: str):
        return self._real.authoritative_role_for(quantity)


class _ExportDB:
    def __init__(self, started: datetime | None = None):
        self.available = True
        self.started = started or datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    async def fetchrow(self, _query, *_args):
        return {
            "id": "EXP-2026-10-10-01",
            "title": "Step",
            "status": "completed",
            "roles": ["od_sensor", "aerator"],
            "started_at": self.started,
            "ended_at": self.started + timedelta(minutes=5),
            "calibration": {},
        }

    async def fetch(self, _query, *_args):
        return []


async def test_native_export_keeps_every_row_at_native_rate(tmp_path: Path) -> None:
    """
    Two roles at different rates must both come out whole. A 5 Hz actuator series must not be
    thinned, and the 1 Hz sensor series must not be padded to match it.
    """
    root = tmp_path / "telemetry"
    registry = _Registry()
    day = "2026-10-10"
    t0_ms = int(datetime(2026, 10, 10, 12, 0, tzinfo=UTC).timestamp() * 1000)

    # od_sensor at 1 Hz: 5 rows.
    sensor_fields = {f.name: "0" for f in registry.roles["od_sensor"].fields}
    sensor_rows = []
    for i in range(5):
        row = dict(sensor_fields)
        row["t_dev_ms"] = str(t0_ms + i * 1000)
        row["od_do_mg_l"] = f"{5.0 + i * 0.1:.2f}"
        sensor_rows.append(row)

    # aerator at 5 Hz: 25 rows over the same span — this is the data the aligned bundle loses.
    aero_fields = {f.name: "0" for f in registry.roles["aerator"].fields}
    aero_rows = []
    for i in range(25):
        row = dict(aero_fields)
        row["t_dev_ms"] = str(t0_ms + i * 200)
        row["drv_rpm"] = str(200 + i)
        aero_rows.append(row)

    _write_archive(root, "od_sensor", day, sensor_rows, gzipped=True)
    _write_archive(root, "aerator", day, aero_rows)

    report = await build_flat_export(
        _Settings(root),
        manifest=None,  # not used by the flat path
        db=_ExportDB(),
        registry=registry,
        experiment_id="EXP-2026-10-10-01",
        out_dir=tmp_path / "out",
        compress=False,
    )

    assert report.per_role_rows["od_sensor"] == 5
    assert report.per_role_rows["aerator"] == 25
    # Every sample survived: 30 across the two roles. That is the property this test guards,
    # and it is asserted on `per_role_rows` because `rows` now means something else.
    assert sum(report.per_role_rows.values()) == 30

    with (tmp_path / "out" / "telemetry_native.csv").open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    # The table is wide: one row per distinct t_dev_ms, both roles' fields as columns. The
    # sensor samples at 0,1000,...,4000 and the aerator at 0,200,...,4800, so every sensor
    # instant coincides with an aerator one and the union is just the 25 aerator instants --
    # not 30. A reader expecting sum-of-samples here would be reading the old format.
    assert report.rows == len(rows) == 25, "one row per distinct instant, not per sample"
    assert len(rows) == 25

    # Every aerator sample survives, at its own cadence. This is the data the aligned bundle
    # loses, so it is the property worth pinning.
    aero_rpms = [r["aerator_drv_rpm"] for r in rows if r.get("aerator_drv_rpm")]
    assert len(aero_rpms) == 25, "all 25 aerator samples must be present, at native rate"
    assert aero_rpms[0] == "200" and aero_rpms[-1] == "224"

    sensor_dos = [r["od_do_mg_l"] for r in rows if r.get("od_do_mg_l")]
    assert len(sensor_dos) == 5, "all 5 sensor samples must be present"
    assert sensor_dos[-1] == "5.40"

    # The payload of the change: at an instant both nodes reported, one row carries both.
    both = [r for r in rows if r.get("od_do_mg_l") and r.get("aerator_drv_rpm")]
    assert both, "the two roles must share rows where their instants coincide"
    assert both[0]["od_do_mg_l"] == "5.00" and both[0]["aerator_drv_rpm"] == "200"

    # And sorted, so a reader does not have to.
    stamps = [int(r["t_dev_ms"]) for r in rows]
    assert stamps == sorted(stamps), "rows must come out in ascending time order"


async def test_native_export_adds_a_relative_time_column(tmp_path: Path) -> None:
    root = tmp_path / "telemetry"
    registry = _Registry()
    day = "2026-10-10"
    started = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    t0_ms = int(started.timestamp() * 1000)

    fields = {f.name: "0" for f in registry.roles["od_sensor"].fields}
    rows = []
    for i in range(3):
        row = dict(fields)
        row["t_dev_ms"] = str(t0_ms + i * 1000)
        row["od_do_mg_l"] = "6.0"
        rows.append(row)
    _write_archive(root, "od_sensor", day, rows)

    db = _ExportDB()
    db.started = started
    await build_flat_export(
        _Settings(root),
        manifest=None,
        db=db,
        registry=registry,
        experiment_id="EXP-2026-10-10-01",
        out_dir=tmp_path / "out2",
        compress=False,
    )

    with (tmp_path / "out2" / "telemetry_native.csv").open(encoding="utf-8") as handle:
        out = list(csv.DictReader(handle))

    assert [r["t_rel_ms"] for r in out] == ["0", "1000", "2000"]


async def test_native_export_reports_missing_roles(tmp_path: Path) -> None:
    """
    A role that takes part but produced nothing must be reported, not silently absent. The
    difference between "nothing happened" and "that node was not recording" is the whole
    reason a thesis export carries a manifest.
    """
    root = tmp_path / "telemetry"
    registry = _Registry()
    day = "2026-10-10"
    t0_ms = int(datetime(2026, 10, 10, 12, 0, tzinfo=UTC).timestamp() * 1000)
    fields = {f.name: "0" for f in registry.roles["od_sensor"].fields}
    rows = [{**fields, "t_dev_ms": str(t0_ms), "od_do_mg_l": "6.0"}]
    _write_archive(root, "od_sensor", day, rows)

    report = await build_flat_export(
        _Settings(root),
        manifest=None,
        db=_ExportDB(),
        registry=registry,
        experiment_id="EXP-2026-10-10-01",
        out_dir=tmp_path / "out3",
        compress=False,
    )

    assert "od_sensor" in report.roles_present
    assert "aerator" in report.roles_missing
    assert any("aerator" in w for w in report.warnings)


async def test_native_export_refuses_when_everything_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "telemetry"
    registry = _Registry()
    with pytest.raises(FlatExportError, match="no telemetry"):
        await build_flat_export(
            _Settings(root),
            manifest=None,
            db=_ExportDB(),
            registry=registry,
            experiment_id="EXP-2026-10-10-01",
            out_dir=tmp_path / "out4",
            compress=False,
        )


async def test_native_export_finds_a_window_that_starts_late_lima_time(tmp_path: Path) -> None:
    """
    An experiment starting 01:44 UTC is 20:44 the *previous* day in Lima, and the archive
    names its files by the Lima day of the device timestamps.

    The regression: `role_files` took `start.date()` in UTC, so for a window beginning in the
    19:00–24:00 Lima band it looked for the next day's file and reported "no telemetry found"
    for data that was on disk. This goes through `build_flat_export`, which is the path the
    dashboard's native export actually takes.
    """
    root = tmp_path / "telemetry"
    registry = _Registry()

    # Starts 01:44 UTC = 20:44 Lima on the 10th, so the data belongs in the 10th's file.
    started = datetime(2026, 10, 11, 1, 44, tzinfo=UTC)
    t0_ms = int(started.timestamp() * 1000)

    sample = {f.name: "0" for f in registry.roles["od_sensor"].fields}
    sample["od_do_mg_l"] = "7.00"
    sample["t_dev_ms"] = str(t0_ms)

    _write_archive(root, "od_sensor", "2026-10-10", [sample])

    report = await build_flat_export(
        _Settings(root),
        manifest=None,
        db=_ExportDB(started=started),
        registry=registry,
        experiment_id="EXP-2026-10-10-01",
        out_dir=tmp_path / "out_late",
        compress=False,
    )

    assert report.rows == 1, "the previous day's file holds this window, and must be found"
    assert report.per_role_rows["od_sensor"] == 1


# ---------------------------------------------------------------------------
# Export index — reads the filesystem, not a table
# ---------------------------------------------------------------------------


def test_list_exports_finds_both_directories_newest_first(tmp_path: Path) -> None:
    exp = "EXP-2026-10-10-01"
    aligned = tmp_path / exp
    native = tmp_path / f"{exp}_native"
    aligned.mkdir()
    native.mkdir()

    (aligned / "manifest.json").write_text(
        '{"rows": 100, "events": 3, "generated_at": "2026-10-10T10:00:00+00:00", "roles_present": ["od_sensor"]}',
        encoding="utf-8",
    )
    (native / "manifest.json").write_text(
        '{"export_format": "aqua-native-1", "rows": 5000, "events": 7, '
        '"generated_at": "2026-10-10T12:00:00+00:00", "roles_present": ["od_sensor", "aerator"], '
        '"roles_missing": ["mixer"]}',
        encoding="utf-8",
    )
    (aligned / f"{exp}.tar.gz").write_bytes(b"x" * 100)

    out = list_exports(tmp_path, exp)

    assert len(out) == 2
    # The native export is newer, so it sorts first.
    assert out[0]["format"] == "aqua-native-1"
    assert out[0]["rows"] == 5000
    assert out[0]["roles_missing"] == ["mixer"]
    assert out[1]["format"] == "aligned"


def test_list_exports_tolerates_a_missing_manifest(tmp_path: Path) -> None:
    """A directory with no readable manifest is still listed, not hidden."""
    exp = "EXP-2026-10-10-02"
    (tmp_path / exp).mkdir()
    (tmp_path / exp / "telemetry.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    out = list_exports(tmp_path, exp)

    assert len(out) == 1
    assert out[0]["format"] == "aligned"  # default, since no manifest said otherwise
    assert out[0]["rows"] == 0
    assert out[0]["size_bytes"] > 0


def test_list_exports_returns_empty_for_an_unknown_experiment(tmp_path: Path) -> None:
    assert list_exports(tmp_path, "EXP-nope") == []
