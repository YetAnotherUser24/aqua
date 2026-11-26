"""
Tests for control_metrics.py (transient and energy KPIs).
"""

from __future__ import annotations

import pytest
from aqua_gateway.orchestration.control_metrics import compute_control_kpis


def test_empty_samples_returns_defaults() -> None:
    res = compute_control_kpis([], target_sp=5.00, c0=0.70)
    assert res["c0_mg_l"] == 0.70
    assert res["target_sp_mg_l"] == 5.00
    assert res["max_do_mg_l"] is None
    assert res["overshoot_pct"] == 0.0
    assert res["rise_time_s"] is None
    assert res["settling_time_5pct_s"] is None
    assert res["settling_time_2pct_s"] is None
    assert res["iae"] == 0.0
    assert res["itae"] == 0.0
    assert res["odrive_energy_wh"] == 0.0
    assert res["sample_count"] == 0


def test_ideal_step_response_kpis() -> None:
    """
    Simulate a smooth, non-overshooting step response from C0 = 0.70 to SP = 5.00 mg/L.
    Delta = 4.30 mg/L.
    10% = 0.70 + 0.43 = 1.13 mg/L
    90% = 0.70 + 3.87 = 4.57 mg/L
    5% band = [4.75, 5.25]
    2% band = [4.90, 5.10]
    """
    samples = []
    # 0 to 300 seconds
    for t in range(0, 301, 5):
        t_f = float(t)
        # Exponential approach: C(t) = 0.70 + 4.30 * (1 - exp(-t / 40.0))
        # At t=0: 0.70
        # At t=40: 0.70 + 4.30 * 0.632 = 3.418
        # At t=120: 0.70 + 4.30 * 0.950 = 4.786 (inside 5% band [4.75, 5.25])
        # At t=160: 0.70 + 4.30 * 0.9817 = 4.921 (inside 2% band [4.90, 5.10])
        import math
        c = 0.70 + 4.30 * (1.0 - math.exp(-t_f / 40.0))
        # Simulated motor power: 30W during rise, decaying to 5W steady-state
        p_w = 5.0 + 25.0 * math.exp(-t_f / 50.0)
        samples.append({"t_s": t_f, "do_mg_l": c, "drv_power_w": p_w})

    res = compute_control_kpis(samples, target_sp=5.00, c0=0.70)

    assert res["c0_mg_l"] == 0.70
    assert res["target_sp_mg_l"] == 5.00
    assert res["max_do_mg_l"] <= 5.00
    assert res["overshoot_pct"] == 0.0  # Zero overshoot imperative

    # Rise time (10% to 90%):
    # t10 is around t = 40 * ln(1/0.9) = 4.2s (first sample >= 1.13 is t=5)
    # t90 is around t = 40 * ln(1/0.1) = 92.1s (first sample >= 4.57 is t=95)
    # Rise time approx 90s
    assert res["rise_time_s"] is not None
    assert 80.0 <= res["rise_time_s"] <= 100.0

    # Settling time 5% (enters band around t=120s and stays inside)
    assert res["settling_time_5pct_s"] is not None
    assert 110.0 <= res["settling_time_5pct_s"] <= 130.0

    # Settling time 2% (enters band around t=160s and stays inside)
    assert res["settling_time_2pct_s"] is not None
    assert 150.0 <= res["settling_time_2pct_s"] <= 170.0

    # IAE and ITAE should be positive
    assert res["iae"] > 0.0
    assert res["itae"] > 0.0

    # Energy should be positive and realistic: average power ~10W * 300s = 3000 Ws = 0.83 Wh
    assert res["odrive_energy_wh"] > 0.1
    assert res["odrive_energy_wh"] < 2.0


def test_overshoot_detected_when_exceeded() -> None:
    samples = [
        {"t_s": 0.0, "do_mg_l": 0.70, "drv_power_w": 20.0},
        {"t_s": 50.0, "do_mg_l": 3.00, "drv_power_w": 20.0},
        {"t_s": 100.0, "do_mg_l": 5.43, "drv_power_w": 10.0},  # Overshoot: 5.43 > 5.00
        {"t_s": 150.0, "do_mg_l": 5.10, "drv_power_w": 5.0},
        {"t_s": 200.0, "do_mg_l": 5.00, "drv_power_w": 5.0},
    ]
    res = compute_control_kpis(samples, target_sp=5.00, c0=0.70)
    # Step height = 5.00 - 0.70 = 4.30. Overshoot = (5.43 - 5.00) / 4.30 * 100 = 10.0%
    assert res["overshoot_pct"] == 10.0
    assert res["max_do_mg_l"] == 5.43

