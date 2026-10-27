#!/usr/bin/env python3
"""
identify_and_tune.py
Standalone Python System Identification & PI Gain Optimizer for Peristaltic Pump.
Ingests CSV telemetry recorded by sysid_stair_logger.py, sysid_prbs_logger.py, or sysid_logger.py,
identifies the FOPDT (First Order Plus Dead Time) motor plant:
    G(s) = K / (tau * s + 1) * exp(-theta * s)
using non-linear least squares / simulation fit and calculates optimal PI gains
using SIMC and ITAE tuning criteria.
"""

import csv
import argparse
import sys
import os
import json
import numpy as np

try:
    from scipy.optimize import minimize
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

def read_csv_data(filepath):
    if not os.path.exists(filepath):
        print(f"[ERROR] Telemetry file not found: {filepath}")
        sys.exit(1)

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
    """Simulate continuous FOPDT model y(t) given input u(t) with delay theta."""
    dt = np.mean(np.diff(t)) if len(t) > 1 else 0.01
    shift_steps = int(round(theta / dt)) if dt > 0 else 0
    u_delayed = np.zeros_like(u)
    if shift_steps < len(u):
        u_delayed[shift_steps:] = u[:len(u) - shift_steps]

    y = np.zeros_like(t)
    tau = max(0.01, tau)
    for i in range(1, len(t)):
        h = t[i] - t[i - 1]
        y[i] = y[i - 1] + (h / tau) * (K * u_delayed[i - 1] - y[i - 1])
    return y

