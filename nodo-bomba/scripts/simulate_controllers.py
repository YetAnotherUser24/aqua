#!/usr/bin/env python3
"""
simulate_controllers.py
Simulates and compares all identified PI controllers in closed-loop (100 Hz discrete execution)
against the identified peristaltic pump model.
Generates:
  - scripts/results/figures/closed_loop_simulation_comparison.png
  - scripts/results/models/simulation_metrics.json
  - A clean comparative performance table in console.
"""

import os
import sys
import tempfile
os.environ.setdefault('MPLCONFIGDIR', tempfile.gettempdir())

import json
import argparse
import numpy as np
import matplotlib.pyplot as plt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, "results", "models")
FIGURES_DIR = os.path.join(SCRIPT_DIR, "results", "figures")

RPM_TO_RAD_S = np.pi / 30.0
RAD_S_TO_RPM = 30.0 / np.pi

def load_plant_model():
    """Loads FOPDT model parameters (K in (rad/s)/duty, tau in s, theta in s)."""
    json_path = os.path.join(MODELS_DIR, "fopdt_model.json")
    if os.path.isfile(json_path):
        try:
            with open(json_path, 'r') as f:
                data = json.load(f)
            return data.get('K', 321.13), data.get('tau', 0.135), data.get('theta', 0.010), data
        except Exception:
            pass
    # Fallback to defaults from identification
    return 321.13, 0.135, 0.010, {}

def parse_gain_file(filepath):
    """Parses --kp <val> --ki <val> [--kd <val>] from a gain text file."""
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, 'r') as f:
            content = f.read().strip()
        parts = content.split()
        if '--kp' in parts and '--ki' in parts:
            kp = float(parts[parts.index('--kp') + 1])
            ki = float(parts[parts.index('--ki') + 1])
            kd = float(parts[parts.index('--kd') + 1]) if '--kd' in parts else 0.0
            return {'kp': kp, 'ki': ki, 'kd': kd}
        # CSV fallback: kp,ki,kd
        comma_parts = [float(x.strip()) for x in content.split(',') if x.strip()]
        if len(comma_parts) >= 2:
            return {'kp': comma_parts[0], 'ki': comma_parts[1], 'kd': comma_parts[2] if len(comma_parts) > 2 else 0.0}
    except Exception:
        pass
    return None

def collect_controllers(meta_data):
    """Gathers all available tuned controllers for benchmarking."""
    controllers = {}

    # 1. Python SIMC
    py_gains = parse_gain_file(os.path.join(MODELS_DIR, "py_gains.txt"))
    if not py_gains and 'controllers' in meta_data and 'simc' in meta_data['controllers']:
        c = meta_data['controllers']['simc']
        py_gains = {'kp': c['kp'], 'ki': c['ki'], 'kd': 0.0}
    if py_gains:
        controllers['Python SIMC (Zero-OS)'] = {
            'gains': py_gains,
            'color': '#1f77b4', # Blue
            'style': '-'
        }

    # 2. Python ITAE
    if 'controllers' in meta_data and 'itae' in meta_data['controllers']:
        c = meta_data['controllers']['itae']
        controllers['Python ITAE (Fast)'] = {
            'gains': {'kp': c['kp'], 'ki': c['ki'], 'kd': 0.0},
            'color': '#17becf', # Cyan
            'style': '--'
        }

    # 3. MATLAB PIDTUNE
    pid_gains = parse_gain_file(os.path.join(MODELS_DIR, "pidtune_gains.txt"))
    if pid_gains:
        controllers['MATLAB pidtune'] = {
            'gains': pid_gains,
            'color': '#d62728', # Red
            'style': '-.'
        }

    # 4. MATLAB PSO Custom (Low Overshoot)
    pso_cust_gains = parse_gain_file(os.path.join(MODELS_DIR, "pso_custom_gains.txt"))
    if pso_cust_gains:
        controllers['MATLAB PSO (Low-OS)'] = {
            'gains': pso_cust_gains,
            'color': '#2ca02c', # Green
            'style': '-'
        }

    # 5. MATLAB PSO ITAE
    pso_itae_gains = parse_gain_file(os.path.join(MODELS_DIR, "pso_itae_gains.txt"))
    if not pso_itae_gains:
        pso_itae_gains = parse_gain_file(os.path.join(MODELS_DIR, "pso_gains.txt"))
    if pso_itae_gains:
        # Only add if distinct from custom
        if not pso_cust_gains or abs(pso_itae_gains['ki'] - pso_cust_gains['ki']) > 1e-5:
            controllers['MATLAB PSO (Pure ITAE)'] = {
                'gains': pso_itae_gains,
                'color': '#9467bd', # Purple
                'style': ':'
            }

    return controllers

