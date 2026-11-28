"""
La receta base completa, corrida contra el gemelo digital del banco (tests/sim_bench.py).

Cada escenario recorre las 7 fases dentro del orquestador real, con reloj virtual: dosis, caída,
verificación con pulsos, armado del PI del firmware, fin del lazo por estabilización y KPIs.
Los rendimientos cubren lo observado (26-02 ≈ 0.65, 26-03 ≈ 0.79) y la solución fresca (≈ 1.0).
"""

import pytest

from sim_bench import Bench, kpis, run_recipe


def _check_complete_and_conforming(res) -> dict:
    assert res.finished, "la corrida no terminó (pausa o espera)"
    reasons = res.reasons()
    assert list(reasons) == ["abc_prep", "abc_dose", "abc_deox", "abc_verify", "abc_arm", "abc_run", "abc_eval"]
    # ninguna transición por timeout ni intervención: todo por su condición
    assert set(reasons.values()) == {"condition_met"}, reasons
    ic = next(d for d in res.details("initial_condition"))
    assert ic["conforming"] is True, ic
    assert 1.0 <= ic["do_mg_l"] <= 1.1
    k = kpis(res)
    assert k is not None and k.get("iae") is not None
    assert 4.9 <= k["max_do_mg_l"] <= 5.3
    return ic


@pytest.mark.asyncio
@pytest.mark.parametrize("eta", [0.79, 1.05])
async def test_full_run_is_automatic_and_conforming(monkeypatch, eta) -> None:
    res = await run_recipe(monkeypatch, Bench(eta=eta))
    _check_complete_and_conforming(res)
    assert not res.details("complementary_dose"), "con dosis suficiente no debe redosificar"
    assert res.t_end_s / 60 < 60


@pytest.mark.asyncio
async def test_short_dose_is_completed_automatically(monkeypatch) -> None:
    # como 26-02: la caída se estanca sobre 1 mg/L; sin redosificar esperaba 45 min y armaba en ≈ 3
    res = await run_recipe(monkeypatch, Bench(eta=0.65))
    _check_complete_and_conforming(res)
    doses = res.details("complementary_dose")
    assert 1 <= len(doses) <= 2
    assert all(10 <= d["dose_ml"] <= 40 for d in doses)
    assert res.t_end_s / 60 < 60


@pytest.mark.asyncio
async def test_late_acceleration_is_capped_by_the_loop_timeout(monkeypatch) -> None:
    # la demanda se acelera durante el lazo: no se estabiliza y termina por el tope de 600 s
    res = await run_recipe(monkeypatch, Bench(eta=0.9, late_accel_at_s=1500))
    assert res.finished
    assert res.reasons()["abc_run"] in ("condition_met", "timeout")
    assert next(d for d in res.details("initial_condition"))["conforming"] is True



@pytest.mark.asyncio
async def test_setpoint_override_reaches_the_controller(monkeypatch) -> None:
    # SP = 6 enviado como target_do: antes el literal setpoint_mg_l = 5 de la receta lo pisaba
    ov = {p: {"target_do": 6.0} for p in ("abc_arm", "abc_run", "abc_eval")}
    res = await run_recipe(monkeypatch, Bench(eta=0.9, c0=6.2), overrides=ov)
    assert res.finished
    k = kpis(res)
    assert k["max_do_mg_l"] > 5.8, k
    assert res.bench.pi.sp == 6.0
