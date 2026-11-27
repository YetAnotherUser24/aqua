"""Benchmark de control con base certificada: receta, condición inicial y trazabilidad."""

import pytest

from aqua_gateway.orchestration.conditions import (
    PhaseContext,
    evaluate,
    least_squares_quadratic_per_min,
)
from aqua_gateway.orchestration.engine import Orchestrator
from aqua_gateway.orchestration.recipes import (
    BASE_DEOX_FLOOR,
    BASE_DO_MG_L,
    BASE_MIN_CURVATURE,
    BASE_TOL_MG_L,
    RECIPES,
    validate_recipes,
)
from aqua_gateway.version import recipe_digest

from test_orchestration import FakeGateway


def test_base_recipe_has_no_burn_and_targets_the_base() -> None:
    r = RECIPES["auto_benchmark_control_base"]
    validate_recipes({r.recipe_id: r})
    ids = [p.phase_id for p in r.phases]
    assert ids == ["abc_prep", "abc_dose", "abc_deox", "abc_verify", "abc_arm", "abc_run", "abc_eval"]
    assert [p.num for p in r.phases] == [str(i) for i in range(1, 8)]
    v = r.phases[3]
    assert v.condition["min_do_mg_l"] == BASE_DO_MG_L
    assert v.condition["min_slope_mg_l_per_min"] > 0  # una meseta no certifica
    # los pulsos frenan por debajo de la base: el sobreimpulso no puede pasarla
    assert v.params["pulse_trigger_do"] < v.params["pulse_brake_do"] < BASE_DO_MG_L
    assert v.on_timeout == "pause"
    # la receta original no cambia
    assert "abc_burn" in [p.phase_id for p in RECIPES["auto_benchmark_control"].phases]


def test_recipe_digest_is_stable_and_sensitive() -> None:
    a = recipe_digest(RECIPES["auto_benchmark_control_base"])
    assert a == recipe_digest(RECIPES["auto_benchmark_control_base"])
    assert a != recipe_digest(RECIPES["auto_benchmark_control"])