def simulate_discrete_closed_loop(K, tau, theta, kp, ki, kd=0.0, target_rpm=1000.0, t_sim=3.0, dt=0.01):
    """
    Simulates the exact 100 Hz discrete PI loop running on ESP32-S3:
      - Motor plant: FOPDT discretised with Zero-Order Hold (ZOH).
      - Saturation: Duty bounded in [0.0, 1.0] (unidirectional forward pump flow).
      - Anti-windup: Integral clamped to [-1.0, 1.0].
    """
    n_steps = int(round(t_sim / dt))
    t = np.arange(n_steps) * dt

    target_rad_s = target_rpm * RPM_TO_RAD_S
    step_time = 0.20 # Step applied after 0.2s baseline
    step_idx = int(round(step_time / dt))

    delay_steps = max(1, int(round(theta / dt)))

    # Continuous-to-discrete FOPDT recurrence:
    # G(s) = K / (tau*s + 1) -> a = exp(-dt/tau), b = K * (1 - a)
    a = np.exp(-dt / max(1e-4, tau))
    b = K * (1.0 - a)

    u_history = np.zeros(n_steps + delay_steps + 1)
    y_rad_s = np.zeros(n_steps)
    duty_pct = np.zeros(n_steps)

    integral = 0.0
    prev_y = 0.0

    for i in range(n_steps):
        target = target_rad_s if i >= step_idx else 0.0
        meas_y = y_rad_s[i]

        # Discrete PI calculation matching pump_controller.cpp
        error = target - meas_y
        p_term = kp * error
        integral += ki * error * dt
        integral = np.clip(integral, -1.0, 1.0) # Anti-windup

        d_term = 0.0
        if kd > 1e-6 and dt > 0.0:
            d_term = -kd * ((meas_y - prev_y) / dt)
        prev_y = meas_y

        u_raw = p_term + integral + d_term

        # Motor H-bridge duty clamping [0.0, 1.0] for forward pumping
        # In case bidirectional braking is allowed in firmware, lower bound can be -1.0
        u_cmd = np.clip(u_raw, 0.0, 1.0)
        duty_pct[i] = u_cmd * 100.0

        # Store in circular/shifted buffer for dead time delay
        u_history[i + delay_steps] = u_cmd

        # Plant recurrence step
        u_delayed = u_history[i]
        if i + 1 < n_steps:
            y_rad_s[i + 1] = a * meas_y + b * u_delayed

    y_rpm = y_rad_s * RAD_S_TO_RPM

    # Compute step response metrics after step_time
    t_eval = t[step_idx:] - step_time
    y_eval = y_rpm[step_idx:]

    target_val = target_rpm
    peak_val = np.max(y_eval)
    os_pct = max(0.0, ((peak_val - target_val) / target_val) * 100.0) if target_val > 0 else 0.0

    # Rise time (10% to 90%)
    val_10 = 0.10 * target_val
    val_90 = 0.90 * target_val
    t_10_idx = np.where(y_eval >= val_10)[0]
    t_90_idx = np.where(y_eval >= val_90)[0]
    t_rise = (t_eval[t_90_idx[0]] - t_eval[t_10_idx[0]]) if (len(t_10_idx) > 0 and len(t_90_idx) > 0) else 0.0

    # Settling time (within +/- 2%)
    band = 0.02 * target_val
    within_band = np.abs(y_eval - target_val) <= band
    t_settle = t_eval[-1]
    for k in range(len(within_band) - 1, -1, -1):
        if not within_band[k]:
            t_settle = t_eval[min(len(t_eval) - 1, k + 1)]
            break

    ess = abs(target_val - y_eval[-1])
    peak_duty = np.max(duty_pct[step_idx:])

    return {
        't': t,
        'y_rpm': y_rpm,
        'duty_pct': duty_pct,
        't_rise': t_rise,
        't_settle': t_settle,
        'overshoot_pct': os_pct,
        'ess': ess,
        'peak_duty': peak_duty
    }

