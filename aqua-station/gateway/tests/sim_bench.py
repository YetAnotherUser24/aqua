"""
Gemelo digital del banco para correr recetas completas contra el orquestador real.

Planta (paso de 1 s), calibrada con los datos del banco:
  dC/dt = [KLa(N)·θ^(T−20) + k_s·mezclador](C* − C) − r,   r = k·S·C^n,   dS/dt = dosis − r
  KLa(N): Hill (Kmax 44.5 h⁻¹, N50 218 rpm, n 4.15), k_s = 1.24 h⁻¹, C* = 6.5 mg/L, T = 27 °C.
  Sulfito en mg/L de O₂ equivalente: 80 mL de 100 g/L = 11.6 mg/L × η (rendimiento efectivo).
  Cinética ajustada a la trayectoria completa (dosis → armado, con los pulsos reales) de 26-03 y 26-02:
  k = 10^-2.76, n ≈ 0.15 (0.0 y 0.31 en cada una); η = 0.79 y 0.65, igual que el balance de O₂ (76 / 63 %).
Sensor: retardo 19 s + primer orden 18 s, muestreo 5 s, ruido gaussiano.
Aerador: el PI 2-DOF del firmware (control_engine.cpp::runPid) portado tal cual, Ts = 5 s.
Bomba: 80 mL en 169 s; el sulfito llega al tanque 20 s después (línea).

El orquestador corre con un reloj virtual: `time.time` del engine se sustituye por el reloj del
simulador y se llama `_tick()` cada segundo, como lo haría `_tick_forever`.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from aqua_gateway.orchestration import engine as engine_mod
from aqua_gateway.orchestration.engine import Orchestrator

from test_orchestration import FakeDB, FakeEvents, FakeLive

KMAX_H, N50, HILL_N, THETA, TEMP = 44.5, 218.0, 4.15, 1.024, 27.0
KS_PER_S, CSTAR = 1.24 / 3600, 6.5
TD_S, TAU_S, TS_S = 19, 18.0, 5
DOSE_EQ_MG_L_PER_80ML = 11.6
PUMP_ML_PER_S = 80.0 / 169.0
LINE_DELAY_S = 20


def kla_per_s(rpm: float) -> float:
    if rpm <= 30:
        return 0.0
    return KMAX_H * rpm**HILL_N / (N50**HILL_N + rpm**HILL_N) * THETA ** (TEMP - 20) / 3600


@dataclass
class PiState:
    kp: float = 221.65
    ki: float = 1.915
    kd: float = 0.0
    beta: float = 0.35
    tt: float = 100.0
    tau_lag: float = 30.0
    sp: float = 5.0
    min_rpm: float = 0.0
    max_rpm: float = 600.0
    integral: float = 0.0
    prev_do: float | None = None
    in_purge: bool = False

    def step(self, do: float, dt: float) -> float:
        """Port of ControlEngine::runPid (nodo-aerador/src/control_engine.cpp)."""
        prev = do if self.prev_do is None else self.prev_do
        dcdt = (do - prev) / dt if dt > 0.001 else 0.0
        self.prev_do = do
        c_pred = do + self.tau_lag * dcdt
        if self.sp <= 1.5 and do < 0.15:
            self.in_purge, self.integral = True, 0.0
            return min(max(250.0, self.min_rpm), self.max_rpm)
        if c_pred >= self.sp and dcdt > 0.0001:
            self.integral = max(0.0, self.integral - 50.0 * dt)
            self.in_purge = False
            return 0.0
        beta = 1.0 if self.sp <= 1.5 else self.beta
        error = self.sp - do
        p_target = max(beta * self.sp, do)
        error_p = (p_target - do) if self.sp > do else error
        if self.in_purge:
            self.in_purge = False
            self.integral = min(max(250.0 - self.kp * error_p, 0.0), self.max_rpm)
        u_unsat = self.kp * error_p + self.integral
        u_sat = min(max(u_unsat, self.min_rpm), self.max_rpm)
        tt = self.tt if self.tt > 0.1 else 100.0
        self.integral += self.ki * error * dt + (u_sat - u_unsat) * (dt / tt)
        self.integral = min(max(self.integral, -2 * self.max_rpm), 2 * self.max_rpm)
        return u_sat


@dataclass
class Bench:
    eta: float = 0.79                 # rendimiento efectivo de la dosis (26-03: 0.79; 26-02: 0.65)
    k: float = 10 ** -2.76            # cinética r = k·S·C^n
    n: float = 0.15
    c0: float = 6.47                  # OD inicial (antes de la dosis)
    noise: float = 0.003
    seed: int = 0
    late_accel_at_s: float | None = None   # "aceleración tardía": k ×3 desde ese instante
    t: int = 0
    C: float = field(init=False)
    S: float = 0.0
    meas: float = field(init=False)
    buf: list[float] = field(init=False)
    aer_mode: str = "manual"
    aer_running: bool = False
    aer_target: float = 0.0
    aer_rpm: float = 0.0
    pi: PiState = field(default_factory=PiState)
    mixer_on: bool = False
    pump_left_ml: float = 0.0
    pump_dosed_ml: float = 0.0
    feed_queue: list[tuple[int, float]] = field(default_factory=list)
    last_do_sample: float | None = None
    log: list[tuple[int, float, float, float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.C = self.meas = self.c0
        self.buf = [self.c0] * TD_S
        self.rng = random.Random(self.seed)

    # -- commands -------------------------------------------------------------
    def apply(self, role: str, action: str, target: dict[str, Any]) -> None:
        if role == "aerator":
            if action == "set_mode":
                self.aer_mode = str(target.get("mode", self.aer_mode))
                self._cfg(target)
            elif action == "set_config":
                self._cfg(target)
            elif action == "set_speed":
                self.aer_target = float(target.get("target_rpm", target.get("step_rpm", 0.0)))
                self.aer_running = True
            elif action == "start":
                self.aer_running = True
                if "target_rpm" in target:
                    self.aer_target = float(target["target_rpm"])
                self.pi.integral, self.pi.prev_do = 0.0, None
            elif action in ("stop", "force_off"):
                self.aer_running, self.aer_target = False, 0.0
        elif role == "mixer":
            self.mixer_on = action in ("start_mixer", "start", "set_speed")
        elif role == "pump":
            if action == "dose":
                self.pump_left_ml = float(target.get("dose_ml", 0.0))
                self.pump_dosed_ml = 0.0
            elif action == "force_off":
                self.pump_left_ml = 0.0

    def _cfg(self, target: dict[str, Any]) -> None:
        for key, attr in (("kp", "kp"), ("ki", "ki"), ("kd", "kd"), ("beta", "beta"), ("tt", "tt"),
                          ("tau_lag", "tau_lag"), ("max_rpm", "max_rpm")):
            if key in target:
                setattr(self.pi, attr, float(target[key]))
        if "target_do" in target:
            self.pi.sp = float(target["target_do"])

    # -- plant ----------------------------------------------------------------
    def step(self) -> bool:
        """Advance 1 s. Returns True when a new DO sample is due."""
        self.t += 1
        if self.pump_left_ml > 0:
            v = min(PUMP_ML_PER_S, self.pump_left_ml)
            self.pump_left_ml -= v
            self.pump_dosed_ml += v
            self.feed_queue.append((self.t + LINE_DELAY_S, v))
        while self.feed_queue and self.feed_queue[0][0] <= self.t:
            _, v = self.feed_queue.pop(0)
            self.S += self.eta * DOSE_EQ_MG_L_PER_80ML * v / 80.0
        # actuator: PI on the node when in pid mode, else the commanded speed
        if self.aer_running and self.aer_mode == "pid":
            target = self.aer_target
        elif self.aer_running:
            target = self.aer_target
        else:
            target = 0.0
        self.aer_rpm = target  # the rotor reaches speed in < 0.5 s (11-14 steps)
        k = self.k * (3.0 if self.late_accel_at_s is not None and self.t >= self.late_accel_at_s else 1.0)
        r = k * self.S * max(self.C, 0.0) ** self.n
        kla = kla_per_s(self.aer_rpm) + (KS_PER_S if self.mixer_on else 0.0)
        self.C = max(self.C + kla * (CSTAR - self.C) - r, 0.0)
        self.S = max(self.S - r, 0.0)
        self.buf.append(self.C)
        self.meas += (self.buf.pop(0) - self.meas) / TAU_S
        self.log.append((self.t, self.C, self.meas, self.S, self.aer_rpm))
        return self.t % TS_S == 0

    def sample(self) -> float:
        y = max(0.0, self.meas + self.rng.gauss(0.0, self.noise))
        if self.aer_running and self.aer_mode == "pid":
            self.aer_target = self.pi.step(y, float(TS_S))
        self.last_do_sample = y
        return y

    def power_w(self) -> float:
        return 0.0 if self.aer_rpm <= 0 else 1.5 + 14.0 * (self.aer_rpm / 360.0) ** 1.42


class SimCommands:
    def __init__(self, bench: Bench) -> None:
        self.bench = bench
        self.calls: list[dict] = []
        self.publisher: object | None = object()

    async def issue(self, **kwargs):
        self.calls.append(kwargs)
        self.bench.apply(kwargs["role"], kwargs["action"], dict(kwargs.get("target") or {}))
        return type("Rec", (), {"status": "published", "id": "x", "role": kwargs["role"], "action": kwargs["action"]})()


class SimGateway:
    def __init__(self, bench: Bench, metrics: dict) -> None:
        self.commands = SimCommands(bench)
        self.db = FakeDB()
        self.events = FakeEvents()
        self.live = FakeLive(metrics)


@dataclass
class RunResult:
    events: list[Any]
    bench: Bench
    t_end_s: int
    finished: bool

    def details(self, event: str | None = None) -> list[dict]:
        out = []
        for e in self.events:
            d = dict(getattr(e, "detail", {}) or {})
            d["_type"] = str(getattr(e, "event_type", ""))
            d["_ts"] = getattr(e, "ts", None)
            if event is None or d.get("event") == event:
                out.append(d)
        return out

    def phase_durations_min(self) -> dict[str, float]:
        ent = {d["phase_id"]: d["_ts"] for d in self.details("entered")}
        com = {d["phase_id"]: (d["_ts"], d.get("reason")) for d in self.details("completed")}
        return {p: round((com[p][0] - ent[p]) / 60, 1) for p in ent if p in com}

    def reasons(self) -> dict[str, str]:
        return {d["phase_id"]: d.get("reason") for d in self.details("completed")}


async def run_recipe(monkeypatch, bench: Bench, recipe_id: str = "auto_benchmark_control_base",
                     t_max_s: int = 3 * 3600, overrides: dict[str, dict[str, Any]] | None = None) -> RunResult:
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(engine_mod.time, "time", lambda: clock["t"])
    metrics: dict[str, dict[str, float]] = {"od_sensor": {}, "aerator": {}, "pump": {}, "mixer": {}}
    gw = SimGateway(bench, metrics)
    gw.db.notes = f"recipe:{recipe_id}"
    orch = Orchestrator(gw)

    async def _no_export(*_a, **_k):
        return None
    monkeypatch.setattr(orch, "_auto_export_and_upload", _no_export)

    def publish_sensors(new_sample: bool) -> None:
        metrics["aerator"]["drv_rpm"] = bench.aer_rpm
        metrics["aerator"]["drv_power_w"] = bench.power_w()
        metrics["pump"]["pump_dosed_ml"] = bench.pump_dosed_ml
        metrics["mixer"]["mixer_drv_rpm"] = 1000.0 if bench.mixer_on else 0.0
        orch.observe(role="aerator", row={"drv_rpm": bench.aer_rpm, "drv_power_w": bench.power_w()},
                     t_dev_ms=int(clock["t"] * 1000))
        if new_sample:
            y = bench.sample()
            metrics["od_sensor"]["od_do_mg_l"] = y
            metrics["od_sensor"]["od_temp_c"] = TEMP
            orch.observe(role="od_sensor", row={"od_do_mg_l": y, "od_temp_c": TEMP}, t_dev_ms=int(clock["t"] * 1000))

    # a minute of history before the start, like a bench that was already running
    for _ in range(60):
        clock["t"] += 1
        publish_sensors(bench.step())
    for phase_id, params in (overrides or {}).items():
        await orch.set_phase_params(phase_id, params)
    await orch.request_state(state="ACTIVE_EXPERIMENT", experiment_id="EXP-SIM", requested_by="sim")
    finished = False
    for _ in range(t_max_s):
        clock["t"] += 1
        publish_sensors(bench.step())
        await orch._tick()
        if orch._experiment_id is None:
            finished = True
            break
    return RunResult(events=list(gw.events.emitted), bench=bench, t_end_s=bench.t, finished=finished)


def fmt(res: RunResult) -> str:
    ic = next((d for d in res.details("initial_condition")), {})
    dur = res.phase_durations_min()
    return (f"η={res.bench.eta:.2f} c0={res.bench.c0:.2f} | " + " ".join(f"{k[4:]}={v}" for k, v in dur.items())
            + f" | total={res.t_end_s/60:.1f} min | C0={ic.get('do_mg_l')} y'={ic.get('slope_mg_l_per_min')} "
              f"y''={ic.get('curvature_mg_l_per_min2')} r={ic.get('residual_demand_mg_l_per_min')} "
              f"conf={ic.get('conforming')} pulsos={ic.get('pulses_in_verify')} | {res.reasons()}")


def kpis(res: RunResult) -> dict | None:
    return next((d.get("kpis") for d in res.details() if "kpis" in d), None)


def true_sulfite_at_arm(res: RunResult) -> float | None:
    ent = next((d["_ts"] for d in res.details("entered") if d["phase_id"] == "abc_arm"), None)
    if ent is None:
        return None
    t_rel = int(ent - 1_800_000_000.0)
    row = next((r for r in res.bench.log if r[0] >= t_rel), None)
    return None if row is None else row[3]


_ = math  # keep import for future use