def _history(do_end: float, slope_per_min: float, curv_per_min2: float = 0.0, span_s: int = 120,
             t_end: int = 1_000_000) -> list[tuple[int, float]]:
    """DO every 5 s ending at ``do_end`` with the given slope and curvature at the last point."""
    pts = []
    for i in range(span_s // 5 + 1):
        x = -(span_s - i * 5) / 60.0  # minutes before the end
        pts.append((t_end + int(x * 60_000), do_end + slope_per_min * x + 0.5 * curv_per_min2 * x * x))
    return pts


def _armed(do_end: float, slope_per_min: float, rpm: float = 0.0, curv: float = 0.0) -> Orchestrator:
    orch = Orchestrator(FakeGateway())
    orch._recipe_id = "auto_benchmark_control_base"
    orch._experiment_id = "EXP-TEST"
    orch._do_history = _history(do_end, slope_per_min, curv)
    orch._last_drv_rpm = rpm
    orch._last_od_temp_c = 25.0
    return orch


@pytest.mark.asyncio
async def test_initial_condition_conforms_at_the_crossing() -> None:
    orch = _armed(BASE_DO_MG_L + 0.03, 0.08)
    await orch._record_initial_condition(RECIPES["auto_benchmark_control_base"].phases[4])
    ic = orch._initial_condition
    assert ic["conforming"] is True
    assert ic["checks"] == {"do_in_band": True, "slope_ok": True, "curvature_ok": True, "aerator_at_rest": True}
    assert abs(ic["slope_mg_l_per_min"] - 0.08) < 1e-3
    assert abs(ic["curvature_mg_l_per_min2"]) < 1e-6
    # C' = y' + τ·y'' (= y' sin curvatura); r = k_s·(C* − C) − C'
    assert abs(ic["true_rate_mg_l_per_min"] - 0.08) < 1e-3
    expected_r = 1.24 / 60 * (6.5 - (BASE_DO_MG_L + 0.03)) - 0.08
    assert abs(ic["residual_demand_mg_l_per_min"] - expected_r) < 1e-3
    assert orch.gateway.events.emitted, "el veredicto queda como evento"


@pytest.mark.asyncio
@pytest.mark.parametrize("do_end,slope,rpm,failed", [
    (BASE_DO_MG_L + BASE_TOL_MG_L + 0.2, 0.08, 0.0, "do_in_band"),   # cruzó tarde: residual
    (BASE_DO_MG_L + 0.02, 0.0, 0.0, "slope_ok"),                     # meseta = sulfito
    (BASE_DO_MG_L + 0.02, 0.08, 120.0, "aerator_at_rest"),           # pulso en curso
])
async def test_initial_condition_flags_each_failure(do_end, slope, rpm, failed) -> None:
    orch = _armed(do_end, slope, rpm)
    await orch._record_initial_condition(RECIPES["auto_benchmark_control_base"].phases[4])
    ic = orch._initial_condition
    assert ic["conforming"] is False
    assert ic["checks"][failed] is False


@pytest.mark.asyncio
async def test_initial_condition_flags_a_decaying_rise() -> None:
    # pendiente suficiente pero frenándose: cola de un pulso con sulfito todavía presente
    orch = _armed(BASE_DO_MG_L + 0.02, 0.08, curv=-0.10)
    await orch._record_initial_condition(RECIPES["auto_benchmark_control_base"].phases[4])
    ic = orch._initial_condition
    assert ic["conforming"] is False
    assert ic["checks"]["curvature_ok"] is False
    assert ic["checks"]["slope_ok"] is True


@pytest.mark.asyncio
async def test_legacy_recipe_has_no_base_verdict() -> None:
    orch = _armed(1.5, 0.05)
    orch._recipe_id = "auto_benchmark_control"
    await orch._record_initial_condition(RECIPES["auto_benchmark_control"].phases[5])
    assert orch._initial_condition["conforming"] is None


@pytest.mark.asyncio
async def test_pause_freezes_the_phase_clock_and_stops_a_pulse() -> None:
    import time
    orch = _armed(0.4, 0.0)
    orch._phase_index = 3  # abc_verify
    orch._phase_entered_s = time.time() - 700  # 100 s de fase antes de una pausa de 600 s
    orch._pulse_active = True
    await orch.pause(requested_by="operator:web")
    assert ("aerator", "stop") in orch.gateway.commands.actions()
    assert orch._pulse_active is False
    orch._paused_at_s -= 600  # diez minutos en pausa
    await orch.resume()
    elapsed = time.time() - orch._phase_entered_s
    assert 95 <= elapsed <= 110, elapsed  # la pausa no cuenta para la fase


@pytest.mark.asyncio
async def test_resume_after_timeout_pause_grants_a_fresh_window() -> None:
    import time
    orch = _armed(0.4, 0.0)
    orch._phase_index = 3
    orch._phase_entered_s = time.time() - 2500
    await orch.pause(requested_by="orchestrator:timeout")
    await orch.resume()
    assert time.time() - orch._phase_entered_s < 5


def test_quadratic_fit_recovers_slope_and_curvature() -> None:
    pts = _history(1.0, 0.07, 0.03)
    q = least_squares_quadratic_per_min(pts)
    assert q is not None
    assert abs(q[0] - 0.07) < 1e-5 and abs(q[1] - 0.03) < 1e-5
    assert least_squares_quadratic_per_min(pts[:3]) is None


def _verify_ctx(pts: list[tuple[int, float]], entered_s: float = 0.0) -> PhaseContext:
    now_s = pts[-1][0] / 1000.0
    return PhaseContext(
        now_s=now_s,
        phase_entered_s=now_s - 600 if entered_s == 0.0 else entered_s,
        metrics={("od_sensor", "od_do_mg_l"): pts[-1][1], ("aerator", "drv_rpm"): 0.0},
        do_history=pts,
        run_started_s=now_s - 3600,
    )


@pytest.mark.parametrize("curv,ok", [(0.0, True), (0.02, True), (-0.015, True), (-0.05, False)])
def test_verify_condition_rejects_a_rise_that_is_dying(curv, ok) -> None:
    cond = dict(RECIPES["auto_benchmark_control_base"].phases[3].condition)
    pts = _history(BASE_DO_MG_L + 0.02, 0.08, curv)
    assert evaluate(cond, _verify_ctx(pts)) is ok


def test_base_deox_ends_on_a_higher_floor_and_verify_has_room() -> None:
    r = RECIPES["auto_benchmark_control_base"]
    deox, verify = r.phases[2], r.phases[3]
    assert deox.condition["below_mg_l"] == BASE_DEOX_FLOOR == deox.params["do_floor_mg_l"]
    # al entrar a la verificación el OD queda bajo el freno de los pulsos: pueden sostenerlo bajo la base
    assert BASE_DEOX_FLOOR < verify.params["pulse_brake_do"]
    assert verify.timeout_s == verify.params["safety_timeout_s"] == 3000
    assert verify.condition["min_curvature_mg_l_per_min2"] == BASE_MIN_CURVATURE
    # la receta original conserva su piso estricto
    orig = next(p for p in RECIPES["auto_benchmark_control"].phases if p.phase_id == "abc_deox")
    assert orig.condition["below_mg_l"] == 0.25
