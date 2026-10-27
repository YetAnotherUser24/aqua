#!/usr/bin/env python3
"""
test_gains.py
Peristaltic Pump Closed-Loop PI Gain Validation Runner.
Applies Kp, Ki, Kd gains on the fly over USB CDC using 'TUNE',
triggers a closed-loop step to target RPM, logs the dynamic response,
and saves CSV for validation.
"""

import serial
import serial.tools.list_ports
import time
import csv
import argparse
import sys
import os

def find_com_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        desc = (port.description or "").lower()
        if "usb" in desc or "serial" in desc or "ch340" in desc or "cp210" in desc:
            return port.device
    if len(ports) > 0:
        return ports[0].device
    return None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    parser = argparse.ArgumentParser(description="Peristaltic Pump PID Gain Tester")
    parser.add_argument('--port', type=str, help='Serial/COM port. Auto-detects if omitted.')
    parser.add_argument('--kp', type=float, required=True, help='Proportional Gain (Kp)')
    parser.add_argument('--ki', type=float, required=True, help='Integral Gain (Ki)')
    parser.add_argument('--kd', type=float, default=0.0, help='Derivative Gain (Kd, default 0.0)')
    parser.add_argument('--rpm', type=float, default=60.0, help='Target RPM for step response. Default: 60')
    parser.add_argument('--duration', type=float, default=8.0, help='Duration to record (s). Default: 8.0')
    parser.add_argument('--output', type=str, default=os.path.join(SCRIPT_DIR, 'data', 'closed_loop_test.csv'), help='Output CSV file')
    args = parser.parse_args()

    port = args.port or find_com_port()
    if not port:
        print("[ERROR] Could not find a Serial port.")
        sys.exit(1)

    print(f"Connecting to {port} at 115200 baud...")
    try:
        ser = serial.Serial(port=port, baudrate=115200, timeout=0.1)
        ser.setDTR(False)
        ser.setRTS(False)
    except Exception as e:
        print(f"[ERROR] Failed to open {port}: {e}")
        sys.exit(1)

    time.sleep(1.5)
    ser.write(b"STOP\n")
    time.sleep(0.3)

    print(f"Uploading Gains: Kp={args.kp:.6f}, Ki={args.ki:.6f}, Kd={args.kd:.6f}")
    ser.write(f"TUNE {args.kp} {args.ki} {args.kd}\n".encode('ascii'))
    time.sleep(0.3)

    print("Activating SYSID Mode...")
    ser.write(b"SYSID ON\n")
    time.sleep(0.1)
    ser.reset_input_buffer()

    print(f"Holding baseline for 0.5s, then applying Step to {args.rpm:.1f} RPM for {args.duration:.1f} s...")

    start_time = time.time()
    step_applied = False
    data_points = []
    current_setpoint_rpm = 0.0

    try:
        while time.time() - start_time < (args.duration + 0.5):
            now = time.time()
            if not step_applied and (now - start_time >= 0.5):
                ser.write(f"RPM {args.rpm}\n".encode('ascii'))
                current_setpoint_rpm = args.rpm
                step_applied = True
                print(f"[{now - start_time:.2f}s] STEP APPLIED: Target RPM = {args.rpm:.1f}")

            line = ser.readline().decode('ascii', errors='ignore').strip()
            if line.startswith("SYSID,"):
                parts = line.split(',')
                # Format: SYSID,Time_ms,Target_Duty,Duty_Pct,RPM,Rad_s,Dosed_mL
                if len(parts) >= 6:
                    data_points.append({
                        'Time_ms': float(parts[1]),
                        'Setpoint_RPM': current_setpoint_rpm,
                        'Duty_Pct': float(parts[3]),
                        'RPM': float(parts[4]),
                        'Rad_s': float(parts[5]),
                        'Dosed_mL': float(parts[6]) if len(parts) > 6 else 0.0
                    })
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    print("Test complete. Stopping motor...")
    ser.write(b"STOP\n")
    time.sleep(0.1)
    ser.write(b"SYSID OFF\n")
    time.sleep(0.1)
    ser.close()

    if not data_points:
        print("[ERROR] No telemetry received.")
        sys.exit(1)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    t0_ms = data_points[0]['Time_ms']

    with open(args.output, mode='w', newline='') as f:
        fieldnames = ['Time_s', 'Setpoint_RPM', 'Duty_Pct', 'RPM', 'Rad_s', 'Dosed_mL']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for dp in data_points:
            t_s = (dp['Time_ms'] - t0_ms) / 1000.0
            writer.writerow({
                'Time_s': f"{t_s:.4f}",
                'Setpoint_RPM': f"{dp['Setpoint_RPM']:.1f}",
                'Duty_Pct': f"{dp['Duty_Pct']:.2f}",
                'RPM': f"{dp['RPM']:.2f}",
                'Rad_s': f"{dp['Rad_s']:.4f}",
                'Dosed_mL': f"{dp['Dosed_mL']:.3f}"
            })

    print(f"[OK] Closed-loop validation data saved to {args.output}")

    # Save run metadata sidecar with controller parameters
    import json
    meta_path = os.path.splitext(args.output)[0] + "_meta.json"
    run_meta = {
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S'),
        "kp": args.kp,
        "ki": args.ki,
        "kd": args.kd,
        "target_rpm": args.rpm,
        "duration_s": args.duration,
        "samples": len(data_points),
        "csv_file": os.path.basename(args.output)
    }
    with open(meta_path, 'w') as mf:
        json.dump(run_meta, mf, indent=2)
    print(f"[OK] Controller parameters saved to {meta_path}")

    # Append to run history ledger
    history_file = os.path.join(os.path.dirname(args.output), '..', 'results', 'run_history.csv')
    history_file = os.path.abspath(history_file)
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    file_exists = os.path.exists(history_file)
    with open(history_file, 'a', newline='') as hf:
        hwriter = csv.writer(hf)
        if not file_exists:
            hwriter.writerow(['Timestamp', 'Run_Type', 'Target_RPM', 'Kp', 'Ki', 'Kd', 'Duration_s', 'Samples', 'CSV_File'])
        run_type = os.path.splitext(os.path.basename(args.output))[0]
        hwriter.writerow([
            run_meta['timestamp'],
            run_type,
            f"{args.rpm:.1f}",
            f"{args.kp:.6f}",
            f"{args.ki:.6f}",
            f"{args.kd:.6f}",
            f"{args.duration:.1f}",
            len(data_points),
            args.output
        ])
    print(f"[OK] Appended run entry to {history_file}")

if __name__ == '__main__':
    main()
