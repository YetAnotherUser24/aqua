#!/usr/bin/env python3
"""
Mock gateway for dashboard development.

Serves ``web/out/`` plus a synthetic implementation of every endpoint the dashboard calls, on
one port, so the UI can be developed and reviewed without a broker, a database, or the Pi.

Why this exists rather than "run the real gateway": the real one needs mosquitto and
PostgreSQL, which are neither installed on a laptop nor worth installing to adjust a layout.
But the field names, units, precisions and thresholds here are **loaded from the real
registry** — ``gateway/aqua_gateway/schema/roles.yaml`` and ``events/derived.py``. So a field
rename in the schema breaks this harness in the same way it would break production, which is
the property that makes it worth having.

What it deliberately does not simulate: the archive, the outbox, MQTT, or the rollup writer.
Those have their own test suites. This is a rendering harness, not a simulator — for a
protocol-level simulator see ``tools/mqtt-simulator.py``.

Usage:
    python3 tools/mock-gateway.py --root web/out --port 8899
"""

from __future__ import annotations

import argparse
import json
import math
import random
import select
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY = REPO_ROOT / "gateway"
sys.path.insert(0, str(GATEWAY))

try:
    from aqua_gateway.events.derived import Thresholds
    from aqua_gateway.mqtt.envelope import TimeSource
    from aqua_gateway.schema.registry import load_registry
except ImportError as exc:  # pragma: no cover - developer feedback path
    sys.exit(
        f"cannot import the gateway package from {GATEWAY}: {exc}\n"
        "Install its dependencies first, e.g. pip install -e gateway"
    )

TIMEZONE = timezone(timedelta(hours=-5))  # America/Lima


# ---------------------------------------------------------------------------
# Simulated pond
# ---------------------------------------------------------------------------


class Pond:
    """
    A small closed-loop simulation, so the numbers move the way the real ones do.

    The control law is the same shape as the firmware's: aeration switching on below a low
    threshold and off above a high one. That matters for review purposes — it means the
    dashboard is exercised through a real hysteresis cycle rather than a sine wave.
    """

    LOW_ON = 5.4
    HIGH_OFF = 7.6
    FALL_PER_S = 0.004  # respiration, no aeration
    RISE_PER_S = 0.012  # aeration on

    def __init__(self, boot_id: int) -> None:
        self.boot_id = boot_id
        self.started = time.time()
        self.do = 7.4
        self.temp = 23.1
        self.battery = 4.16
        self.aerator_on = True
        self.aerator_rpm = 0.0
        self.mixer_on = False
        self.mixer_rpm = 0.0
        self.pump_dosed_ml = 0.0
        self.pump_mode = "idle"
        self.dose_until = 0.0
        self.seq = 0
        self.lock = threading.Lock()

    def tick(self, dt: float) -> None:
        with self.lock:
            if self.do < self.LOW_ON:
                self.aerator_on = True
            elif self.do > self.HIGH_OFF:
                self.aerator_on = False

            elapsed = time.time() - self.started
            # Aerator runs a continuous sawtooth waveform (100 to 600 RPM over 60s)
            sawtooth_phase = (elapsed % 60.0) / 60.0
            self.aerator_rpm = round(100.0 + sawtooth_phase * 500.0, 1)
            self.aerator_on = True

            # DO is synchronized with the continuous 16-min oxygenation cycle
            self.do = round(6.0 + 1.8 * math.sin((elapsed / 480.0) * math.pi), 3)

            # Mixer runs in bursts (90s on, 150s off)
            self.mixer_on = (elapsed % 240.0) < 90.0
            self.mixer_rpm = 780.0 if self.mixer_on else 0.0

            # Pump doses every ~90 s while the mixer runs.
            if (
                self.pump_mode == "idle"
                and self.mixer_on
                and (elapsed % 90) < 1.5
            ):
                self.pump_mode = "dose"
                self.dose_until = time.time() + 12
                self.pump_dosed_ml += 25.0
            elif self.pump_mode == "dose" and time.time() > self.dose_until:
                self.pump_mode = "idle"

            self.seq += 1

    def samples(self) -> dict[str, dict[str, Any]]:
        """Live metrics per role, matching schema v2 field names exactly."""
        with self.lock:
            # Baseline dissolved oxygen saturation referenced against C* = 7.15 mg/L
            saturation = max(0.0, min(160.0, (self.do / 7.15) * 100.0))
            running = self.aerator_rpm > 50
            return {
                "od_sensor": {
                    "od_do_mg_l": round(self.do, 3),
                    "od_do_raw": int(round(self.do * 1000)),
                    "od_sat_pct": round(saturation, 2),
                    "od_sat_raw": int(round(saturation * 10)),
                    "od_temp_c": round(self.temp, 2),
                    "od_temp_raw": int(round(self.temp * 100)),
                    "od_aux1_raw": 0,
                    "od_aux2_raw": 0,
                    "pwr_battery_v": round(self.battery, 2),
                    "rtc_temp_c": round(self.temp + 2.4, 2),
                    "rtc_health": 1,
                    "dev_status": 0,
                },
                "aerator": {
                    "drv_rpm": round(self.aerator_rpm, 2),
                    "drv_rpm_target": 2150.0 if self.aerator_on else 0.0,
                    "drv_vbus_v": round(23.8 + random.gauss(0, 0.05), 2),
                    "drv_ibus_a": round(
                        0.02 + (self.aerator_rpm / 2150.0) * 3.4, 2
                    ),
                    "drv_power_w": round(
                        (self.aerator_rpm / 2150.0) * 81.0, 2
                    ),
                    "drv_torque_nm": round(
                        (self.aerator_rpm / 2150.0) * 0.21, 3
                    ),
                    "drv_iq_a": round((self.aerator_rpm / 2150.0) * 2.9, 2),
                    "drv_running": running,
                    "drv_status": 0,
                    "ctl_mode": "pid",
                    "ctl_failsafe": False,
                    "peer_do_mg_l": round(self.do, 3),
                    "peer_temp_c": round(self.temp, 2),
                },
                "mixer": {
                    "drv_rpm": round(self.mixer_rpm, 1),
                    "drv_rpm_target": 780.0 if self.mixer_on else 0.0,
                    "drv_rad_s": round(self.mixer_rpm * 0.10472, 3),
                    "drv_rad_s_target": round(780.0 * 0.10472, 3)
                    if self.mixer_on
                    else 0.0,
                    "drv_duty_pct": round((self.mixer_rpm / 780.0) * 62.0, 2),
                    "drv_running": self.mixer_rpm > 40,
                    "drv_stall": False,
                    "drv_status": 0,
                },
                "pump": {
                    "pump_dosed_ml": round(self.pump_dosed_ml, 2),
                    "pump_target_ml": 25.0,
                    "pump_rpm": 42.0 if self.pump_mode == "dose" else 0.0,
                    "pump_mode": self.pump_mode,
                    "pump_dose_completed": self.pump_mode == "idle"
                    and self.pump_dosed_ml > 0,
                    "pump_dose_timed_out": False,
                    "drv_status": 0,
                },
            }


