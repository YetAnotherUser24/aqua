#!/usr/bin/env python3
"""
plot_sysid.py - Visualizes System Identification data (Staircase, Step, PRBS)
and compares measured velocity against the fitted FOPDT plant model.
"""

import os
import sys
import argparse
import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import lsim, lti


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


def main():
    parser = argparse.ArgumentParser(
        description="Plot System Identification Fit"
    )
    parser.add_argument(
        "--input", default="scripts/data/step_response.csv", help="Input CSV"
    )
    parser.add_argument(
        "--output",
        default="scripts/results/figures/sysid_identification.png",
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

    df = pd.read_csv(args.input)
    t = (
        df["Time_s"].values
        if "Time_s" in df.columns
        else (df["Time_ms"].values - df["Time_ms"].iloc[0]) / 1000.0
    )

    if "Target_Duty" in df.columns:
        duty = df["Target_Duty"].values.astype(float).copy()
    elif "Compensated_Duty" in df.columns:
        duty = df["Compensated_Duty"].values.astype(float).copy()
    else:
        duty = df.iloc[:, 1].values.astype(float).copy()

    if np.max(duty) > 1.05:
        duty = duty / 100.0

    if "Rad_s" in df.columns and df["Rad_s"].max() > 0.05:
        meas = df["Rad_s"].values
        unit_str = "rad/s"
    elif "RPM" in df.columns:
        meas = df["RPM"].values
        unit_str = "RPM"
    else:
        meas = np.zeros_like(t)
        unit_str = "RPM"

    # Default model parameters if not passed: check fopdt_model.json
    if args.k is None:
        model_file = "scripts/results/models/fopdt_model.json"
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
    axes[1].legend(loc="upper right", frameon=True)

    # Metrics annotation box
    metrics_text = f"Plant Parameters:\n  Gain K = {K:.3f}\n  Tau = {tau:.3f} s\n  Deadtime = {theta * 1000:.1f} ms\nFit Quality:\n  R² = {r2:.4f}\n  RMSE = {rmse:.3f} {unit_str}"
    axes[1].text(
        0.02,
        0.95,
        metrics_text,
        transform=axes[1].transAxes,
        fontsize=10,
        verticalalignment="top",
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
