"""
Phase exit conditions: pure predicates over the live view.

Each function answers one question — "is this phase done?" — from data the gateway already
holds, and never touches I/O. That is what makes the whole state machine testable by feeding
it a sequence of snapshots.

The trend estimator here is the same least-squares slope the derived rule engine uses for
``do_drop_rate_exceeded``. It is shared (``events/derived.py`` imports it) rather than copied,
because two implementations of "the rate of change" that disagree is a bug waiting for the
first alert that contradicts a phase transition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def least_squares_rate_per_min(points: Sequence[tuple[int, float]]) -> float | None:
    """
    Slope of ``(t_ms, value)`` in **units per minute**, or ``None`` if it cannot be told.

    ``None`` until the points span a real interval: a slope over a single instant is not a
    rate. The caller decides how much history is enough; this only refuses to invent one.
    """
    if len(points) < 2:
        return None
    if points[-1][0] - points[0][0] <= 0:
        return None
    count = len(points)
    mean_t = sum(t for t, _ in points) / count
    mean_v = sum(v for _, v in points) / count
    spread = sum((t - mean_t) ** 2 for t, _ in points)
    if spread <= 0:
        return None
    covariance = sum((t - mean_t) * (v - mean_v) for t, v in points)
    return (covariance / spread) * 60_000.0


def least_squares_quadratic_per_min(points: Sequence[tuple[int, float]]) -> tuple[float, float] | None:
    """
    Quadratic least-squares fit of ``(t_ms, value)``: ``(slope at the last point [units/min],
    curvature d²v/dt² [units/min²])``, or ``None`` if it cannot be told (fewer than 4 points or a
    degenerate span).

    The curvature tells a rise that is still accelerating (sulfite running out) from one that is
    decaying (the tail of a pulse seen through the sensor lag, with demand still present). With a
    first-order sensor of time constant τ, the true rate is ``slope + τ·curvature``.
    """
    if len(points) < 4 or points[-1][0] - points[0][0] <= 0:
        return None
    t_end = points[-1][0]
    xs = [(t - t_end) / 60_000.0 for t, _ in points]
    ys = [v for _, v in points]
    s = [sum(x ** k for x in xs) for k in range(5)]
    b = [sum(y * x ** k for x, y in zip(xs, ys)) for k in range(3)]
    m = [[s[0], s[1], s[2]], [s[1], s[2], s[3]], [s[2], s[3], s[4]]]

    def det(a: list[list[float]]) -> float:
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))

    d = det(m)
    if abs(d) < 1e-12:
        return None
    coef = []
    for col in range(3):
        mc = [row[:] for row in m]
        for r in range(3):
            mc[r][col] = b[r]
        coef.append(det(mc) / d)
    return coef[1], 2.0 * coef[2]


@dataclass(frozen=True, slots=True)
class PhaseContext:
    """
    Everything a predicate needs, assembled by the engine.

    Deliberately a plain bundle rather than a reference to the Gateway: a predicate that
    could reach the database or issue commands would be untestable, and the whole point of
    this module is that it is not.
    """

    #: Latest value per ``(role, field)``. Missing keys read as ``None``.
    metrics: Mapping[tuple[str, str], float]
    #: Recent ``(t_ms, do_mg_l)`` history for the oxygen trend, oldest first.
    do_history: Sequence[tuple[int, float]]
    #: Gateway wall clock, epoch seconds — the reference for every elapsed-time check.
    now_s: float
    #: When the current phase was entered (``entered_at``), epoch seconds.
    phase_entered_s: float
    #: When the run started (``t₀``), epoch seconds. Used by relative-time conditions.
    run_started_s: float

    def value(self, role: str, field: str) -> float | None:
        return self.metrics.get((role, field))


def evaluate(condition: Mapping[str, Any], ctx: PhaseContext) -> bool:
    """
    Is the phase's exit condition satisfied?

    Unknown ``kind`` raises rather than returning ``False``: a typo'd condition silently
    returning "not yet" would stall a run until its timeout, which reads as a hardware
    problem. ``recipes.validate_recipes`` rejects them at load, so reaching here with an
    unknown kind means the two lists drifted.
    """
    kind = condition.get("kind")
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise ValueError(f"unknown condition kind {kind!r}")
    return handler(condition, ctx)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _elapsed(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    """Wall-clock timeout. Always available as a phase's safety net."""
    return ctx.now_s - ctx.phase_entered_s >= float(cond["seconds"])


