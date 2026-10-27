#!/usr/bin/env python3
"""
plot_dosing_profile.py - Simulates and visualizes a full chemical dosing cycle
including closed-loop acceleration, steady flow, cut-off at target volume,
and automatic anti-drip meniscus suck-back retraction via H-Bridge.
"""

import os
import sys
import argparse
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def simulate_dosing_cycle(
    target_ml=38.0,
    dose_rpm=1000.0,
    ml_per_rev=0.03488,
    suckback_revs=4.5,
    suckback_rpm=200.0,
    dt=0.01,
):
    """Simulates a complete dosing cycle with PI velocity control and suck-back."""
    # Estimated dosing time + buffer
    nominal_flow_ml_s = (dose_rpm / 60.0) * ml_per_rev
    est_dose_time = target_ml / max(nominal_flow_ml_s, 0.001)
    total_time = est_dose_time + 10.0
    t = np.arange(0, total_time, dt)
    n = len(t)

    rpm = np.zeros(n)
    duty = np.zeros(n)
    vol = np.zeros(n)

    # State variables
    current_rpm = 0.0
    current_duty = 0.0
    integral = 0.0
    dosed_ml = 0.0
    revs_accum = 0.0
    state = "IDLE"  # IDLE, DOSE, SUCKBACK, DONE

    # PI params (SIMC calibrated for motor shaft)
    Kp = 0.003780
    Ki = 0.026888
    min_duty = 0.10
    max_slew = 3.0  # duty fraction/s

    # Plant parameters from system identification
    K_plant = 338.099
    tau_plant = 0.1406

    # Try loading best controller if available
    try:
        json_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "results",
            "models",
            "best_controller.json",
        )
        if os.path.exists(json_path):
            import json

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                Kp = data.get("best_gains", {}).get("kp", Kp)
                Ki = data.get("best_gains", {}).get("ki", Ki)
                K_plant = data.get("plant_model", {}).get("K", K_plant)
                tau_plant = data.get("plant_model", {}).get("tau", tau_plant)
    except Exception:
        pass

    start_t = 1.0
    suckback_start_rev = 0.0
    done_idx = n

    for i in range(n):
        ti = t[i]

        # State transitions
        if ti < start_t:
            target_speed_rpm = 0.0
            state = "IDLE"
        elif state == "IDLE":
            state = "DOSE"
            target_speed_rpm = dose_rpm
        elif state == "DOSE":
            target_speed_rpm = dose_rpm
            if dosed_ml >= target_ml:
                state = "SUCKBACK"
                suckback_start_rev = revs_accum
                target_speed_rpm = -suckback_rpm
        elif state == "SUCKBACK":
            target_speed_rpm = -suckback_rpm
            revs_retracted = suckback_start_rev - revs_accum
            if revs_retracted >= suckback_revs:
                state = "DONE"
                target_speed_rpm = 0.0
                done_idx = min(i + int(2.0 / dt), n)
        elif state == "DONE":
            target_speed_rpm = 0.0

        # PI Control loop
        target_rad_s = target_speed_rpm * (2.0 * np.pi / 60.0)
        actual_rad_s = current_rpm * (2.0 * np.pi / 60.0)
        error_rad_s = target_rad_s - actual_rad_s

        if abs(target_rad_s) > 0.05:
            integral += Ki * error_rad_s * dt
            integral = np.clip(integral, -0.5, 1.0)
            u_raw = Kp * error_rad_s + integral
            sign_u = np.tanh(u_raw / 0.03)
            u_eff = (sign_u * min_duty) + u_raw * (1.0 - min_duty)
        else:
            u_eff = 0.0
            integral = 0.0

        u_eff = np.clip(u_eff, -1.0, 1.0)

        # Slew rate limiter
        delta_max = max_slew * dt
        delta = np.clip(u_eff - current_duty, -delta_max, delta_max)
        current_duty += delta

        # Plant dynamic simulation: d(omega)/dt = (K * duty - omega) / tau
        d_omega = (K_plant * current_duty - actual_rad_s) / tau_plant
        actual_rad_s += d_omega * dt
        current_rpm = actual_rad_s * (60.0 / (2.0 * np.pi))

        # Revolutions and volume
        d_rev = (current_rpm / 60.0) * dt
        revs_accum += d_rev
        if state == "DOSE" and d_rev > 0:
            dosed_ml += d_rev * ml_per_rev

        rpm[i] = current_rpm
        duty[i] = current_duty * 100.0
        vol[i] = dosed_ml

        if i >= done_idx:
            break

    return t[:done_idx], rpm[:done_idx], duty[:done_idx], vol[:done_idx]


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(
        description="Plot Full Chemical Dosing Cycle Profile"
    )
    parser.add_argument(
        "--output",
        default=os.path.join(
            SCRIPT_DIR, "results", "figures", "dosing_cycle_profile.png"
        ),
        help="Output PNG",
    )
    parser.add_argument(
        "--volume", type=float, default=38.0, help="Target dosing volume in mL"
    )
    parser.add_argument("--rpm", type=float, default=1000.0, help="Dosing speed in RPM")
    parser.add_argument(
        "--suckback_rpm", type=float, default=200.0, help="Suckback speed in RPM"
    )
    args = parser.parse_args()

    t, rpm, duty, vol = simulate_dosing_cycle(
        target_ml=args.volume, dose_rpm=args.rpm, suckback_rpm=args.suckback_rpm
    )

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(10, 8.5),
        sharex=True,
        gridspec_kw={"height_ratios": [1.8, 1.8, 1.2]},
    )
    fig.patch.set_facecolor("#ffffff")

    # 1. Rotational Speed with Suck-back
    axes[0].plot(t, rpm, color="#1f77b4", linewidth=2.0, label="Pump Speed (RPM)")
    axes[0].axhline(
        args.rpm,
        color="#d62728",
        linestyle="--",
        linewidth=1.5,
        label=f"Forward Dosing ({args.rpm:.1f} RPM)",
    )
    axes[0].axhline(
        -args.suckback_rpm,
        color="#9467bd",
        linestyle=":",
        linewidth=1.5,
        label=f"Anti-drip Suck-Back (-{args.suckback_rpm:.1f} RPM)",
    )
    axes[0].axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    axes[0].set_ylabel("Speed (RPM)", fontsize=11, fontweight="bold")
    axes[0].grid(True, linestyle="--", alpha=0.6)
    axes[0].legend(loc="lower left", frameon=True)
    axes[0].set_title(
        f"Complete Peristaltic Dosing & Anti-Drip Suck-Back Cycle ({args.volume:.1f} mL Na₂SO₃)",
        fontsize=13,
        fontweight="bold",
        pad=10,
    )

    # Annotations on Speed Plot
    cutoff_indices = np.where(vol >= args.volume)[0]
    t_cutoff = t[cutoff_indices[0]] if len(cutoff_indices) > 0 else t[-1]
    axes[0].axvline(t_cutoff, color="#d62728", linestyle=":", linewidth=1.8)
    axes[0].annotate(
        f"Target {args.volume:.1f} mL Reached\nTrigger Suck-Back",
        xy=(t_cutoff, args.rpm * 0.5),
        xytext=(max(0.0, t_cutoff - 15.0), args.rpm * 0.7),
        arrowprops=dict(arrowstyle="->", color="#d62728", lw=1.5),
        fontsize=9,
        fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.3", fc="#ffe6e6", ec="#d62728"),
    )

    # 2. Cumulative Dosed Volume
    axes[1].plot(t, vol, color="#2ca02c", linewidth=2.2, label="Integrated Volume (mL)")
    axes[1].axhline(
        args.volume,
        color="#d62728",
        linestyle="--",
        linewidth=1.5,
        label=f"Target Volume ({args.volume:.1f} mL)",
    )
    axes[1].set_ylabel("Dosed Volume (mL)", fontsize=11, fontweight="bold")
    axes[1].grid(True, linestyle="--", alpha=0.6)
    axes[1].legend(loc="lower right", frameon=True)

    # 3. H-Bridge Duty Cycle (with Smooth Zero-Crossing)
    axes[2].plot(
        t, duty, color="#ff7f0e", linewidth=1.8, label="H-Bridge Command Duty Cycle (%)"
    )
    axes[2].axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    axes[2].set_xlabel("Time (seconds)", fontsize=11, fontweight="bold")
    axes[2].set_ylabel("Duty (%)", fontsize=11, fontweight="bold")
    axes[2].grid(True, linestyle="--", alpha=0.6)
    axes[2].legend(loc="upper right", frameon=True)

    plt.tight_layout()
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    plt.savefig(args.output, dpi=200)
    print(f"[OK] Dosing Profile Plot saved to: {args.output}")


if __name__ == "__main__":
    main()
