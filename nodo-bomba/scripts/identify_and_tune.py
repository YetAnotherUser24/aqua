#!/usr/bin/env python3
"""
identify_and_tune.py
Standalone Python System Identification & PI Gain Optimizer for Peristaltic Pump.
Ingests CSV telemetry recorded by sysid_stair_logger.py or sysid_logger.py,
identifies the FOPDT (First Order Plus Dead Time) motor plant:
    G(s) = K / (tau * s + 1) * exp(-theta * s)
and calculates optimal PI gains using SIMC and ITAE tuning criteria.
"""

import csv
import argparse
import sys
import os
import math

def load_csv(filepath):
    if not os.path.exists(filepath):
        print(f"[ERROR] Telemetry file not found: {filepath}")
        sys.exit(1)

    t, u, y = [], [], []
    with open(filepath, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        # Look for Time_s or Time_ms, Target_Duty or Duty_Pct, Rad_s or RPM
        for row in reader:
            if not row or len(row) < 4:
                continue
            try:
                time_val = float(row[0])
                duty_val = float(row[1])
                # Prefer rad/s if available (column 4 or 3), otherwise RPM
                if len(row) >= 5 and float(row[4]) > 0:
                    speed_val = float(row[4]) # Rad_s
                else:
                    speed_val = float(row[3]) # RPM
                t.append(time_val)
                u.append(duty_val)
                y.append(speed_val)
            except ValueError:
                continue

    return t, u, y

def fit_fopdt(t, u, y):
    """
    Fits FOPDT model from the largest positive step in the data.
    Finds step instant, steady-state gain K, delay theta, and time constant tau.
    """
    n = len(t)
    if n < 20:
        print("[ERROR] Insufficient data points for identification.")
        sys.exit(1)

    # Detect largest step in u
    max_step_idx = 0
    max_delta_u = 0.0
    for i in range(1, n):
        du = u[i] - u[i-1]
        if du > max_delta_u:
            max_delta_u = du
            max_step_idx = i

    if max_delta_u < 0.1:
        # Fallback: step from initial state to average high
        t_step = t[0]
        u_base = u[0]
        u_step = max(u)
        delta_u = u_step - u_base
        step_idx = 0
    else:
        step_idx = max_step_idx
        t_step = t[step_idx]
        u_base = u[step_idx - 1]
        u_step = u[step_idx]
        delta_u = u_step - u_base

    # Find baseline y before step
    base_window = max(1, step_idx)
    y_base = sum(y[max(0, step_idx - 10):step_idx]) / max(1, min(10, step_idx)) if step_idx > 0 else y[0]

    # Find steady state y after step (before next step or end)
    end_idx = n - 1
    for i in range(step_idx + 1, n):
        if abs(u[i] - u_step) > 0.05:
            end_idx = i - 1
            break

    y_ss = sum(y[max(step_idx, end_idx - 15):end_idx + 1]) / max(1, (end_idx + 1 - max(step_idx, end_idx - 15)))
    delta_y = y_ss - y_base

    if delta_u <= 0.001 or delta_y <= 0.001:
        print("[ERROR] Inadequate excitation: delta_u or delta_y too small.")
        sys.exit(1)

    # Static Gain K
    K = delta_y / delta_u

    # Target rise levels: 28.3% and 63.2%
    y_28 = y_base + 0.283 * delta_y
    y_63 = y_base + 0.632 * delta_y

    t_28 = t_step
    t_63 = t_step

    for i in range(step_idx, end_idx):
        if y[i] >= y_28 and t_28 == t_step:
            t_28 = t[i]
        if y[i] >= y_63 and t_63 == t_step:
            t_63 = t[i]
            break

    # Two-point FOPDT estimation (Smith / Sundaresan method):
    # tau = 1.5 * (t_63 - t_28)
    # theta = max(0.01, t_63 - t_step - tau)
    tau = max(0.05, 1.5 * (t_63 - t_28))
    theta = max(0.01, (t_63 - t_step) - tau)

    return K, tau, theta, delta_u, delta_y

def tune_pi(K, tau, theta):
    """
    Computes PI gains using:
    1. SIMC (Skogestad): robust, low overshoot
    2. ITAE Optimal Setpoint Tracking
    """
    # SIMC Tuning (closed loop time constant tau_c = max(tau, theta))
    tau_c = max(0.1, theta)
    kp_simc = (1.0 / K) * (tau / (tau_c + theta))
    ti_simc = min(tau, 4.0 * (tau_c + theta))
    ki_simc = kp_simc / ti_simc

    # ITAE Optimal Tuning for FOPDT:
    # Kp = (0.586 / K) * (theta / tau)^(-0.916)
    # Ti = tau / (1.03 - 0.165 * (theta / tau))
    ratio = max(0.01, theta / tau)
    kp_itae = (0.586 / K) * (ratio ** (-0.916))
    ti_itae = tau / max(0.2, (1.03 - 0.165 * ratio))
    ki_itae = kp_itae / ti_itae

    return {
        'simc': (kp_simc, ki_simc),
        'itae': (kp_itae, ki_itae)
    }

def main():
    parser = argparse.ArgumentParser(description="Peristaltic Pump System Identification and PI Tuning")
    parser.add_argument('--input', type=str, default='scripts/data/step_response.csv', help='Input CSV telemetry')
    parser.add_argument('--out_dir', type=str, default='scripts/results/models', help='Directory to save tuned gains')
    args = parser.parse_args()

    print("==============================================================")
    print("      PERISTALTIC PUMP SYSTEM IDENTIFICATION & PI TUNER       ")
    print("==============================================================")
    print(f"Loading data from: {args.input}")

    t, u, y = load_csv(args.input)
    K, tau, theta, du, dy = fit_fopdt(t, u, y)

    print("\n--------------------------------------------------------------")
    print(" 1. IDENTIFIED PLANT TRANSFER FUNCTION (FOPDT)")
    print("--------------------------------------------------------------")
    print(f"  Steady-State Gain (K):     {K:.4f}  [(rad/s)/Duty or (RPM)/Duty]")
    print(f"  Time Constant (tau):       {tau:.4f}  seconds")
    print(f"  Apparent Dead Time (theta):{theta:.4f}  seconds")
    print(f"  Plant Model:               G(s) = {K:.3f} / ({tau:.3f} s + 1) * exp(-{theta:.3f} s)")
    print("--------------------------------------------------------------")

    gains = tune_pi(K, tau, theta)
    kp_simc, ki_simc = gains['simc']
    kp_itae, ki_itae = gains['itae']

    print("\n--------------------------------------------------------------")
    print(" 2. OPTIMAL DISCRETE PI GAINS (100 Hz Loop)")
    print("--------------------------------------------------------------")
    print(f"  A) SIMC (Recomendado - Suave, Robusto, Sin Sobrepico):")
    print(f"     Kp = {kp_simc:.6f}")
    print(f"     Ki = {ki_simc:.6f}")
    print(f"     Kd = 0.000000")
    print()
    print(f"  B) ITAE (Agresivo - Mínimo Tiempo de Asentamiento):")
    print(f"     Kp = {kp_itae:.6f}")
    print(f"     Ki = {ki_itae:.6f}")
    print(f"     Kd = 0.000000")
    print("--------------------------------------------------------------")

    os.makedirs(args.out_dir, exist_ok=True)

    # Save for pipeline CLI execution
    import json
    model_file = os.path.join(args.out_dir, "fopdt_model.json")
    with open(model_file, 'w') as f:
        json.dump({'K': K, 'tau': tau, 'theta': theta}, f, indent=2)

    pidtune_file = os.path.join(args.out_dir, "pidtune_gains.txt")
    with open(pidtune_file, 'w') as f:
        f.write(f"--kp {kp_simc:.6f} --ki {ki_simc:.6f} --kd 0.000000\n")

    pso_file = os.path.join(args.out_dir, "pso_gains.txt")
    with open(pso_file, 'w') as f:
        f.write(f"--kp {kp_itae:.6f} --ki {ki_itae:.6f} --kd 0.000000\n")

    print(f"[OK] Saved SIMC gains to {pidtune_file}")
    print(f"[OK] Saved ITAE gains to {pso_file}")
    print(f"\nPara probar en caliente en el ESP32-S3 via Serial Monitor:")
    print(f"  TUNE {kp_simc:.6f} {ki_simc:.6f} 0.0")
    print("==============================================================\n")

if __name__ == '__main__':
    main()

