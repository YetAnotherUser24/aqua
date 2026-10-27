#!/usr/bin/env python3
"""
plot_sysid.py - Visualizes System Identification data (Staircase, Step, PRBS)
and compares measured velocity against the fitted FOPDT plant model.
"""

import os
import sys
import argparse
import csv
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import lsim, lti


def read_csv_data(filepath):
    data = {}
    with open(filepath, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            for k, v in row.items():
                if k is None:
                    continue
                k_clean = k.strip()
                if k_clean not in data:
                    data[k_clean] = []
                try:
                    data[k_clean].append(float(v.strip()))
                except (ValueError, AttributeError):
                    data[k_clean].append(0.0)
    return {k: np.array(v) for k, v in data.items()}


def simulate_fopdt(t, u, K, tau, theta):
    """Simulate a continuous FOPDT model y(t) given input u(t) with delay theta."""
    dt = np.mean(np.diff(t))
    shift_steps = int(round(theta / dt)) if dt > 0 else 0
    u_delayed = np.zeros_like(u)
    if shift_steps < len(u):
        u_delayed[shift_steps:] = u[: len(u) - shift_steps]

    # State equation: d y / dt = (K * u_delayed - y) / tau
    y = np.zeros_like(t)
    for i in range(1, len(t)):
        h = t[i] - t[i - 1]
        y[i] = y[i - 1] + (h / tau) * (K * u_delayed[i - 1] - y[i - 1])
    return y


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    parser = argparse.ArgumentParser(
        description="Plot System Identification Fit"
    )
    parser.add_argument(
        "--input", default=os.path.join(SCRIPT_DIR, "data", "step_response.csv"), help="Input CSV"
    )
    parser.add_argument(
        "--output",
        default=os.path.join(SCRIPT_DIR, "results", "figures", "sysid_identification.png"),
        help="Output PNG",
    )
    parser.add_argument("--k", type=float, default=None, help="Plant gain K")
    parser.add_argument(
        "--tau", type=float, default=None, help="Time constant tau"
    )
    parser.add_argument(
        "--theta", type=float, default=None, help="Delay theta"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: input file {args.input} not found.")
        sys.exit(1)

    cols = read_csv_data(args.input)
    if not cols:
        print(f"Error: input file {args.input} is empty or unreadable.")
        sys.exit(1)

    if "Time_s" in cols:
        t = cols["Time_s"]
    elif "Time_ms" in cols:
        t = (cols["Time_ms"] - cols["Time_ms"][0]) / 1000.0
    else:
        t = np.arange(len(next(iter(cols.values())))) * 0.02

    if "Target_Duty" in cols:
        duty = cols["Target_Duty"].copy()
    elif "Compensated_Duty" in cols:
        duty = cols["Compensated_Duty"].copy()
    elif "Duty_Pct" in cols:
        duty = cols["Duty_Pct"].copy() / 100.0
    else:
        duty = next(iter(cols.values())).copy()

    if len(duty) > 0 and np.max(duty) > 1.05:
        duty = duty / 100.0

    if "Rad_s" in cols and np.max(cols["Rad_s"]) > 0.05:
        meas = cols["Rad_s"]
        unit_str = "rad/s"
    elif "RPM" in cols:
        meas = cols["RPM"]
        unit_str = "RPM"
    else:
        meas = np.zeros_like(t)
        unit_str = "rad/s"

    # Default model parameters if not passed: check fopdt_model.json
    if args.k is None:
        model_file = os.path.join(SCRIPT_DIR, "results", "models", "fopdt_model.json")
        if os.path.isfile(model_file):
            import json

            with open(model_file, "r") as f:
                p = json.load(f)
                K = float(p.get("K", 287.646))
                tau = float(p.get("tau", 0.150))
                theta = float(p.get("theta", 0.010))
        else:
            K = 287.646
            tau = 0.150
            theta = 0.010
    else:
        K = args.k
        tau = args.tau if args.tau is not None else 0.150
        theta = args.theta if args.theta is not None else 0.010

    # Simulate model
    y_sim = simulate_fopdt(t, duty, K, tau, theta)
    residual = meas - y_sim
    rmse = np.sqrt(np.mean(residual**2))
    ss_tot = np.sum((meas - np.mean(meas)) ** 2)
    ss_res = np.sum(residual**2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Create 3-panel publication quality figure
    fig, axes = plt.subplots(
        3,
        1,
        figsize=(10, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [1.2, 2.0, 1.0]},
    )
    fig.patch.set_facecolor("#ffffff")

    # Panel 1: Duty Cycle Input
    axes[0].step(
        t,
        duty * 100.0,
        color="#1f77b4",
        where="post",
        linewidth=1.8,
        label="Excitation Duty Cycle (%)",
    )
    axes[0].set_ylabel("Duty (%)", fontsize=11, fontweight="bold")
    axes[0].grid(True, linestyle="--", alpha=0.6)
    axes[0].legend(loc="upper right", frameon=True)
    axes[0].set_title(
        f"Peristaltic Pump System Identification (FOPDT Model Validation)",
        fontsize=13,
        fontweight="bold",
        pad=10,
    )

    # Panel 2: Measured vs Simulated Response
    axes[1].plot(
        t,
        meas,
        color="#d62728",
        linewidth=1.8,
        label=f"Measured ({unit_str}) [AS5600]",
    )
    axes[1].plot(
        t,
        y_sim,
        color="#2ca02c",
        linestyle="--",
        linewidth=2.0,
        label=f"FOPDT Model: G(s) = {K:.2f}/({tau:.3f}s+1) e^{{-{theta:.3f}s}}",
    )
    axes[1].set_ylabel(
        f"Velocity ({unit_str})", fontsize=11, fontweight="bold"
    )
    axes[1].grid(True, linestyle="--", alpha=0.6)
    axes[1].legend(loc="lower right", frameon=True)

    # Metrics annotation box
    metrics_text = f"Plant Parameters:\n  Gain K = {K:.3f}\n  Tau = {tau:.3f} s\n  Deadtime = {theta * 1000:.1f} ms\nFit Quality:\n  R² = {r2:.4f}\n  RMSE = {rmse:.3f} {unit_str}"
    axes[1].text(
        0.98,
        0.95,
        metrics_text,
        transform=axes[1].transAxes,
        fontsize=10,
        verticalalignment="top",
        horizontalalignment="right",
        bbox=dict(
            boxstyle="round,pad=0.5",
            facecolor="#f8f9fa",
            edgecolor="#cccccc",
            alpha=0.9,
        ),
    )

    # Panel 3: Error Residuals
    axes[2].plot(
        t,
        residual,
        color="#7f7f7f",
        linewidth=1.2,
        label="Model Residual Error e(t)",
    )
    axes[2].axhline(0, color="black", linestyle=":", linewidth=1.0)
    axes[2].set_xlabel("Time (seconds)", fontsize=11, fontweight="bold")
    axes[2].set_ylabel("Residual", fontsize=11, fontweight="bold")
    axes[2].grid(True, linestyle="--", alpha=0.6)
    axes[2].legend(loc="upper right", frameon=True)

    plt.tight_layout()
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    plt.savefig(args.output, dpi=200)
    print(f"[OK] SysID Plot generated successfully: {args.output}")
    print(f"     Fit R²: {r2:.4f} | RMSE: {rmse:.4f}")


if __name__ == "__main__":
    main()