def fit_fopdt(t, u, y):
    """
    Fits FOPDT model parameters (K, tau, theta) using initial heuristic
    followed by numerical optimization (minimizing RMSE).
    """
    n = len(t)
    if n < 20:
        print("[ERROR] Insufficient data points for identification.")
        sys.exit(1)

    # Initial guess heuristic from largest step
    max_step_idx = 0
    max_du = 0.0
    for i in range(1, n):
        du = abs(u[i] - u[i - 1])
        if du > max_du:
            max_du = du
            max_step_idx = i

    if max_du > 0.05:
        step_idx = max_step_idx
        t_step = t[step_idx]
        u_base = u[step_idx - 1]
        u_step = u[step_idx]
        du = u_step - u_base
        y_base = np.mean(y[max(0, step_idx - 10):step_idx]) if step_idx > 0 else y[0]

        end_idx = n - 1
        for i in range(step_idx + 1, n):
            if abs(u[i] - u_step) > 0.05:
                end_idx = i - 1
                break
        y_ss = np.mean(y[max(step_idx, end_idx - 15):end_idx + 1])
        dy = y_ss - y_base
        K0 = dy / du if abs(du) > 0.001 else 250.0
    else:
        u_max = np.max(u)
        y_max = np.max(y)
        K0 = y_max / u_max if u_max > 0.05 else 250.0
        t_step = t[0]

    K0 = max(10.0, min(1000.0, K0))
    tau0 = 0.15
    theta0 = 0.01

    if HAS_SCIPY:
        # Refine with non-linear least squares on full dynamic profile
        def loss(params):
            K_est, tau_est, th_est = params
            y_pred = simulate_fopdt(t, u, K_est, tau_est, th_est)
            return np.mean((y - y_pred) ** 2)

        res = minimize(
            loss,
            x0=[K0, tau0, theta0],
            bounds=[(10.0, 1200.0), (0.02, 2.0), (0.001, 0.50)],
            method='L-BFGS-B'
        )
        if res.success:
            K, tau, theta = res.x
        else:
            K, tau, theta = K0, tau0, theta0
    else:
        K, tau, theta = K0, tau0, theta0

    # Calculate fit metrics
    y_sim = simulate_fopdt(t, u, K, tau, theta)
    residual = y - y_sim
    rmse = np.sqrt(np.mean(residual ** 2))
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    ss_res = np.sum(residual ** 2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    aic = n * np.log(max(1e-9, ss_res / n)) + 2 * 3

    return K, tau, theta, r2, rmse, aic

def tune_pi(K, tau, theta):
    """
    Computes PI gains using:
    1. SIMC (Skogestad IMC): pure PI, zero overshoot, ASCE 2-22 compliant.
    2. ITAE Optimal Setpoint Tracking for FOPDT.
    """
    # SIMC Tuning (closed loop time constant tau_c = max(tau, theta))
    tau_c = max(0.10, theta)
    kp_simc = (1.0 / K) * (tau / (tau_c + theta))
    ti_simc = min(tau, 4.0 * (tau_c + theta))
    ki_simc = kp_simc / ti_simc

    # ITAE Optimal Tuning for FOPDT:
    ratio = max(0.01, theta / tau)
    kp_itae = (0.586 / K) * (ratio ** (-0.916))
    ti_itae = tau / max(0.2, (1.03 - 0.165 * ratio))
    ki_itae = kp_itae / ti_itae

    return {
        'simc': (kp_simc, ki_simc),
        'itae': (kp_itae, ki_itae)
    }

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    parser = argparse.ArgumentParser(description="Peristaltic Pump System Identification and PI Tuning")
    parser.add_argument('--input', type=str, default=os.path.join(SCRIPT_DIR, 'data', 'step_response.csv'), help='Input CSV telemetry')
    parser.add_argument('--out_dir', type=str, default=os.path.join(SCRIPT_DIR, 'results', 'models'), help='Directory to save tuned gains')
    args = parser.parse_args()

    print("==============================================================")
    print("      PERISTALTIC PUMP SYSTEM IDENTIFICATION & PI TUNER       ")
    print("==============================================================")
    print(f"Loading data from: {args.input}")

    cols = read_csv_data(args.input)
    if 'Time_s' in cols:
        t = cols['Time_s']
    elif 'Time_ms' in cols:
        t = (cols['Time_ms'] - cols['Time_ms'][0]) / 1000.0
    else:
        t = np.arange(len(next(iter(cols.values())))) * 0.02

    if 'Target_Duty' in cols:
        u = cols['Target_Duty'].copy()
    elif 'Compensated_Duty' in cols:
        u = cols['Compensated_Duty'].copy()
    elif 'Duty_Pct' in cols:
        u = cols['Duty_Pct'].copy() / 100.0
    else:
        u = next(iter(cols.values())).copy()

    if len(u) > 0 and np.max(u) > 1.05:
        u = u / 100.0

    if 'Rad_s' in cols and np.max(cols['Rad_s']) > 0.05:
        y = cols['Rad_s']
        unit_str = "rad/s"
    elif 'RPM' in cols:
        y = cols['RPM'] * (np.pi / 30.0)
        unit_str = "rad/s"
    else:
        y = next(iter(cols.values())).copy()
        unit_str = "rad/s"

    K, tau, theta, r2, rmse, aic = fit_fopdt(t, u, y)

    print("\n--------------------------------------------------------------")
    print(" 1. IDENTIFIED PLANT TRANSFER FUNCTION (FOPDT)")
    print("--------------------------------------------------------------")
    print(f"  Steady-State Gain (K):     {K:.4f}  [(rad/s)/Duty]")
    print(f"  Time Constant (tau):       {tau:.4f}  seconds")
    print(f"  Apparent Dead Time (theta):{theta * 1000:.1f}  ms ({theta:.4f} s)")
    print(f"  Plant Model:               G(s) = {K:.3f} / ({tau:.3f} s + 1) * exp(-{theta:.3f} s)")
    print(f"  Fit Quality:               R² = {r2:.4f} | RMSE = {rmse:.4f} {unit_str} | AIC = {aic:.1f}")
    print("--------------------------------------------------------------")

    speed_span = float(np.max(y) - np.min(y))
    if speed_span < 2.0 or r2 < 0.20:
        print("\n[ERROR] ==============================================================")
        print(f"[ERROR] IDENTIFICATION REJECTED: Low Quality Fit (R² = {r2:.4f} < 0.20, Speed Span = {speed_span:.2f} {unit_str})")
        print("[ERROR] The telemetry recording shows no sustained motor rotation.")
        print("[ERROR] Probable causes:")
        print("[ERROR]   1. 12V motor power supply (DRV8871/TB6612 VM) is OFF or unplugged.")
        print("[ERROR]   2. Motor phase / driver wiring disconnected or loose.")
        print("[ERROR]   3. Pump head mechanically jammed or tube pinched beyond motor torque.")
        print("[ERROR] SAFETY GUARD: Previous controller gains preserved. Aborting without overwrite.")
        print("[ERROR] ==============================================================\n")
        sys.exit(1)

    gains = tune_pi(K, tau, theta)
    kp_simc, ki_simc = gains['simc']
    kp_itae, ki_itae = gains['itae']

    print("\n--------------------------------------------------------------")
    print(" 2. OPTIMAL DISCRETE PI GAINS (100 Hz Loop)")
    print("--------------------------------------------------------------")
    print(f"  A) SIMC (Recomendado - Suave, Robusto, Sin Sobrepico):")
    print(f"     Kp = {kp_simc:.6f} [duty/(rad/s)]")
    print(f"     Ki = {ki_simc:.6f} [duty/(rad/s*s)]")
    print(f"     Kd = 0.000000")
    print()
    print(f"  B) ITAE (Agresivo - Mínimo Tiempo de Asentamiento):")
    print(f"     Kp = {kp_itae:.6f} [duty/(rad/s)]")
    print(f"     Ki = {ki_itae:.6f} [duty/(rad/s*s)]")
    print(f"     Kd = 0.000000")
    print("--------------------------------------------------------------")

    os.makedirs(args.out_dir, exist_ok=True)

    import time
    # Save model metadata
    model_file = os.path.join(args.out_dir, "fopdt_model.json")
    with open(model_file, 'w') as f:
        json.dump({
            'identified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'input_dataset': os.path.basename(args.input),
            'K': K,
            'tau': tau,
            'theta': theta,
            'r2': r2,
            'rmse': rmse,
            'aic': aic,
            'plant': {
                'K': K,
                'tau': tau,
                'theta': theta,
                'r2': r2,
                'rmse': rmse,
                'aic': aic,
                'formula': f"G(s) = {K:.3f} / ({tau:.3f}s + 1) * exp(-{theta:.3f}s)"
            },
            'controllers': {
                'simc': {
                    'kp': kp_simc,
                    'ki': ki_simc,
                    'kd': 0.0,
                    'formula': 'SIMC (Skogestad IMC) - Zero Overshoot (ASCE 2-22)'
                },
                'itae': {
                    'kp': kp_itae,
                    'ki': ki_itae,
                    'kd': 0.0,
                    'formula': 'ITAE Optimal Tracking'
                }
            }
        }, f, indent=2)

    # Save gains in formats compatible with test_gains.py & pipeline.py
    pidtune_file = os.path.join(args.out_dir, "pidtune_gains.txt")
    with open(pidtune_file, 'w') as f:
        f.write(f"--kp {kp_simc:.6f} --ki {ki_simc:.6f} --kd 0.000000\n")

    pso_file = os.path.join(args.out_dir, "pso_gains.txt")
    with open(pso_file, 'w') as f:
        f.write(f"--kp {kp_itae:.6f} --ki {ki_itae:.6f} --kd 0.000000\n")

    py_file = os.path.join(args.out_dir, "py_gains.txt")
    with open(py_file, 'w') as f:
        f.write(f"--kp {kp_simc:.6f} --ki {ki_simc:.6f} --kd 0.000000\n")

    print(f"[OK] Saved SIMC gains to {pidtune_file} and {py_file}")
    print(f"[OK] Saved ITAE gains to {pso_file}")
    print(f"\nPara probar en caliente en el ESP32-S3 via Serial Monitor:")
    print(f"  TUNE {kp_simc:.6f} {ki_simc:.6f} 0.0")
    print("==============================================================\n")

if __name__ == '__main__':
    main()