def _do_below(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    do = ctx.value("od_sensor", "od_do_mg_l")
    if do is None:
        return False
    if ctx.now_s - ctx.phase_entered_s < float(cond.get("min_elapsed_s", 0)):
        return False
    return do < float(cond["threshold_mg_l"])


def _do_above(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    do = ctx.value("od_sensor", "od_do_mg_l")
    if do is None:
        return False
    return do >= float(cond["threshold_mg_l"])


def _do_stable(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    """The oxygen trend is flat to within ``eps`` and has been for ``window_s``."""
    if ctx.now_s - ctx.phase_entered_s < float(cond.get("min_elapsed_s", 0)):
        return False
    window_s = float(cond.get("window_s", 30))
    window_ms = int(window_s * 1000)
    recent = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms] if ctx.do_history else []
    # Allow 85% of window_ms to account for discrete sampling interval jitter (e.g. 5s sampling)
    if not recent or (recent[-1][0] - recent[0][0]) < (window_ms * 0.85):
        # Not enough history yet: cannot claim stability, so do not.
        return False
    rate = least_squares_rate_per_min(recent)
    if rate is None:
        return False
    return abs(rate) < float(cond["eps_mg_l_per_min"])


def _do_below_and_stable(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    if ctx.now_s - ctx.phase_entered_s < float(cond.get("min_elapsed_s", 0)):
        return False
    below = ctx.value("od_sensor", "od_do_mg_l")
    if below is None or below >= float(cond["below_mg_l"]):
        return False
    window_s = float(cond.get("window_s", 60))
    window_ms = int(window_s * 1000)
    recent = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms] if ctx.do_history else []
    # Allow 85% of window_ms to account for discrete sampling interval jitter (e.g. 5s sampling)
    if len(recent) < 2 or (recent[-1][0] - recent[0][0]) < (window_ms * 0.85):
        return False

    # Option B: Stability tube (peak-to-peak amplitude check over the full window)
    if "max_delta_mg_l" in cond:
        values = [p[1] for p in recent]
        if (max(values) - min(values)) > float(cond["max_delta_mg_l"]):
            return False

    if "eps_mg_l_per_min" in cond:
        rate = least_squares_rate_per_min(recent)
        if rate is None or abs(rate) >= float(cond["eps_mg_l_per_min"]):
            return False

    return True


def _do_above_and_stable(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    """
    Oxygen reached saturation plateau: DO >= above_mg_l and trend is flat.
    """
    if ctx.now_s - ctx.phase_entered_s < float(cond.get("min_elapsed_s", 0)):
        return False
    val = ctx.value("od_sensor", "od_do_mg_l")
    if val is None or val < float(cond.get("above_mg_l", 0.0)):
        return False
    window_s = float(cond.get("window_s", 60))
    window_ms = int(window_s * 1000)
    recent = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms] if ctx.do_history else []
    # Allow 85% of window_ms to account for discrete sampling interval jitter (e.g. 5s sampling)
    if len(recent) < 2 or (recent[-1][0] - recent[0][0]) < (window_ms * 0.85):
        return False

    if "max_delta_mg_l" in cond:
        values = [p[1] for p in recent]
        if (max(values) - min(values)) > float(cond["max_delta_mg_l"]):
            return False

    if "eps_mg_l_per_min" in cond:
        rate = least_squares_rate_per_min(recent)
        if rate is None or abs(rate) >= float(cond["eps_mg_l_per_min"]):
            return False

    return True


def _dose_complete(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    """
    The pump reported the requested volume.

    Reads the pump's own accumulated-volume field rather than assuming the command
    succeeded: a dose that was published but never pumped must not advance the phase.
    """
    delivered = ctx.value("pump", "pump_dosed_ml")
    if delivered is None:
        return False
    return delivered >= float(cond["target_ml"]) - float(cond.get("tol_ml", 0.2))


def _setpoint_reached(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    measured = ctx.value(str(cond["role"]), str(cond["field"]))
    if measured is None:
        return False
    return abs(measured - float(cond["target"])) <= float(cond.get("tol", 1.0))


def _sulfite_exhausted_post_hold(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    """
    Certifies that post-aeration hold verifies zero residual sulfite:
    1. Holds for at least min_elapsed_s (default 60 s).
    2. DO level is at or above min_do_mg_l (default 0.60 mg/L).
    3. Peak-to-peak amplitude in the window is <= max_delta_mg_l (default 0.04 mg/L).
    4. Slope is not falling chemically: rate >= min_slope_mg_l_per_min (default -0.02 mg/L/min).
    """
    # 0. Rest hold & pulse safety: do NOT certify exhaustion if aerator is running
    # or if an adaptive pulse ended too recently (< rest_cooldown_s).
    if bool(cond.get("require_rest_motor", True)):
        aerator_rpm = ctx.value("aerator", "drv_rpm")
        if aerator_rpm is not None and aerator_rpm > float(cond.get("max_rest_rpm", 5.0)):
            return False

    rest_cooldown_s = float(cond.get("rest_cooldown_s", 60.0))
    last_pulse_end_s = cond.get("last_pulse_end_s")
    if last_pulse_end_s is not None:
        if (ctx.now_s - float(last_pulse_end_s)) < rest_cooldown_s:
            return False

    min_elapsed = float(cond.get("min_elapsed_s", 60))
    if ctx.now_s - ctx.phase_entered_s < min_elapsed:
        return False

    current_do = ctx.value("od_sensor", "od_do_mg_l")
    if current_do is None and ctx.do_history:
        current_do = ctx.do_history[-1][1]
    min_do = float(cond.get("min_do_mg_l", 0.60))
    if current_do is None or current_do < min_do:
        return False

    window_s = float(cond.get("window_s", 60))
    window_ms = int(window_s * 1000)
    recent = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms] if ctx.do_history else []
    # Allow 85% of window_ms to account for discrete sampling interval jitter (e.g. 5s sampling)
    if len(recent) < 2 or (recent[-1][0] - recent[0][0]) < (window_ms * 0.85):
        return False

    # Stability tube: max - min <= max_delta_mg_l
    max_delta = float(cond.get("max_delta_mg_l", 0.04))
    values = [p[1] for p in recent]
    if (max(values) - min(values)) > max_delta:
        return False

    # Slope check: verify signal is not chemically falling
    rate = least_squares_rate_per_min(recent)
    min_slope = float(cond.get("min_slope_mg_l_per_min", -0.02))
    if rate is None or rate < min_slope:
        return False

    # Curvature check: the rise must not be decaying. After a pulse, the sensor lag keeps the
    # reading rising while the tank already slows; if sulfite is still there the slope keeps
    # falling. A slope that holds or grows means the demand is running out.
    if "min_curvature_mg_l_per_min2" in cond:
        cw_ms = int(float(cond.get("curvature_window_s", 120)) * 1000)
        recent2 = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - cw_ms]
        if len(recent2) < 4 or (recent2[-1][0] - recent2[0][0]) < (cw_ms * 0.85):
            return False
        q = least_squares_quadratic_per_min(recent2)
        if q is None or q[1] < float(cond["min_curvature_mg_l_per_min2"]):
            return False

    return True


def _loop_settled(cond: Mapping[str, Any], ctx: PhaseContext) -> bool:
    """
    The closed loop has nothing left to show: DO near the set point and flat.

    After the rise and the peak the aerator saturates at 0 rpm (u >= 0) and what remains is the
    plant drifting on its own (mixer reaeration up, residual demand down). Running on adds no
    information about the loop, so the run ends once, after ``min_elapsed_s``, the DO is within
    ``band_mg_l`` of ``setpoint`` and its least-squares slope over ``window_s`` is below
    ``eps_mg_l_per_min`` in magnitude. The phase timeout caps a loop that never settles.
    """
    if ctx.now_s - ctx.phase_entered_s < float(cond.get("min_elapsed_s", 180)):
        return False
    do = ctx.value("od_sensor", "od_do_mg_l")
    if do is None or abs(do - float(cond["setpoint"])) > float(cond.get("band_mg_l", 0.30)):
        return False
    window_ms = int(float(cond.get("window_s", 60)) * 1000)
    recent = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms] if ctx.do_history else []
    if len(recent) < 2 or (recent[-1][0] - recent[0][0]) < (window_ms * 0.85):
        return False
    rate = least_squares_rate_per_min(recent)
    return rate is not None and abs(rate) <= float(cond.get("eps_mg_l_per_min", 0.02))


_HANDLERS = {
    "elapsed": _elapsed,
    "do_below": _do_below,
    "do_above": _do_above,
    "do_stable": _do_stable,
    "do_below_and_stable": _do_below_and_stable,
    "dose_complete": _dose_complete,
    "setpoint_reached": _setpoint_reached,
    "sulfite_exhausted_post_hold": _sulfite_exhausted_post_hold,
    "do_above_and_stable": _do_above_and_stable,
    "loop_settled": _loop_settled,
}
