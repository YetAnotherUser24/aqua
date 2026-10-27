#!/usr/bin/env python3
"""
test_speed_sweep.py
Multi-Speed Closed-Loop Staircase Validation for Peristaltic Pump.
Sequentially tests multiple target RPM setpoints in a single continuous run
to evaluate tracking accuracy, linearity, duty cycle demand, and low-speed stability limits.

Generates:
  - scripts/data/speed_sweep.csv
  - scripts/results/figures/speed_sweep_validation.png
  - Console performance table
"""

import os
import sys
import tempfile
os.environ.setdefault('MPLCONFIGDIR', tempfile.gettempdir())

import time
import csv
import argparse
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import serial
import serial.tools.list_ports

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
FIGURES_DIR = os.path.join(SCRIPT_DIR, "results", "figures")
MODELS_DIR = os.path.join(SCRIPT_DIR, "results", "models")

def find_com_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        desc = (port.description or "").lower()
        if any(k in desc for k in ["usb", "serial", "ch340", "cp210", "uart"]):
            return port.device
    if len(ports) > 0:
        return ports[0].device
    return None

def load_best_gains():
    gains_file = os.path.join(MODELS_DIR, "best_gains.txt")
    if not os.path.isfile(gains_file):
        gains_file = os.path.join(MODELS_DIR, "py_gains.txt")
    if os.path.isfile(gains_file):
        try:
            with open(gains_file, 'r') as f:
                parts = f.read().strip().split()
            if '--kp' in parts and '--ki' in parts:
                kp = float(parts[parts.index('--kp') + 1])
                ki = float(parts[parts.index('--ki') + 1])
                kd = float(parts[parts.index('--kd') + 1]) if '--kd' in parts else 0.0
                return kp, ki, kd
        except Exception:
            pass
    return 0.003780, 0.026888, 0.0