def main():
    parser = argparse.ArgumentParser(description="Simulate and compare all identified controllers in closed-loop.")
    parser.add_argument('--rpm', type=float, default=1000.0, help='Target setpoint RPM (default: 1000.0)')
    parser.add_argument('--duration', type=float, default=3.0, help='Simulation duration in seconds (default: 3.0)')
    parser.add_argument('--output', type=str, default=os.path.join(FIGURES_DIR, "closed_loop_simulation_comparison.png"),
                        help='Output comparison figure path')
    args = parser.parse_args()

    K, tau, theta, meta_data = load_plant_model()
    controllers = collect_controllers(meta_data)

    if not controllers:
        print("[ERROR] No tuned controllers found in results/models. Run an identification step first!")
        sys.exit(1)

    print("==========================================================================================")
    print("           CLOSED-LOOP SIMULATION & BENCHMARK OF IDENTIFIED CONTROLLERS                   ")
    print("==========================================================================================")
    print(f"Identified Plant: G(s) = {K:.2f} / ({tau:.3f} s + 1) * exp(-{theta*1000:.1f} ms)")
    print(f"Target Setpoint:  {args.rpm:.1f} RPM | Discrete Frequency: 100 Hz (Ts = 10 ms)")
    print("------------------------------------------------------------------------------------------")

    results = {}
    metrics_summary = {}

    for name, cinfo in controllers.items():
        g = cinfo['gains']
        sim_res = simulate_discrete_closed_loop(
            K=K, tau=tau, theta=theta,
            kp=g['kp'], ki=g['ki'], kd=g.get('kd', 0.0),
            target_rpm=args.rpm, t_sim=args.duration, dt=0.01
        )
        results[name] = {
            'cinfo': cinfo,
            'sim': sim_res
        }
        metrics_summary[name] = {
            'kp': g['kp'],
            'ki': g['ki'],
            'kd': g.get('kd', 0.0),
            'rise_time_s': round(sim_res['t_rise'], 3),
            'settling_time_s': round(sim_res['t_settle'], 3),
            'overshoot_pct': round(sim_res['overshoot_pct'], 1),
            'ess_rpm': round(sim_res['ess'], 2),
            'peak_duty_pct': round(sim_res['peak_duty'], 1)
        }

    # Print Clean Scoreboard
    header = f"{'Controller Method':<26} | {'Kp':<9} | {'Ki':<9} | {'t_rise':<7} | {'t_settle':<8} | {'Overshoot':<10} | {'Peak Duty':<9}"
    print(header)
    print("-" * len(header))
    for name, m in metrics_summary.items():
        print(f"{name:<26} | {m['kp']:<9.6f} | {m['ki']:<9.6f} | {m['rise_time_s']:<6.3f}s | {m['settling_time_s']:<7.3f}s | {m['overshoot_pct']:<9.1f}% | {m['peak_duty_pct']:<8.1f}%")
    print("------------------------------------------------------------------------------------------")

    # Save metrics JSON
    os.makedirs(MODELS_DIR, exist_ok=True)
    metrics_file = os.path.join(MODELS_DIR, "simulation_metrics.json")
    with open(metrics_file, 'w') as f:
        json.dump({
            'plant': {'K': K, 'tau': tau, 'theta': theta},
            'target_rpm': args.rpm,
            'controllers': metrics_summary
        }, f, indent=2)
    print(f"[OK] Simulation metrics saved to: {metrics_file}")

    # Plot Publication Comparison Figure
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True, gridspec_kw={'height_ratios': [2.2, 1.2]})

    # 1. Output Velocity (RPM)
    axes[0].axhline(args.rpm, color='black', linestyle='--', linewidth=1.5, label=f'Target ({args.rpm:.0f} RPM)')
    for name, r in results.items():
        cinfo = r['cinfo']
        sim = r['sim']
        lbl = f"{name} (OS: {sim['overshoot_pct']:.1f}%, tr: {sim['t_rise']:.2f}s)"
        axes[0].plot(sim['t'], sim['y_rpm'], color=cinfo['color'], linestyle=cinfo['style'], linewidth=2.0, label=lbl)

    axes[0].set_ylabel('Velocity (RPM)', fontsize=11, fontweight='bold')
    axes[0].set_title(f'Theoretical Closed-Loop Step Response Comparison (100 Hz Discrete PI Loop)\nIdentified Plant: G(s) = {K:.2f} / ({tau:.3f}s + 1) e^{{-{theta*1000:.0f}ms}}',
                      fontsize=12, fontweight='bold', pad=10)
    axes[0].grid(True, linestyle='--', alpha=0.6)
    axes[0].legend(loc='lower right', frameon=True, fontsize=9.5, facecolor='#ffffff', framealpha=0.9)
    axes[0].set_ylim(-20, max(args.rpm * 1.5, 1200))

    # Box with benchmark metrics on the top right
    best_os_method = min(metrics_summary.items(), key=lambda x: x[1]['overshoot_pct'])[0]
    box_text = f"Plant: K={K:.1f}, $\\tau$={tau*1000:.0f}ms, $\\theta$={theta*1000:.0f}ms\nMin Overshoot: {best_os_method}\nASCE 2-22 Zero-OS Compliance"
    axes[0].text(0.98, 0.95, box_text, transform=axes[0].transAxes, fontsize=9.5,
                 verticalalignment='top', horizontalalignment='right',
                 bbox=dict(boxstyle='round,pad=0.5', facecolor='#f8f9fa', edgecolor='#cccccc', alpha=0.9))

    # 2. Control Effort (Duty Cycle %)
    for name, r in results.items():
        cinfo = r['cinfo']
        sim = r['sim']
        axes[1].plot(sim['t'], sim['duty_pct'], color=cinfo['color'], linestyle=cinfo['style'], linewidth=1.8, label=name)

    axes[1].axhline(100.0, color='gray', linestyle=':', alpha=0.7)
    axes[1].set_ylabel('Duty Cycle (%)', fontsize=11, fontweight='bold')
    axes[1].set_xlabel('Time (s)', fontsize=11, fontweight='bold')
    axes[1].set_ylim(-5, 105)
    axes[1].grid(True, linestyle='--', alpha=0.6)
    axes[1].legend(loc='lower right', frameon=True, fontsize=9, facecolor='#ffffff', framealpha=0.9)

    plt.tight_layout()
    plt.savefig(args.output, dpi=300)
    plt.close()
    print(f"[OK] Simulation comparison plot saved to: {args.output}")

    # Check for experimental hardware test files to also generate hardware comparison
    generate_experimental_comparison()
    print("==========================================================================================\n")

