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
    window_s = float(cond.get("window_s", 30))
    window_ms = int(window_s * 1000)
    recent = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms] if ctx.do_history else []
    if not recent or recent[-1][0] - recent[0][0] < window_ms:
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
    window_ms = int(float(cond.get("window_s", 30)) * 1000)
    recent = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms] if ctx.do_history else []
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < window_ms:
        return False
    rate = least_squares_rate_per_min(recent)
    return rate is not None and abs(rate) < float(cond["eps_mg_l_per_min"])


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


_HANDLERS = {
    "elapsed": _elapsed,
    "do_below": _do_below,
    "do_above": _do_above,
    "do_stable": _do_stable,
    "do_below_and_stable": _do_below_and_stable,
    "dose_complete": _dose_complete,
    "setpoint_reached": _setpoint_reached,
}
