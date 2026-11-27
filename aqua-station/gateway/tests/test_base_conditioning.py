"""Benchmark de control con base certificada: receta, condición inicial y trazabilidad."""

import pytest

from aqua_gateway.orchestration.engine import Orchestrator
from aqua_gateway.orchestration.recipes import BASE_DO_MG_L, BASE_TOL_MG_L, RECIPES, validate_recipes
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


def _armed(do_end: float, slope_per_min: float, rpm: float = 0.0) -> Orchestrator:
    orch = Orchestrator(FakeGateway())
    orch._recipe_id = "auto_benchmark_control_base"
    orch._experiment_id = "EXP-TEST"
    t_end = 1_000_000
    orch._do_history = [(t_end - 60_000 + i * 5_000, do_end - slope_per_min * (60 - i * 5) / 60) for i in range(13)]
    orch._last_drv_rpm = rpm
    orch._last_od_temp_c = 25.0
    return orch


@pytest.mark.asyncio
async def test_initial_condition_conforms_at_the_crossing() -> None:
    orch = _armed(BASE_DO_MG_L + 0.03, 0.08)
    await orch._record_initial_condition(RECIPES["auto_benchmark_control_base"].phases[4])
    ic = orch._initial_condition
    assert ic["conforming"] is True
    assert ic["checks"] == {"do_in_band": True, "slope_ok": True, "aerator_at_rest": True}
    assert abs(ic["slope_mg_l_per_min"] - 0.08) < 1e-3
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
async def test_legacy_recipe_has_no_base_verdict() -> None:
    orch = _armed(1.5, 0.05)
    orch._recipe_id = "auto_benchmark_control"
    await orch._record_initial_condition(RECIPES["auto_benchmark_control"].phases[5])
    assert orch._initial_condition["conforming"] is None