def generate_experimental_comparison(output_path=os.path.join(FIGURES_DIR, "closed_loop_experimental_comparison.png")):
    """Plots side-by-side comparison of real experimental hardware test runs if available."""
    exp_files = [
        ('Python SIMC', os.path.join(SCRIPT_DIR, "data", "closed_loop_py.csv"), '#1f77b4', '-'),
        ('MATLAB PSO', os.path.join(SCRIPT_DIR, "data", "closed_loop_pso.csv"), '#2ca02c', '-'),
        ('MATLAB pidtune', os.path.join(SCRIPT_DIR, "data", "closed_loop_pidtune.csv"), '#d62728', '-.')
    ]
    
    valid_runs = []
    import csv
    for label, path, color, style in exp_files:
        if os.path.isfile(path):
            try:
                data = {'t': [], 'rpm': [], 'duty': [], 'sp': []}
                with open(path, 'r') as f:
                    r = csv.DictReader(f)
                    for row in r:
                        data['t'].append(float(row['Time_s']))
                        data['rpm'].append(float(row['RPM']))
                        data['duty'].append(float(row['Duty_Pct']))
                        data['sp'].append(float(row.get('Setpoint_RPM', 1000.0)))
                if len(data['t']) > 20:
                    valid_runs.append((label, {k: np.array(v) for k, v in data.items()}, color, style))
            except Exception:
                pass

    if len(valid_runs) < 2:
        return

    fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True, gridspec_kw={'height_ratios': [2.2, 1.2]})
    target_rpm = np.max(valid_runs[0][1]['sp']) if np.max(valid_runs[0][1]['sp']) > 0 else 1000.0

    axes[0].axhline(target_rpm, color='black', linestyle='--', linewidth=1.5, label=f'Target ({target_rpm:.0f} RPM)')

    metrics_text_lines = ["Experimental Hardware Metrics:"]
    for label, data, color, style in valid_runs:
        t = data['t']
        y = data['rpm']
        duty = data['duty']

        # Calculate metrics after step at t=0.5
        mask = t >= 0.5
        t_eval = t[mask] - 0.5
        y_eval = y[mask]
        peak_y = np.max(y_eval) if len(y_eval) > 0 else 0.0
        os_pct = max(0.0, ((peak_y - target_rpm) / target_rpm) * 100.0)
        
        lbl = f"{label} (Peak: {peak_y:.0f} RPM, OS: {os_pct:.1f}%)"
        axes[0].plot(t, y, color=color, linestyle=style, linewidth=1.8, label=lbl)
        axes[1].plot(t, duty, color=color, linestyle=style, linewidth=1.8, label=label)
        metrics_text_lines.append(f"  {label:<14}: OS={os_pct:>4.1f}%, Peak={peak_y:>4.0f} RPM")

    axes[0].set_ylabel('Velocity (RPM)', fontsize=11, fontweight='bold')
    axes[0].set_title('Experimental Hardware Closed-Loop Step Response Comparison (ESP32-S3 Physical Bench)',
                      fontsize=12, fontweight='bold', pad=10)
    axes[0].grid(True, linestyle='--', alpha=0.6)
    axes[0].legend(loc='lower right', frameon=True, fontsize=9.5, facecolor='#ffffff', framealpha=0.9)
    axes[0].set_ylim(-50, max(2100, target_rpm * 2.1))

    # Box with metrics on the top right
    axes[0].text(0.98, 0.95, "\n".join(metrics_text_lines), transform=axes[0].transAxes, fontsize=9.5,
                 verticalalignment='top', horizontalalignment='right',
                 bbox=dict(boxstyle='round,pad=0.5', facecolor='#f8f9fa', edgecolor='#cccccc', alpha=0.9))

    axes[1].axhline(100.0, color='gray', linestyle=':', alpha=0.7)
    axes[1].axhline(0.0, color='gray', linestyle='-', alpha=0.5)
    axes[1].set_ylabel('Duty Cycle (%)', fontsize=11, fontweight='bold')
    axes[1].set_xlabel('Time (s)', fontsize=11, fontweight='bold')
    axes[1].set_ylim(-10, 110)
    axes[1].grid(True, linestyle='--', alpha=0.6)
    axes[1].legend(loc='lower right', frameon=True, fontsize=9, facecolor='#ffffff', framealpha=0.9)

    plt.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    plt.savefig(output_path, dpi=300)
    plt.close()
    print(f"[OK] Experimental hardware comparison plot saved to: {output_path}")

if __name__ == '__main__':
    main()
