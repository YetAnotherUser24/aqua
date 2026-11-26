"""
Automated control performance metrics (ASCE/EWRI 2-22 and control benchmark).

Calculates standard transient and steady-state KPIs for closed-loop dissolved oxygen
controllers:
  - Rise time (t_r, 10% to 90% of step)
  - Settling time (t_s at 5% and 2% tolerance bands)
  - Overshoot (M_p %)
  - Integrated error criteria (IAE, ITAE)
  - Electrical actuator energy (ODrive W*h)
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def compute_control_kpis(
    samples: Sequence[Mapping[str, float]],
    target_sp: float,
    *,
    c0: float | None = None,
    band_5pct: float | None = None,
    band_2pct: float | None = None,
) -> dict[str, Any]:
    """
    Compute transient and energy KPIs from a sequence of time-series samples.

    Each sample dict must contain:
      - 't_s': timestamp in seconds relative to the start of the control step (t0 = 0.0)
      - 'do_mg_l': measured dissolved oxygen concentration
      - optionally 'odrive_power_w' or 'drv_power_w': electrical power in Watts

    Returns a dict with:
      - c0_mg_l: baseline concentration
      - target_sp_mg_l: target setpoint
      - max_do_mg_l: peak DO observed
      - overshoot_pct: M_p in percent (0.0% if no overshoot)
      - rise_time_s: time from 10% to 90% of the step height (or None if not reached)
      - settling_time_5pct_s: time after which DO remains within ±5% band (or None)
      - settling_time_2pct_s: time after which DO remains within ±2% band (or None)
      - iae: Integral of Absolute Error (mg/L * s)
      - itae: Integral of Time-weighted Absolute Error (mg/L * s^2)
      - odrive_energy_wh: total electrical energy drawn by the aerator motor in Watt-hours
      - sample_count: number of valid points evaluated
    """
    if not samples:
        return {
            "c0_mg_l": c0,
            "target_sp_mg_l": target_sp,
            "max_do_mg_l": None,
            "overshoot_pct": 0.0,
            "rise_time_s": None,
            "settling_time_5pct_s": None,
            "settling_time_2pct_s": None,
            "iae": 0.0,
            "itae": 0.0,
            "odrive_energy_wh": 0.0,
            "sample_count": 0,
        }

    # Sort samples chronologically by t_s
    sorted_pts = sorted(samples, key=lambda s: s["t_s"])
    t_vals = [float(s["t_s"]) for s in sorted_pts]
    c_vals = [float(s["do_mg_l"]) for s in sorted_pts]

    # Baseline C0
    init_c0 = float(c0) if c0 is not None else c_vals[0]
    delta_sp = target_sp - init_c0

    # Max DO and overshoot
    max_do = max(c_vals)
    if delta_sp > 0.001 and max_do > target_sp:
        overshoot_pct = round(((max_do - target_sp) / delta_sp) * 100.0, 2)
    else:
        overshoot_pct = 0.0

    # Rise time (10% to 90% of step delta)
    rise_time_s: float | None = None
    if delta_sp > 0.05:
        c10 = init_c0 + 0.10 * delta_sp
        c90 = init_c0 + 0.90 * delta_sp
        t10: float | None = None
        t90: float | None = None
        for t, c in zip(t_vals, c_vals):
            if t10 is None and c >= c10:
                t10 = t
            if t90 is None and c >= c90:
                t90 = t
            if t10 is not None and t90 is not None:
                break
        if t10 is not None and t90 is not None and t90 >= t10:
            rise_time_s = round(t90 - t10, 2)

    # Settling times (5% and 2% bands of setpoint)
    b5 = band_5pct if band_5pct is not None else 0.05 * target_sp
    b2 = band_2pct if band_2pct is not None else 0.02 * target_sp

    lower_5 = target_sp - b5
    upper_5 = target_sp + b5
    lower_2 = target_sp - b2
    upper_2 = target_sp + b2

    # Find last index where signal was OUTSIDE the band
    settling_5_s: float | None = None
    settling_2_s: float | None = None

    # Check if final point is inside band
    if lower_5 <= c_vals[-1] <= upper_5:
        last_outside_idx_5 = -1
        for i, c in enumerate(c_vals):
            if c < lower_5 or c > upper_5:
                last_outside_idx_5 = i
        if last_outside_idx_5 < len(c_vals) - 1:
            settling_5_s = round(t_vals[last_outside_idx_5 + 1], 2)

    if lower_2 <= c_vals[-1] <= upper_2:
        last_outside_idx_2 = -1
        for i, c in enumerate(c_vals):
            if c < lower_2 or c > upper_2:
                last_outside_idx_2 = i
        if last_outside_idx_2 < len(c_vals) - 1:
            settling_2_s = round(t_vals[last_outside_idx_2 + 1], 2)

    # Error integrals (IAE, ITAE) and ODrive Energy (trapezoidal integration)
    iae = 0.0
    itae = 0.0
    energy_ws = 0.0

    for i in range(1, len(sorted_pts)):
        dt = t_vals[i] - t_vals[i - 1]
        if dt <= 0.0:
            continue

        e_prev = abs(target_sp - c_vals[i - 1])
        e_curr = abs(target_sp - c_vals[i])
        iae += 0.5 * (e_prev + e_curr) * dt

        it_prev = t_vals[i - 1] * e_prev
        it_curr = t_vals[i] * e_curr
        itae += 0.5 * (it_prev + it_curr) * dt

        p_prev = float(sorted_pts[i - 1].get("odrive_power_w") or sorted_pts[i - 1].get("drv_power_w") or 0.0)
        p_curr = float(sorted_pts[i].get("odrive_power_w") or sorted_pts[i].get("drv_power_w") or 0.0)
        energy_ws += 0.5 * (max(0.0, p_prev) + max(0.0, p_curr)) * dt

    odrive_energy_wh = round(energy_ws / 3600.0, 3)

    return {
        "c0_mg_l": round(init_c0, 3),
        "target_sp_mg_l": round(target_sp, 3),
        "max_do_mg_l": round(max_do, 3),
        "overshoot_pct": overshoot_pct,
        "rise_time_s": rise_time_s,
        "settling_time_5pct_s": settling_5_s,
        "settling_time_2pct_s": settling_2_s,
        "iae": round(iae, 2),
        "itae": round(itae, 2),
        "odrive_energy_wh": odrive_energy_wh,
        "sample_count": len(sorted_pts),
    }

