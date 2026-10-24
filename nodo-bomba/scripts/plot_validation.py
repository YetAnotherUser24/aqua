#!/usr/bin/env python3
"""
plot_validation.py - Generates graphical validation figures for Closed-Loop
Speed and Flow Rate tracking on the peristaltic pump.
"""

import os
import sys
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def calculate_step_info(t, y, y_sp, t_start=0.5):
    """Computes rise time, settling time (2%), overshoot and steady-state error."""
    mask = t >= t_start
    t_step = t[mask] - t_start
    y_step = y[mask]
    
    if len(y_step) == 0 or y_sp == 0:
        return 0, 0, 0, 0

    # Steady state value (average of last 15%)
    n_tail = max(5, int(0.15 * len(y_step)))
    y_ss = np.mean(y_step[-n_tail:])
    ess = abs(y_sp - y_ss)
    
    # Peak and overshoot
    y_max = np.max(y_step)
    overshoot = max(0.0, ((y_max - y_sp) / y_sp) * 100.0) if y_sp > 0 else 0.0

    # Rise time (10% to 90%)
    idx_10 = np.where(y_step >= 0.10 * y_sp)[0]
    idx_90 = np.where(y_step >= 0.90 * y_sp)[0]
    t_rise = (t_step[idx_90[0]] - t_step[idx_10[0]]) if (len(idx_10) > 0 and len(idx_90) > 0) else 0.0

    # Settling time (within 2% band of y_sp)
    band = 0.02 * y_sp
    outside = np.where(np.abs(y_step - y_sp) > band)[0]
    t_settle = t_step[outside[-1]] if len(outside) > 0 else 0.0

    return t_rise, t_settle, overshoot, ess

def main():
    parser = argparse.ArgumentParser(description="Plot Closed Loop Validation Results")
    parser.add_argument("--input", default="scripts/data/closed_loop_py.csv", help="Input CSV")
    parser.add_argument("--output", default="scripts/results/figures/closed_loop_validation.png", help="Output PNG")
    parser.add_argument("--ml_per_rev", type=float, default=1.00, help="mL per revolution")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: input file {args.input} not found.")
        sys.exit(1)

    df = pd.read_csv(args.input)
    t = df['Time_s'].values
    sp = df['Setpoint_RPM'].values
    rpm = df['RPM'].values
    duty = df['Duty_Pct'].values
    target_rpm = np.max(sp)

    # Virtual Flow Rate (mL/min) and Volume
    flow = rpm * args.ml_per_rev
    target_flow = target_rpm * args.ml_per_rev
    
    if 'Dosed_mL' in df.columns:
        volume = df['Dosed_mL'].values
    else:
        # Numerically integrate forward revolutions
        dt = np.gradient(t)
        volume = np.cumsum(np.maximum(0, rpm / 60.0) * dt * args.ml_per_rev)

    # Dynamic metrics
    t_rise, t_settle, os_pct, ess = calculate_step_info(t, rpm, target_rpm)

    # Create figure with 3 stacked subplots
    fig, axes = plt.subplots(3, 1, figsize=(10, 8.5), sharex=True, gridspec_kw={'height_ratios': [2.0, 1.2, 1.2]})
    fig.patch.set_facecolor('#ffffff')

    # 1. Speed Tracking (RPM)
    axes[0].plot(t, rpm, color='#1f77b4', linewidth=2.0, label='Measured Velocity (RPM) [AS5600]')
    axes[0].step(t, sp, color='#d62728', linestyle='--', linewidth=1.8, where='post', label=f'Setpoint ({target_rpm:.1f} RPM)')
    axes[0].axhspan(target_rpm * 0.98, target_rpm * 1.02, color='#2ca02c', alpha=0.15, label='±2% Settling Band')
    axes[0].set_ylabel('Speed (RPM)', fontsize=11, fontweight='bold')
    axes[0].grid(True, linestyle='--', alpha=0.6)
    axes[0].legend(loc='lower right', frameon=True)
    axes[0].set_title(f"Closed-Loop PI Velocity Control Response on ESP32-S3", fontsize=13, fontweight='bold', pad=10)

    # Performance metrics box
    metrics_str = f"Performance Metrics:\n  Rise Time (10-90%): {t_rise:.3f} s\n  Settling Time (±2%): {t_settle:.3f} s\n  Overshoot: {os_pct:.1f}%\n  Steady-State Error: {ess:.3f} RPM"
    axes[0].text(0.02, 0.95, metrics_str, transform=axes[0].transAxes, fontsize=10, verticalalignment='top',
                 bbox=dict(boxstyle='round,pad=0.5', facecolor='#f8f9fa', edgecolor='#cccccc', alpha=0.9))

    # 2. Control Effort (H-Bridge Duty Cycle)
    axes[1].plot(t, duty, color='#ff7f0e', linewidth=1.8, label='H-Bridge Duty Cycle (%)')
    axes[1].set_ylabel('Duty (%)', fontsize=11, fontweight='bold')
    axes[1].set_ylim(-5, 105)
    axes[1].grid(True, linestyle='--', alpha=0.6)
    axes[1].legend(loc='upper right', frameon=True)

    # 3. Virtual Flow Rate & Cumulative Volume
    ax3_vol = axes[2].twinx()
    l1 = axes[2].plot(t, flow, color='#9467bd', linewidth=1.8, label='Virtual Flow Rate Q (mL/min)')
    l2 = ax3_vol.plot(t, volume, color='#2ca02c', linewidth=1.8, linestyle='-.', label='Accumulated Volume (mL)')
    axes[2].set_xlabel('Time (seconds)', fontsize=11, fontweight='bold')
    axes[2].set_ylabel('Flow (mL/min)', fontsize=11, fontweight='bold', color='#9467bd')
    ax3_vol.set_ylabel('Dosed Volume (mL)', fontsize=11, fontweight='bold', color='#2ca02c')
    axes[2].grid(True, linestyle='--', alpha=0.6)
    
    # Combined legend
    lines = l1 + l2
    labels = [l.get_label() for l in lines]
    axes[2].legend(lines, labels, loc='lower right', frameon=True)

    plt.tight_layout()
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    plt.savefig(args.output, dpi=200)
    print(f"[OK] Closed-Loop Validation Plot saved to: {args.output}")
    print(f"     Rise Time: {t_rise:.3f}s | Settling: {t_settle:.3f}s | OS: {os_pct:.1f}% | ess: {ess:.3f} RPM")

if __name__ == "__main__":
    main()