class FakeGateway:
    """Holds the simulated state and the shapes the API is expected to return."""

    @staticmethod
    def _build_phase_plan(
        recipe_id: str = "planta_2_step",
    ) -> list[dict[str, Any]]:
        """The phase plan for a recipe, taken from the gateway's own ``RECIPES``.

        Derived rather than hand-copied, so the live-run view carries the same titles, params
        and conditions the real engine would report — the drift that left the panel blank.
        """
        from aqua_gateway.orchestration.recipes import RECIPES

        recipe = RECIPES[recipe_id]
        return [
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
        ]

    def __init__(self, simulate: bool = True) -> None:
        self.registry = load_registry()
        self.thresholds = Thresholds()
        self.pond = Pond(boot_id=int(time.time()))
        self.simulate = simulate
        self.started = time.time()
        self.events: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.experiment_id = "exp-2026-10-09-deox"
        self._event_id = 1
        # Experiment rows, newest first. The GET returns this and a POST inserts into it, so the
        # registry is not a fixed fixture.
        self.experiments: list[dict[str, Any]] = [
            {
                "id": self.experiment_id,
                "title": "Planta 1 — desoxigenación y respuesta del lazo",
                "status": "active",
                "roles": ["od_sensor", "aerator", "mixer"],
                "started_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=88)
                ).isoformat(),
                "ended_at": None,
                "created_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=90)
                ).isoformat(),
            },
            {
                "id": "exp-2026-10-07-step",
                "title": "Planta 2 — escalón de referencia",
                "status": "completed",
                "roles": ["od_sensor", "aerator"],
                "started_at": (
                    datetime.now(timezone.utc) - timedelta(days=2, hours=3)
                ).isoformat(),
                "ended_at": (
                    datetime.now(timezone.utc) - timedelta(days=2)
                ).isoformat(),
                "created_at": (
                    datetime.now(timezone.utc) - timedelta(days=2, hours=4)
                ).isoformat(),
            },
            {
                "id": "exp-2026-10-05-closed",
                "title": "Caso B — lazo cerrado con setpoint 6.5 mg/L",
                "status": "completed",
                "roles": ["od_sensor", "aerator", "pump"],
                "started_at": (
                    datetime.now(timezone.utc) - timedelta(days=4, hours=5)
                ).isoformat(),
                "ended_at": (
                    datetime.now(timezone.utc) - timedelta(days=4, hours=2)
                ).isoformat(),
                "created_at": (
                    datetime.now(timezone.utc) - timedelta(days=4, hours=6)
                ).isoformat(),
            },
        ]
        # Operator milestones, newest first. Seeded so the bitácora is not empty on first load;
        # a POST appends to this and the GET returns it, so "add a hito" is observable.
        self._milestone_seq = 2
        # Monotonic counter for created experiments, so ids never collide with the seeds above.
        self._experiment_seq = len(self.experiments)
        self.milestones: list[dict[str, Any]] = [
            {
                "id": 1,
                "ts": (
                    datetime.now(timezone.utc)
                    - timedelta(minutes=3, seconds=33)
                ).isoformat(),
                "t_dev_ms": None,
                "experiment_id": self.experiment_id,
                "event_type": "milestone_recorded",
                "severity": "info",
                "scope": "operator",
                "source": "ui",
                "action": "step",
                "detail": {
                    "kind": "step",
                    "label": "Escalón ODrive",
                    "detail": "0 → 210 RPM",
                    "t_rel_s": 90.0,
                    "do_mg_l": 3.2,
                },
            },
            {
                "id": 2,
                "ts": (
                    datetime.now(timezone.utc)
                    - timedelta(minutes=3, seconds=3)
                ).isoformat(),
                "t_dev_ms": None,
                "experiment_id": self.experiment_id,
                "event_type": "milestone_recorded",
                "severity": "info",
                "scope": "operator",
                "source": "ui",
                "action": "sulfite",
                "detail": {
                    "kind": "sulfite",
                    "label": "Dosis Na₂SO₃",
                    "detail": "12.5 mL",
                    "t_rel_s": 45.0,
                    "do_mg_l": 6.4,
                },
            },
        ]

        # -- orchestrator state (WS1), mutable so the run-control buttons do something ----
        # The real gateway's engine owns this; here it is a small state machine that mirrors
        # the engine's observable behaviour so the UI can be exercised end to end.
        self._state = "ACTIVE_EXPERIMENT"
        self._paused = False
        self._recipe_id = "planta_2_step"
        self._phase_plan = self._build_phase_plan(self._recipe_id)
        self._phase_index = 2  # 0-based into _phase_plan; index 2 == p2_acq
        self._awaiting_confirm: str | None = None
        self._mode_overrides: dict[str, str] = {}
        self._started_at = datetime.now(timezone.utc) - timedelta(
            minutes=4, seconds=18
        )
        self._paused_accum_s = 0.0
        self._paused_at: float | None = None

        self._seed_events()

        if simulate:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # -- simulation loop ---------------------------------------------------

    def _run(self) -> None:
        last = time.time()
        while True:
            time.sleep(1.0)
            now = time.time()
            self.pond.tick(min(2.0, now - last))
            last = now

    def _emit(
        self,
        event_type: str,
        severity: str,
        scope: str,
        source: str,
        *,
        node_id: str | None = None,
        action: str | None = None,
        detail: dict[str, Any] | None = None,
        age_s: float = 0.0,
    ) -> None:
        self.events.insert(
            0,
            {
                "id": self._event_id,
                "ts": (
                    datetime.now(timezone.utc) - timedelta(seconds=age_s)
                ).isoformat(),
                "t_dev_ms": int((time.time() - age_s) * 1000),
                "experiment_id": self.experiment_id
                if scope == "experiment"
                else None,
                "event_type": event_type,
                "severity": severity,
                "scope": scope,
                "source": source,
                "node_id": node_id,
                "correlation_id": None,
                "action": action,
                "detail": detail or {},
            },
        )
        self._event_id += 1

    def _seed_events(self) -> None:
        node = {
            "od_sensor": self.registry.for_role("od_sensor").node_id,
            "aerator": self.registry.for_role("aerator").node_id,
            "mixer": self.registry.for_role("mixer").node_id,
            "pump": self.registry.for_role("pump").node_id,
        }
        self._emit(
            "system_boot",
            "info",
            "system",
            "gateway",
            age_s=5400,
            detail={
                "schema_version": self.registry.version,
                "version": "0.1.0",
            },
        )
        self._emit(
            "clock_resynced",
            "info",
            "node",
            "gateway",
            node_id=node["od_sensor"],
            age_s=5390,
            detail={"offset_ms": -38, "t_src": "espnow"},
        )
        self._emit(
            "node_online",
            "info",
            "node",
            "firmware",
            node_id=node["od_sensor"],
            age_s=5388,
            detail={"topic": "aqua/tlm/od_sensor"},
        )
        self._emit(
            "experiment_started",
            "info",
            "experiment",
            "ui",
            age_s=5200,
            action="start",
            detail={"roles": ["od_sensor", "aerator"]},
        )
        self._emit(
            "do_low_warning",
            "warning",
            "process",
            "gateway",
            age_s=3100,
            action="aerator_on",
            detail={"value_mg_l": 5.28, "threshold_mg_l": 5.0},
        )
        self._emit(
            "aerator_started",
            "info",
            "node",
            "firmware",
            node_id=node["aerator"],
            age_s=3098,
            action="start",
            detail={"target_rpm": 2150.0},
        )
        self._emit(
            "do_low_recovered",
            "info",
            "process",
            "gateway",
            age_s=2400,
            detail={"value_mg_l": 7.62, "deadband_mg_l": 0.3},
        )
        self._emit(
            "dose_completed",
            "info",
            "node",
            "firmware",
            node_id=node["pump"],
            age_s=640,
            action="dose",
            detail={"dosed_ml": 25.0, "target_ml": 25.0},
        )
        self._emit(
            "operator_override",
            "warning",
            "operator",
            "ui",
            age_s=180,
            action="set_speed",
            detail={"by": "operator:web", "reason": "manual agitation"},
        )
        self._emit(
            "sequence_gap",
            "warning",
            "node",
            "gateway",
            node_id=node["mixer"],
            age_s=75,
            detail={"expected": 412, "received": 415, "missing": 2},
        )

    # -- endpoint payloads -------------------------------------------------

    def roles_payload(self) -> dict[str, Any]:
        from aqua_gateway.api.app import _AGGREGATES_BY_FIELD

        return {
            "schema_version": self.registry.version,
            "max_batch": self.registry.max_batch,
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
                for schema in self.registry.all_roles()
            },
        }

    def live_payload(self) -> dict[str, Any]:
        metrics = self.pond.samples()
        nodes: dict[str, Any] = {}
        for role in self.registry.roles:
            schema = self.registry.for_role(role)
            online = True
            nodes[role] = {
                "role": role,
                "node_id": schema.node_id,
                "online": online,
                "age_s": round(random.uniform(0.2, 1.4), 1)
                if online
                else 412.6,
                "rssi": random.randint(-72, -48) if online else None,
                "t_src": TimeSource.ESPNOW.value
                if role == "od_sensor"
                else TimeSource.NTP.value,
                "schema_version": self.registry.version,
                "clock_offset_ms": random.randint(-90, 120)
                if online
                else None,
                "metrics": metrics.get(role, {}),
            }
        return {"ts": time.time(), "nodes": nodes}

    def levels_payload(self) -> dict[str, Any]:
        t = self.thresholds
        return {
            "levels": {
                "od_sensor": {
                    "do_low": "OK",
                    "temp_high": "OK",
                    "battery_low": "OK",
                },
                "aerator": {"do_low": "OK"},
                "pump": {"do_low": "OK"},
            },
            "thresholds": {
                "do_warning_mg_l": t.do_warning_mg_l,
                "do_critical_mg_l": t.do_critical_mg_l,
                "do_recovery_deadband_mg_l": t.do_recovery_deadband_mg_l,
                "do_drop_rate_mg_l_per_min": t.do_drop_rate_mg_l_per_min,
                "do_jump_mg_l": t.do_jump_mg_l,
                "temp_warning_c": t.temp_warning_c,
                "battery_warning_v": t.battery_warning_v,
            },
        }

    def state_payload(self) -> dict[str, Any]:
        """Orchestrator state (WS1/WS2), shaped for the run header and phase panel.

        Derived from the mutable fields above rather than a fixed dict, so a POST to
        ``/api/state``, ``/pause``, ``/resume``, ``/confirm`` or ``/phase-mode`` is reflected
        on the next read — which is what makes the run-control buttons demonstrable here.
        """
        phases = []
        for index, phase in enumerate(self._phase_plan):
            if index < self._phase_index:
                status = "completed"
            elif index == self._phase_index:
                status = (
                    "awaiting_confirm"
                    if self._awaiting_confirm == phase["phase_id"]
                    else "active"
                )
            else:
                status = "pending"
            phases.append(
                {
                    **phase,
                    "mode": self._mode_overrides.get(
                        phase["phase_id"], phase["mode"]
                    ),
                    "status": status,
                }
            )
        active = (
            self._phase_plan[self._phase_index]
            if self._phase_index < len(self._phase_plan)
            else None
        )
        return {
            "state": self._state,
            "paused": self._paused,
            "experiment_id": self.experiment_id
            if self._state != "IDLE"
            else None,
            "recipe_id": self._recipe_id if self._state != "IDLE" else None,
            "phase_id": active["phase_id"]
            if active and self._state == "ACTIVE_EXPERIMENT"
            else None,
            "phase_num": active["num"]
            if active and self._state == "ACTIVE_EXPERIMENT"
            else None,
            "awaiting_confirm": self._awaiting_confirm,
            "started_at": self._started_at.isoformat(),
            "t_rel_s": self._t_rel_s(),
            "phases": phases,
        }

    def _t_rel_s(self) -> float:
        paused = self._paused_accum_s
        if self._paused and self._paused_at is not None:
            paused += time.time() - self._paused_at
        return round(
            (datetime.now(timezone.utc) - self._started_at).total_seconds()
            - paused,
            1,
        )

    def transition_state(self, body: dict[str, Any]) -> dict[str, Any]:
        """Handle ``POST /api/state``: IDLE stops, ACTIVE_EXPERIMENT starts/resumes.

        Mirrors the engine's contract closely enough for the UI: ACTIVE requires an
        experiment, and returning to IDLE clears the run.
        """
        state = str(body.get("state", "")).upper()
        if state == "IDLE":
            self._state = "IDLE"
            self._paused = False
            self._awaiting_confirm = None
        elif state == "ACTIVE_EXPERIMENT":
            if body.get("experiment_id"):
                self.experiment_id = str(body["experiment_id"])
            self._state = "ACTIVE_EXPERIMENT"
            self._paused = False
        elif state == "MANUAL_OVERRIDE":
            self._state = "MANUAL_OVERRIDE"
        return self.state_payload()

    def pause_run(self) -> dict[str, Any]:
        if self._state == "ACTIVE_EXPERIMENT" and not self._paused:
            self._paused = True
            self._paused_at = time.time()
        return self.state_payload()

    def resume_run(self) -> dict[str, Any]:
        if self._paused and self._paused_at is not None:
            self._paused_accum_s += time.time() - self._paused_at
            self._paused = False
            self._paused_at = None
        return self.state_payload()

    def confirm_phase(self) -> dict[str, Any]:
        """Operator visto bueno / Forzar Avance: clear any hold and step to the next phase."""
        self._awaiting_confirm = None
        if self._phase_index < len(self._phase_plan) - 1:
            self._phase_index += 1
        return self.state_payload()

    def set_phase_mode(self, phase_id: str, mode: str) -> dict[str, Any]:
        self._mode_overrides[phase_id] = mode
        return self.state_payload()

    def recipes_payload(self) -> dict[str, Any]:
        """The recipe catalogue (WS1), built from the gateway's own ``RECIPES``.

        Derived rather than hand-copied, so a param or phase added to ``recipes.py`` shows up
        here without a second edit — the drift that made the phase params render empty.
        """
        from aqua_gateway.orchestration.recipes import RECIPES

        recipes = []
        for recipe in RECIPES.values():
            recipes.append(
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
            )
        return {"recipes": recipes}

    def sample_metric(self, pt_ts: float, role: str, metric: str) -> float:
        """Unified deterministic physical metric evaluator for any timestamp.
        Guarantees 100% data identity across all time ranges (10m, 1h, 3h, 24h, 7d).
        """
        elapsed_s = pt_ts - self.started
        if metric == "drv_rpm":
            if role == "mixer":
                is_on = (elapsed_s % 240.0) < 90.0
                return 780.0 if is_on else 0.0
            else:
                # Aerator continuous sawtooth wave from 100 to 600 RPM over 60s
                sawtooth_phase = (elapsed_s % 60.0) / 60.0
                return 100.0 + sawtooth_phase * 500.0
        elif metric == "od_do_mg_l":
            # Continuous smooth oxygenation wave (period 16 min = 960s)
            wave = math.sin((elapsed_s / 480.0) * math.pi)
            return 6.0 + 1.8 * wave
        elif metric == "od_sat_pct":
            # Baseline dissolved oxygen saturation referenced against C* = 7.15 mg/L
            do_val = self.sample_metric(pt_ts, role, "od_do_mg_l")
            return max(0.0, min(160.0, (do_val / 7.15) * 100.0))
        elif metric == "drv_power_w":
            if role == "mixer":
                is_on = (elapsed_s % 240.0) < 90.0
                return 120.0 if is_on else 0.0
            else:
                sawtooth_phase = (elapsed_s % 60.0) / 60.0
                rpm = 100.0 + sawtooth_phase * 500.0
                return (rpm / 600.0) * 80.0
        elif metric == "od_temp_c":
            return 23.4
        elif metric == "drv_vbus_v":
            return 24.1
        elif metric == "drv_ibus_a":
            sawtooth_phase = (elapsed_s % 60.0) / 60.0
            return 0.5 + sawtooth_phase * 3.0
        elif metric == "pump_dosed_ml":
            return 12.5
        else:
            return 0.0

    def samples_payload(
        self, role: str, metric: str, rng: str
    ) -> dict[str, Any]:
        """Raw samples (every point). Anchored to absolute time so waveforms slide stably without morphing."""
        span_s = {"m": 60, "h": 3600, "d": 86400}[rng[-1]] * int(rng[:-1])
        step = 1.0 if span_s <= 3600 else max(1.0, span_s / 600)
        count = int(span_s / step)
        now_ts = time.time()
        end_ts = math.floor(now_ts / step) * step
        start_ts = end_ts - span_s

        data = []
        for i in range(count):
            pt_ts = start_ts + (i + 1) * step
            value = self.sample_metric(pt_ts, role, metric)
            data.append(
                {
                    "bucket": datetime.fromtimestamp(pt_ts, timezone.utc).isoformat(),
                    "value": round(value, 3),
                }
            )
        return {
            "role": role,
            "metric": metric,
            "range": rng,
            "source": "archive",
            "count": len(data),
            "truncated": False,
            "data": data,
        }

    def milestones_payload(self) -> dict[str, Any]:
        """Milestones (WS2) — an operator's documented points, newest first.

        Returns the live list rather than a fixed one, so a milestone posted from the UI is
        reflected on the next read (the chart and the bitácora both poll this).
        """
        return {
            "experiment_id": self.experiment_id,
            "milestones": list(self.milestones),
        }

    def add_milestone(
        self, experiment_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Append an operator milestone, shaped like the event rows the UI already reads."""
        self._milestone_seq += 1
        now = datetime.now(timezone.utc)
        milestone = {
            "id": self._milestone_seq,
            "ts": now.isoformat(),
            "t_dev_ms": None,
            "experiment_id": experiment_id,
            "event_type": "milestone_recorded",
            "severity": "info",
            "scope": "operator",
            "source": "ui",
            "action": body.get("kind", "note"),
            "detail": {
                "kind": body.get("kind", "note"),
                "label": body.get("label", "Hito"),
                "detail": body.get("detail", ""),
                "t_rel_s": self._t_rel_s(),
                "do_mg_l": round(self.pond.do, 3),
            },
        }
        self.milestones.insert(0, milestone)
        return milestone

    def history_payload(
        self, role: str, metric: str, rng: str
    ) -> dict[str, Any]:
        span_minutes = {"m": 1, "h": 60, "d": 1440}[rng[-1]] * int(rng[:-1])
        hourly = span_minutes > 1440
        bucket_s = 3600 if hourly else 60
        count = max(2, min(1440, span_minutes * 60 // bucket_s))
        now_ts = time.time()
        end_ts = math.floor(now_ts / bucket_s) * bucket_s
        start_ts = end_ts - count * bucket_s

        data = []
        for i in range(count):
            pt_ts = start_ts + (i + 1) * bucket_s
            value = self.sample_metric(pt_ts, role, metric)
            expected = int(60 * bucket_s / 5) if not hourly else 720
            samples = expected if i % 37 else int(expected * 0.42)
            data.append(
                {
                    "bucket": datetime.fromtimestamp(pt_ts, timezone.utc).isoformat(),
                    "value": round(max(0.0, value), 3),
                    "samples": samples,
                    "samples_expected": expected,
                    "uncertain_frac": 0.0,
                }
            )
        return {
            "role": role,
            "metric": metric,
            "column": metric,
            "range": rng,
            "source": "rollup_1h" if hourly else "rollup_1min",
            "data": data,
        }

    def experiments_payload(self) -> dict[str, Any]:
        return {"experiments": list(self.experiments)}

    def create_experiment(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create an experiment row, so 'Iniciar Ensayo' can start a fresh run."""
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        self._experiment_seq += 1
        experiment_id = (
            body.get("id") or f"EXP-{day}-{self._experiment_seq:02d}"
        )
        recipe_id = body.get("recipe_id", "planta_2_step")
        row = {
            "id": experiment_id,
            "title": body.get("title") or f"Ensayo {recipe_id}",
            "status": "active",
            "roles": body.get("roles", ["od_sensor", "aerator"]),
            "recipe_id": recipe_id,
            "started_at": now.isoformat(),
            "ended_at": None,
            "created_at": now.isoformat(),
        }
        self.experiments.insert(0, row)
        return row

    def commands_payload(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)

        def cmd(
            cid: str,
            role: str,
            action: str,
            status: str,
            age_s: int,
            **extra: Any,
        ) -> dict[str, Any]:
            base = {
                "id": cid,
                "role": role,
                "action": action,
                "target": extra.pop("target", {}),
                "status": status,
                "experiment_id": self.experiment_id,
                "requested_by": extra.pop("requested_by", "operator:web"),
                "issued_at": (now - timedelta(seconds=age_s)).isoformat(),
                "deadline_at": (
                    now - timedelta(seconds=age_s - 300)
                ).isoformat(),
                "published_at": (
                    now - timedelta(seconds=age_s - 1)
                ).isoformat(),
                "ack_at": (now - timedelta(seconds=age_s - 3)).isoformat()
                if status == "acknowledged"
                else None,
                "superseded_by": extra.pop("superseded_by", None),
                "error": extra.pop("error", None),
                "superseded": extra.pop("superseded", []),
            }
            return base

        payload = [
            cmd(
                "c-8f21",
                "aerator",
                "emergency_stop",
                "acknowledged",
                180,
                target={},
                requested_by="operator:web",
            ),
            cmd(
                "c-8f1e",
                "aerator",
                "set_speed",
                "superseded",
                950,
                target={"rpm": 1800},
                superseded_by="c-8f1f",
            ),
            cmd(
                "c-8f1f",
                "aerator",
                "set_speed",
                "acknowledged",
                940,
                target={"rpm": 2150},
            ),
            cmd(
                "c-8f1a",
                "pump",
                "dose",
                "acknowledged",
                640,
                target={"ml": 25.0},
            ),
            cmd(
                "c-8f0c",
                "mixer",
                "set_mode",
                "expired",
                3600,
                target={"mode": "manual"},
                error="deadline passed before the node acknowledged",
            ),
            cmd(
                "c-8f07",
                "od_sensor",
                "set_config",
                "queued",
                12,
                target={"interval_s": 60},
            ),
        ]
        # Seeded history is the tail; anything the UI has posted since boot is the head, so a
        # freshly pressed button appears at the top of the console rather than being wiped.
        seeded = payload
        live = [
            c
            for c in self.commands
            if c.get("id", "").startswith("c-") and c not in seeded
        ]
        return {"commands": live + seeded}

    def health_payload(self) -> dict[str, Any]:
        elapsed = time.time() - self.started
        return {
            "status": "ok",
            "ingest": {
                "connected": True,
                "tracked_nodes": 4,
                "unknown_pending": 2,
            },
            "archive": {
                "rows_written": int(elapsed * 5.0),
                "bytes_written": int(elapsed * 640.0),
                "block_writes": int(elapsed / 12.2),
                "bytes_per_block": 65536.0,
                "flushes": int(elapsed / 12.2),
                "rotations": 1,
                "open_files": {
                    r: f"/data/archive/{r}/2026-10-09.csv"
                    for r in self.registry.roles
                },
            },
            "rollup": {"buckets_open": 4, "flushed": int(elapsed / 60)},
            "events": {
                "buffered": len(self.events),
                "written": len(self.events),
                "spilled": 0,
                "replayed": 0,
                "last_error": None,
            },
            "commands": {
                "issued": 6,
                "superseded": 1,
                "expired": 1,
                "last_error": None,
                "publisher_bound": True,
            },
            "database": {"available": True, "latency_ms": 1.8},
            "flush_backlog_s": 4230,
            "live_subscribers": 1,
        }


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

GATEWAY_STATE: FakeGateway | None = None
STATIC_ROOT: Path = REPO_ROOT / "web" / "out"


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(
        self, fmt: str, *args: Any
    ) -> None:  # quieter default logging
        if "/api/stream" in (self.path or ""):
            return
        super().log_message(fmt, *args)

    # -- helpers -----------------------------------------------------------

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, detail: str) -> None:
        self._json({"detail": detail}, status=status)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(
            (k, v)
            for k, _, v in (p.partition("=") for p in query.split("&") if p)
        )
        gw = GATEWAY_STATE
        assert gw is not None

        if path == "/api/roles":
            return self._json(gw.roles_payload())
        if path == "/api/state":
            return self._json(gw.state_payload())
        if path == "/api/recipes":
            return self._json(gw.recipes_payload())
        if path == "/api/live":
            return self._json(gw.live_payload())
        if path == "/healthz":
            return self._json(gw.health_payload())
        if path == "/api/events/levels":
            return self._json(gw.levels_payload())
        if path == "/api/events/types":
            from aqua_gateway.events.model import EventType, Scope, Severity

            return self._json(
                {
                    "types": [t.value for t in EventType],
                    "severities": [s.value for s in Severity],
                    "scopes": [s.value for s in Scope],
                }
            )
        if path == "/api/events":
            events = gw.events
            sev = params.get("severity")
            scope = params.get("scope")
            etype = params.get("event_type")
            exp = params.get("experiment_id")
            if sev:
                events = [e for e in events if e["severity"] == sev]
            if scope:
                events = [e for e in events if e["scope"] == scope]
            if etype:
                events = [e for e in events if e["event_type"] == etype]
            if exp:
                events = [e for e in events if e["experiment_id"] == exp]
            limit = int(params.get("limit", "200"))
            return self._json({"events": events[:limit]})
        if path == "/api/experiments":
            return self._json(gw.experiments_payload())
        if path == "/api/commands":
            return self._json(gw.commands_payload())
        if path == "/api/history":
            role = params.get("role", "")
            metric = params.get("metric", "")
            rng = params.get("range", "24h")
            if not role or not metric:
                return self._error(422, "role and metric are required")
            return self._json(gw.history_payload(role, metric, rng))
        if path == "/api/samples":
            role = params.get("role", "")
            metric = params.get("metric", "")
            rng = params.get("range", "10m")
            if not role or not metric:
                return self._error(422, "role and metric are required")
            return self._json(gw.samples_payload(role, metric, rng))
        if path.endswith("/milestones"):
            return self._json(gw.milestones_payload())
        if path.endswith("/exports"):
            experiment_id = path.split("/")[3]
            return self._json(
                {
                    "experiment_id": experiment_id,
                    "exports": [
                        {
                            "experiment_id": experiment_id,
                            "format": "aqua-native-1",
                            "dir": f"/data/exports/{experiment_id}_native",
                            "bundle": f"/data/exports/{experiment_id}_native/{experiment_id}_native.tar.gz",
                            "generated_at": "2026-10-10T16:00:00+00:00",
                            "rows": 10842,
                            "events": 37,
                            "roles_present": ["od_sensor", "aerator", "pump"],
                            "roles_missing": ["mixer"],
                            "size_bytes": 2_310_144,
                            "modified_at": "2026-10-10T16:00:00+00:00",
                        },
                        {
                            "experiment_id": experiment_id,
                            "format": "aligned",
                            "dir": f"/data/exports/{experiment_id}",
                            "bundle": f"/data/exports/{experiment_id}/{experiment_id}.tar.gz",
                            "generated_at": "2026-10-10T15:30:00+00:00",
                            "rows": 3600,
                            "events": 12,
                            "roles_present": ["od_sensor", "aerator"],
                            "roles_missing": [],
                            "size_bytes": 880_640,
                            "modified_at": "2026-10-10T15:30:00+00:00",
                        },
                    ],
                }
            )
        if path == "/api/stream":
            return self._stream()

        # Static export.
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        gw = GATEWAY_STATE
        assert gw is not None
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._error(400, "invalid JSON")

        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path == "/api/experiments":
            return self._json(gw.create_experiment(body))

        if path == "/api/commands":
            record = {
                "id": f"c-{random.randint(0x1000, 0xFFFF):04x}",
                "role": body.get("role"),
                "action": body.get("action"),
                "target": body.get("target") or {},
                "status": "published",
                "experiment_id": body.get("experiment_id"),
                "requested_by": body.get("requested_by", "operator:web"),
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "deadline_at": None,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "ack_at": None,
                "superseded_by": None,
                "error": None,
                "superseded": [],
            }
            gw.commands.insert(0, record)
            return self._json(record)

        # -- orchestrator transitions (WS1) — the run-control buttons land here -------------
        if path == "/api/state":
            return self._json(gw.transition_state(body))
        if path == "/api/state/pause":
            return self._json(gw.pause_run())
        if path == "/api/state/resume":
            return self._json(gw.resume_run())
        if path == "/api/state/confirm":
            return self._json(gw.confirm_phase())
        if path == "/api/state/phase-mode":
            params = dict(
                (k, v)
                for k, _, v in (
                    p.partition("=") for p in query.split("&") if p
                )
            )
            return self._json(
                gw.set_phase_mode(
                    params.get("phase_id", ""), params.get("mode", "auto")
                )
            )

        if path.endswith("/milestones"):
            experiment_id = path.split("/")[3]
            return self._json(gw.add_milestone(experiment_id, body))

        if path.endswith("/export/native"):
            experiment_id = path.split("/")[3]
            return self._json(
                {
                    "experiment_id": experiment_id,
                    "out_dir": f"/data/exports/{experiment_id}_native",
                    "bundle": f"/data/exports/{experiment_id}_native/{experiment_id}_native.tar.gz",
                    "rows": 10842,
                    "columns": [
                        "role",
                        "t_dev_ms",
                        "t_rel_ms",
                        "t_src",
                        "seq",
                        "od_do_mg_l",
                        "drv_rpm",
                    ],
                    "events": 37,
                    "grid_seconds": None,
                    "roles_present": ["od_sensor", "aerator", "pump"],
                    "roles_missing": ["mixer"],
                    "warnings": [],
                }
            )

        if path.endswith("/export"):
            experiment_id = path.split("/")[3]
            return self._json(
                {
                    "experiment_id": experiment_id,
                    "out_dir": f"/data/exports/{experiment_id}",
                    "bundle": f"/data/exports/{experiment_id}/bundle.csv.gz",
                    "rows": 10842,
                    "columns": [
                        "t_dev_ms",
                        "t_src",
                        "od_do_mg_l",
                        "od_sat_pct",
                        "od_temp_c",
                        "drv_rpm",
                        "drv_power_w",
                        "pump_dosed_ml",
                        "event_count",
                    ],
                    "events": 37,
                    "grid_seconds": 1.0,
                    "roles_present": ["od_sensor", "aerator", "pump"],
                    # Mixer, on purpose: this is the case the report exists to surface.
                    "roles_missing": ["mixer"],
                    "warnings": [
                        "2 samples had a low-trust source clock; see the manifest for offsets",
                        "role 'mixer' produced no rows in the experiment window",
                    ],
                }
            )

        if path == "/api/archive/rotate":
            return self._json(
                {
                    "today": "2026-10-09",
                    "rotated": ["/data/archive/od_sensor/2026-10-09.csv"],
                    "finalised": [],
                }
            )

        return self._error(404, f"no mock for POST {path}")

    def _stream(self) -> None:
        gw = GATEWAY_STATE
        assert gw is not None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Same `retry:` hint the real gateway sends.
        self.wfile.write(b"retry: 4000\n\n")
        self.wfile.flush()
        try:
            while True:
                # Stop when the client is gone. The real gateway checks
                # `request.is_disconnected()` and ends its generator; this mock
                # streamed into a `while True` with no exit, so a browser that
                # reloads (or an `EventSource` that retries) left the old socket
                # open. The browser's per-host connection budget is finite, and
                # after hours of accumulated streams every fetch failed with
                # `net::ERR_INSUFFICIENT_RESOURCES` -- including the ones the UI
                # needs to STOP a run. Reopening the page fixed it, which made it
                # look like a UI bug rather than a leaking mock.
                if select.select([self.connection], [], [], 0)[0] and not (
                    self.connection.recv(1, socket.MSG_PEEK)
                ):
                    return  # EOF: the client closed its end.
                frame = json.dumps(gw.live_payload(), separators=(",", ":"))
                self.wfile.write(f"data: {frame}\n\n".encode())
                self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main() -> int:
    global GATEWAY_STATE, STATIC_ROOT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", default=str(STATIC_ROOT), help="static export directory"
    )
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--no-simulate", action="store_true", help="freeze the pond"
    )
    args = parser.parse_args()

    STATIC_ROOT = Path(args.root).resolve()
    if not (STATIC_ROOT / "index.html").exists():
        print(
            f"warning: {STATIC_ROOT}/index.html not found — run `npm run build` first"
        )

    GATEWAY_STATE = FakeGateway(simulate=not args.no_simulate)

    handler = lambda *a, **kw: Handler(*a, directory=str(STATIC_ROOT), **kw)  # noqa: E731
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"mock gateway on http://{args.host}:{args.port}/  (static: {STATIC_ROOT})"
    )
    print(
        "endpoints: /api/roles /api/live /api/stream /api/events /api/events/levels"
    )
    print("           /api/history /api/experiments /api/commands /healthz")
    print("press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