def main():
    default_kp, default_ki, default_kd = load_best_gains()

    parser = argparse.ArgumentParser(description="Multi-Speed Staircase Closed-Loop Sweep")
    parser.add_argument('--port', type=str, help='Serial port (e.g. COM3). Auto-detected if omitted.')
    parser.add_argument('--speeds', type=str, default='400,800,1200,1600,2000',
                        help='Comma-separated RPM test points (default: 400,800,1200,1600,2000)')
    parser.add_argument('--step_time', type=float, default=4.0,
                        help='Duration in seconds per speed step (default: 4.0s)')
    parser.add_argument('--kp', type=float, default=default_kp, help=f'PI Proportional Gain (default: {default_kp:.6f})')
    parser.add_argument('--ki', type=float, default=default_ki, help=f'PI Integral Gain (default: {default_ki:.6f})')
    parser.add_argument('--kd', type=float, default=default_kd, help=f'PI Derivative Gain (default: {default_kd:.6f})')
    parser.add_argument('--output_csv', type=str, default=os.path.join(DATA_DIR, 'speed_sweep.csv'),
                        help='Output telemetry CSV path')
    parser.add_argument('--output_png', type=str, default=os.path.join(FIGURES_DIR, 'speed_sweep_validation.png'),
                        help='Output plot PNG path')
    args = parser.parse_args()

    port = args.port or find_com_port()
    if not port:
        print("[ERROR] Could not find an active Serial/COM port. Connect the ESP32 and check Device Manager.")
        sys.exit(1)

    speed_list = [float(s.strip()) for s in args.speeds.split(',') if s.strip()]
    if not speed_list:
        print("[ERROR] No valid speeds specified.")
        sys.exit(1)

    total_time = len(speed_list) * args.step_time + 1.0

    print("==============================================================")
    print("      PERISTALTIC PUMP MULTI-SPEED CLOSED-LOOP SWEEP          ")
    print("==============================================================")
    print(f"Port:       {port} @ 115200 baud")
    print(f"Gains:      Kp = {args.kp:.6f}, Ki = {args.ki:.6f}, Kd = {args.kd:.6f}")
    print(f"Staircase:  {speed_list} RPM ({args.step_time:.1f}s each, total ~{total_time:.1f}s)")
    print(f"Output CSV: {args.output_csv}")
    print("--------------------------------------------------------------")

    try:
        ser = serial.Serial(port=port, baudrate=115200, timeout=0.1)
        ser.setDTR(False)
        ser.setRTS(False)
    except Exception as e:
        print(f"[ERROR] Failed to open port {port}: {e}")
        print("Ensure the Serial Monitor in PlatformIO / Arduino is CLOSED.")
        sys.exit(1)

    time.sleep(1.5)  # USB CDC handshake

    # Upload gains
    tune_cmd = f"TUNE {args.kp:.6f} {args.ki:.6f} {args.kd:.6f}\n"
    print(f"[1/4] Uploading Controller Gains: {tune_cmd.strip()}")
    ser.write(tune_cmd.encode('ascii'))
    time.sleep(0.3)

    # Reset motor
    ser.write(b"STOP\n")
    time.sleep(0.5)

    # Enable SYSID streaming
    print("[2/4] Activating 50 Hz telemetry stream (SYSID ON)...")
    ser.write(b"SYSID ON\n")
    time.sleep(0.2)
    ser.reset_input_buffer()

    print("[3/4] Executing speed staircase sequence...")
    data_records = []
    start_time = time.time()
    current_idx = -1
    step_start_time = start_time + 0.5  # 0.5s baseline
    current_target_rpm = 0.0

    try:
        while time.time() - start_time < (total_time + 1.0):
            now = time.time()
            elapsed = now - start_time

            # Check if time to advance to next step
            if now >= step_start_time and current_idx < len(speed_list):
                current_idx += 1
                if current_idx < len(speed_list):
                    current_target_rpm = speed_list[current_idx]
                    cmd = f"RPM {current_target_rpm:.1f}\n".encode('ascii')
                    ser.write(cmd)
                    print(f"  [{elapsed:5.1f}s] >>> STEP {current_idx + 1}/{len(speed_list)}: Target = {current_target_rpm:.1f} RPM")
                    step_start_time = now + args.step_time
                else:
                    # Final stop
                    ser.write(b"STOP\n")
                    print(f"  [{elapsed:5.1f}s] Sequence finished. Stopping motor...")
                    break

            # Read telemetry lines
            while ser.in_waiting > 0:
                line = ser.readline().decode('ascii', errors='ignore').strip()
                if line.startswith("SYSID,"):
                    parts = line.split(',')
                    # SYSID,Time_ms,Target_Duty,Duty_Pct,RPM,Rad_s,Dosed_mL
                    if len(parts) >= 6:
                        try:
                            t_ms = float(parts[1])
                            duty_pct = float(parts[3])
                            actual_rpm = float(parts[4])
                            rad_s = float(parts[5])
                            data_records.append({
                                'Time_s': elapsed,
                                'Setpoint_RPM': current_target_rpm,
                                'Duty_Pct': duty_pct,
                                'RPM': actual_rpm,
                                'Rad_s': rad_s
                            })
                        except ValueError:
                            pass
            time.sleep(0.005)

    except KeyboardInterrupt:
        print("\n[WARNING] Interrupted by user. Stopping motor safely...")

    # Stop motor and close port
    ser.write(b"STOP\n")
    time.sleep(0.1)
    ser.write(b"SYSID OFF\n")
    time.sleep(0.1)
    ser.close()

    if len(data_records) < 10:
        print("[ERROR] Insufficient telemetry data collected. Check connections.")
        sys.exit(1)

    # Save CSV
    os.makedirs(os.path.dirname(os.path.abspath(args.output_csv)), exist_ok=True)
    with open(args.output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['Time_s', 'Setpoint_RPM', 'Duty_Pct', 'RPM', 'Rad_s'])
        writer.writeheader()
        writer.writerows(data_records)
    print(f"[OK] Telemetry saved to: {args.output_csv} ({len(data_records)} samples)")

    # Data arrays
    t = np.array([r['Time_s'] for r in data_records])
    sp = np.array([r['Setpoint_RPM'] for r in data_records])
    rpm = np.array([r['RPM'] for r in data_records])
    duty = np.array([r['Duty_Pct'] for r in data_records])

    # Analyze performance per step
    step_stats = []
    for s_target in speed_list:
        mask = (sp == s_target) & (t > 0)
        t_sub = t[mask]
        rpm_sub = rpm[mask]
        duty_sub = duty[mask]
        if len(rpm_sub) > 10:
            # Steady-state: last 50% of the step
            n_ss = max(5, int(len(rpm_sub) * 0.50))
            rpm_ss = rpm_sub[-n_ss:]
            duty_ss = duty_sub[-n_ss:]
            mean_rpm = float(np.mean(rpm_ss))
            std_rpm = float(np.std(rpm_ss))
            mean_duty = float(np.mean(duty_ss))
            err_rpm = abs(mean_rpm - s_target)
            err_pct = (err_rpm / s_target) * 100.0 if s_target > 0 else 0.0
            peak_rpm = float(np.max(rpm_sub))
            os_pct = max(0.0, ((peak_rpm - s_target) / s_target) * 100.0) if s_target > 0 else 0.0
            status = "STABLE" if std_rpm < (0.08 * s_target) and err_pct < 5.0 else ("HUNTING/OSC" if std_rpm > (0.15 * s_target) else "ACCEPTABLE")
            step_stats.append({
                'target': s_target,
                'mean_rpm': mean_rpm,
                'std_rpm': std_rpm,
                'err_rpm': err_rpm,
                'err_pct': err_pct,
                'overshoot_pct': os_pct,
                'mean_duty': mean_duty,
                'status': status
            })

    # Print Summary Table
    print("\n[4/4] ==============================================================")
    print("                 MULTI-SPEED PERFORMANCE SUMMARY                    ")
    print("====================================================================")
    header = f"{'Target (RPM)':<13} | {'Measured (RPM)':<15} | {'Error (RPM)':<12} | {'Overshoot':<10} | {'Duty (%)':<9} | {'Status':<12}"
    print(header)
    print("-" * len(header))
    for st in step_stats:
        meas_str = f"{st['mean_rpm']:.1f} ± {st['std_rpm']:.1f}"
        print(f"{st['target']:<13.1f} | {meas_str:<15} | {st['err_rpm']:<5.1f} ({st['err_pct']:>4.1f}%) | {st['overshoot_pct']:>8.1f}% | {st['mean_duty']:>7.1f}% | {st['status']:<12}")
    print("====================================================================\n")

    # Generate Publication-Quality Plot
    os.makedirs(os.path.dirname(os.path.abspath(args.output_png)), exist_ok=True)
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True, gridspec_kw={'height_ratios': [2.2, 1.2]})

    # Top: RPM Tracking
    axes[0].plot(t, sp, 'k--', linewidth=1.5, label='Setpoint (RPM)', alpha=0.85)
    axes[0].plot(t, rpm, color='#1f77b4', linewidth=1.6, label='Motor Velocity (AS5600 [RPM])')
    axes[0].set_ylabel('Velocity (RPM)', fontsize=11, fontweight='bold')
    axes[0].set_title(f'Multi-Speed Closed-Loop Staircase Tracking (PI: Kp={args.kp:.6f}, Ki={args.ki:.6f})',
                      fontsize=12, fontweight='bold', pad=10)
    axes[0].grid(True, linestyle='--', alpha=0.6)
    axes[0].legend(loc='lower right', frameon=True, fontsize=10, facecolor='#ffffff', framealpha=0.9)
    axes[0].set_ylim(-50, max(np.max(rpm) * 1.15, max(speed_list) * 1.25))

    # Summary box on top right
    box_lines = ["Speed Envelope:"]
    for st in step_stats:
        box_lines.append(f"  {st['target']:>4.0f} RPM: {st['mean_rpm']:>4.0f} RPM (Duty: {st['mean_duty']:>4.1f}%) [{st['status']}]")
    axes[0].text(0.98, 0.95, "\n".join(box_lines), transform=axes[0].transAxes, fontsize=8.5,
                 verticalalignment='top', horizontalalignment='right',
                 bbox=dict(boxstyle='round,pad=0.5', facecolor='#f8f9fa', edgecolor='#cccccc', alpha=0.9))

    # Bottom: Duty Cycle (%)
    axes[1].plot(t, duty, color='#ff7f0e', linewidth=1.6, label='H-Bridge Duty Cycle (%)')
    axes[1].axhline(100.0, color='gray', linestyle=':', alpha=0.6)
    axes[1].axhline(0.0, color='gray', linestyle='-', alpha=0.5)
    axes[1].set_ylabel('Duty Cycle (%)', fontsize=11, fontweight='bold')
    axes[1].set_xlabel('Time (s)', fontsize=11, fontweight='bold')
    axes[1].set_ylim(-5, 105)
    axes[1].grid(True, linestyle='--', alpha=0.6)
    axes[1].legend(loc='upper right', frameon=True, fontsize=9.5, facecolor='#ffffff', framealpha=0.9)

    plt.tight_layout()
    plt.savefig(args.output_png, dpi=300)
    plt.close()
    print(f"[OK] Multi-speed validation figure saved to: {args.output_png}\n")

if __name__ == '__main__':
    main()

