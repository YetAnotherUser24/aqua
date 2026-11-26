"""
KLa estimation metrics according to ASCE/EWRI 2-22 standards.

Implements the log-linear OLS parameter estimation for clean water reaeration tests:
    ln((C* - C0) / (C* - C(t))) = KLa * (t - t0)
over the standardized truncation window [0.20 * C*, 0.80 * C*].
Also computes temperature correction to standard 20 °C (ASCE theta = 1.024).
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


def compute_kla_metrics(
    samples: Sequence[Mapping[str, float]],
    c_star: float,
    *,
    water_temp_c: float = 20.0,
    trunc_lower_pct: float = 0.20,
    trunc_upper_pct: float = 0.80,
    theta: float = 1.024,
) -> dict[str, Any]:
    """
    Fit KLa via log-linear regression over [trunc_lower_pct * c_star, trunc_upper_pct * c_star].

    Each sample dict must contain:
      - 't_s': timestamp in seconds relative to the start of the aeration step (t0 = 0.0)
      - 'do_mg_l': measured dissolved oxygen concentration

    Returns a dict with:
      - kla_s: KLa in s^-1
      - kla_hr: KLa in h^-1
      - kla_20_hr: KLa corrected to standard 20 °C (ASCE 2-22)
      - r_squared: coefficient of determination R^2
      - c_star_mg_l: empirical or saturation concentration used
      - water_temp_c: average water temperature during step
      - sample_count: number of points within truncation window
      - total_points: total raw points evaluated
      - valid_fit: whether the fit met statistical criteria (points >= 3 and R^2 >= 0.80)
    """
    if not samples or c_star <= 0:
        return {
            "kla_s": None,
            "kla_hr": None,
            "kla_20_hr": None,
            "r_squared": None,
            "c_star_mg_l": c_star,
            "water_temp_c": water_temp_c,
            "sample_count": 0,
            "total_points": len(samples) if samples else 0,
            "valid_fit": False,
        }

    sorted_pts = sorted(samples, key=lambda s: float(s["t_s"]))
    c_lower = trunc_lower_pct * c_star
    c_upper = trunc_upper_pct * c_star

    valid_pts = [
        (float(s["t_s"]), float(s["do_mg_l"]))
        for s in sorted_pts
        if c_lower <= float(s["do_mg_l"]) <= c_upper and float(s["do_mg_l"]) < c_star
    ]

    if len(valid_pts) < 3:
        return {
            "kla_s": None,
            "kla_hr": None,
            "kla_20_hr": None,
            "r_squared": None,
            "c_star_mg_l": c_star,
            "water_temp_c": water_temp_c,
            "sample_count": len(valid_pts),
            "total_points": len(sorted_pts),
            "valid_fit": False,
        }

    t0_fit, c0_fit = valid_pts[0]
    # ln((C* - C0) / (C* - C(t))) = - ln((C* - C(t)) / (C* - C0))
    # y = - ln((C* - C(t)) / (C* - C0))
    ts: list[float] = []
    ys: list[float] = []
    for t, c in valid_pts:
        diff_curr = c_star - c
        diff_init = c_star - c0_fit
        if diff_curr > 0 and diff_init > 0:
            y_val = -math.log(diff_curr / diff_init)
            t_val = t - t0_fit
            ts.append(t_val)
            ys.append(y_val)

    if len(ts) < 3:
        return {
            "kla_s": None,
            "kla_hr": None,
            "kla_20_hr": None,
            "r_squared": None,
            "c_star_mg_l": c_star,
            "water_temp_c": water_temp_c,
            "sample_count": len(ts),
            "total_points": len(sorted_pts),
            "valid_fit": False,
        }

    mean_t = sum(ts) / len(ts)
    mean_y = sum(ys) / len(ys)
    num = sum((ts[i] - mean_t) * (ys[i] - mean_y) for i in range(len(ts)))
    den = sum((ts[i] - mean_t) ** 2 for i in range(len(ts)))
    sum_yy = sum((ys[i] - mean_y) ** 2 for i in range(len(ys)))

    kla_s = (num / den) if den > 0 else 0.0
    kla_hr = kla_s * 3600.0

    r_squared = (num**2) / (den * sum_yy) if den > 0 and sum_yy > 0 else 0.0

    # Temperature correction: KLa_20 = KLa_T * theta^(20 - T)
    temp_factor = theta ** (20.0 - water_temp_c)
    kla_20_hr = kla_hr * temp_factor

    valid_fit = (r_squared >= 0.80) and (kla_s > 0)

    return {
        "kla_s": round(kla_s, 6),
        "kla_hr": round(kla_hr, 3),
        "kla_20_hr": round(kla_20_hr, 3),
        "r_squared": round(r_squared, 4),
        "c_star_mg_l": round(c_star, 3),
        "water_temp_c": round(water_temp_c, 2),
        "sample_count": len(ts),
        "total_points": len(sorted_pts),
        "valid_fit": valid_fit,
    }

