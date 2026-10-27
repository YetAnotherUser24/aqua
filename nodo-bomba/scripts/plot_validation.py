#!/usr/bin/env python3
"""
plot_validation.py - Generates graphical validation figures for Closed-Loop
Speed and Flow Rate tracking on the peristaltic pump.
"""

import os
import sys
import argparse
import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    parser = argparse.ArgumentParser(description="Plot Closed Loop Validation Results")
    parser.add_argument("--input", default=os.path.join(SCRIPT_DIR, "data", "closed_loop_py.csv"), help="Input CSV")
    parser.add_argument("--output", default=os.path.join(SCRIPT_DIR, "results", "figures", "closed_loop_validation.png"), help="Output PNG")
    parser.add_argument("--ml_per_rev", type=float, default=1.00, help="mL per revolution")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: input file {args.input} not found.")
        sys.exit(1)

    cols = read_csv_data(args.input)
    if not cols:
        print(f"Error: input file {args.input} is empty or unreadable.")
        sys.exit(1)

    if 'Time_s' in cols:
        t = cols['Time_s']
    elif 'Time_ms' in cols:
        t = (cols['Time_ms'] - cols['Time_ms'][0]) / 1000.0
    else:
        t = np.arange(len(next(iter(cols.values())))) * 0.01

    if 'Setpoint_RPM' in cols:
        sp = cols['Setpoint_RPM']
    elif 'Setpoint_rad_s' in cols:
        sp = cols['Setpoint_rad_s'] * (30.0 / np.pi)
    else:
        sp = np.zeros_like(t)

    if 'RPM' in cols:
        rpm = cols['RPM']
    elif 'Rad_s' in cols:
        rpm = cols['Rad_s'] * (30.0 / np.pi)
    else:
        rpm = np.zeros_like(t)

    if 'Duty_Pct' in cols:
        duty = cols['Duty_Pct']
    elif 'Compensated_Duty' in cols:
        duty = cols['Compensated_Duty']
    elif 'Target_Duty' in cols:
        duty = cols['Target_Duty'] * 100.0
    else:
        duty = np.zeros_like(t)

    if len(duty) > 0 and np.max(duty) <= 1.05 and np.max(duty) > 0.0:
        duty = duty * 100.0

    target_rpm = np.max(sp) if len(sp) > 0 and np.max(sp) > 0.1 else 60.0

    # Virtual Flow Rate (mL/min) and Volume
    flow = rpm * args.ml_per_rev
    target_flow = target_rpm * args.ml_per_rev
    
    if 'Dosed_mL' in cols:
        volume = cols['Dosed_mL']
    else:
        # Numerically integrate forward revolutions
        dt = np.gradient(t) if len(t) > 1 else np.array([0.01])
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
    import json
    import time
    meta_path = os.path.splitext(args.input)[0] + "_meta.json"
    gains_info = None
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, 'r') as mf:
                gains_info = json.load(mf)
        except Exception:
            pass
    elif "pso" in args.input.lower() and os.path.isfile(os.path.join(SCRIPT_DIR, "results", "models", "pso_gains.txt")):
        try:
            with open(os.path.join(SCRIPT_DIR, "results", "models", "pso_gains.txt")) as gf:
                parts = gf.read().strip().split()
                if '--kp' in parts and '--ki' in parts:
                    gains_info = {'kp': float(parts[parts.index('--kp') + 1]), 'ki': float(parts[parts.index('--ki') + 1])}
        except Exception:
            pass
    elif "pidtune" in args.input.lower() and os.path.isfile(os.path.join(SCRIPT_DIR, "results", "models", "pidtune_gains.txt")):
        try:
            with open(os.path.join(SCRIPT_DIR, "results", "models", "pidtune_gains.txt")) as gf:
                parts = gf.read().strip().split()
                if '--kp' in parts and '--ki' in parts:
                    gains_info = {'kp': float(parts[parts.index('--kp') + 1]), 'ki': float(parts[parts.index('--ki') + 1])}
        except Exception:
            pass
    elif os.path.isfile(os.path.join(SCRIPT_DIR, "results", "models", "py_gains.txt")):
        try:
            with open(os.path.join(SCRIPT_DIR, "results", "models", "py_gains.txt")) as gf:
                parts = gf.read().strip().split()
                if '--kp' in parts and '--ki' in parts:
                    gains_info = {'kp': float(parts[parts.index('--kp') + 1]), 'ki': float(parts[parts.index('--ki') + 1])}
        except Exception:
            pass

    gains_str = f"  Gains: Kp={gains_info['kp']:.6f}, Ki={gains_info['ki']:.6f}\n" if (gains_info and 'kp' in gains_info and 'ki' in gains_info) else ""
    metrics_str = f"Performance Metrics:\n{gains_str}  Rise Time (10-90%): {t_rise:.3f} s\n  Settling Time (±2%): {t_settle:.3f} s\n  Overshoot: {os_pct:.1f}%\n  Steady-State Error: {ess:.3f} RPM"
    axes[0].text(0.98, 0.95, metrics_str, transform=axes[0].transAxes, fontsize=10, verticalalignment='top', horizontalalignment='right',
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

    # Persist structured validation metrics
    metrics_json_path = os.path.join(SCRIPT_DIR, 'results', 'models', 'validation_metrics.json')
    os.makedirs(os.path.dirname(metrics_json_path), exist_ok=True)
    all_metrics = {}
    if os.path.exists(metrics_json_path):
        try:
            with open(metrics_json_path, 'r') as jf:
                all_metrics = json.load(jf)
        except Exception:
            pass
    run_key = os.path.splitext(os.path.basename(args.input))[0]
    all_metrics[run_key] = {
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S'),
        "gains": gains_info if gains_info else {},
        "target_rpm": float(target_rpm),
        "rise_time_s": round(float(t_rise), 4),
        "settling_time_s": round(float(t_settle), 4),
        "overshoot_pct": round(float(os_pct), 2),
        "steady_state_error_rpm": round(float(ess), 4),
        "csv_file": args.input,
        "png_file": args.output
    }
    with open(metrics_json_path, 'w') as jf:
        json.dump(all_metrics, jf, indent=2)
    print(f"[OK] Validation metrics updated in: {metrics_json_path}")

    # Append to central run history ledger
    history_file = os.path.join(SCRIPT_DIR, 'results', 'run_history.csv')
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    file_exists = os.path.exists(history_file)
    with open(history_file, 'a', newline='') as hf:
        hwriter = csv.writer(hf)
        if not file_exists:
            hwriter.writerow(['Timestamp', 'Model_Type', 'Target_RPM', 'Kp', 'Ki', 'Kd', 'Rise_Time_s', 'Settling_Time_s', 'Overshoot_Pct', 'Ess_RPM', 'CSV_File', 'PNG_File'])
        kp_val = f"{gains_info['kp']:.6f}" if (gains_info and 'kp' in gains_info) else "N/A"
        ki_val = f"{gains_info['ki']:.6f}" if (gains_info and 'ki' in gains_info) else "N/A"
        kd_val = f"{gains_info['kd']:.6f}" if (gains_info and 'kd' in gains_info) else "0.000000"
        hwriter.writerow([
            all_metrics[run_key]['timestamp'],
            run_key,
            f"{target_rpm:.1f}",
            kp_val,
            ki_val,
            kd_val,
            f"{t_rise:.4f}",
            f"{t_settle:.4f}",
            f"{os_pct:.2f}",
            f"{ess:.4f}",
            args.input,
            args.output
        ])
    print(f"[OK] Run recorded in ledger: {history_file}")

if __name__ == "__main__":
    main()

