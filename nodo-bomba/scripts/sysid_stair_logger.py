#!/usr/bin/env python3
"""
sysid_stair_logger.py
Data acquisition script for Peristaltic Pump System Identification.
Executes a Return-to-Zero Staircase sequence over Serial USB (115200 baud),
logging high-frequency telemetry into CSV.
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

def main():
    parser = argparse.ArgumentParser(description="Peristaltic Pump Stair-Step System ID Logger")
    parser.add_argument('--port', type=str, help='Serial/COM port (e.g., /dev/ttyACM0 or COM3). Auto-detects if omitted.')
    parser.add_argument('--step_time', type=float, default=4.0, help='Time in seconds to hold each step. Default: 4.0')
    parser.add_argument('--output', type=str, default='scripts/data/step_response.csv', help='Output CSV file name.')
    args = parser.parse_args()

    port = args.port or find_com_port()
    if not port:
        print("[ERROR] Could not automatically find a Serial port. Please specify --port.")
        sys.exit(1)

    print(f"Connecting to {port} at 115200 baud...")
    try:
        ser = serial.Serial(port=port, baudrate=115200, timeout=0.1)
        ser.setDTR(False)
        ser.setRTS(False)
    except Exception as e:
        print(f"[ERROR] Failed to open port {port}: {e}")
        print("Ensure the Serial Monitor in PlatformIO / Arduino is closed.")
        sys.exit(1)

    time.sleep(1.5)  # Let ESP32 finish USB CDC handshake

    print("Stopping pump to establish baseline zero-state...")
    ser.write(b"STOP\n")
    time.sleep(0.5)

    print("Activating SYSID Mode (50Hz CSV stream)...")
    ser.write(b"SYSID ON\n")
    time.sleep(0.2)
    ser.reset_input_buffer()

    # Return-to-zero staircase sequence for peristaltic pump:
    # Isolates static friction (stiction of pinching tube) and mechanical time constant.
    sequence = [0.25, 0.0, 0.45, 0.0, 0.65, 0.0, 0.85, 0.0, 1.0, 0.0]
    total_duration = len(sequence) * args.step_time

    print(f"Sequence: {sequence}")
    print(f"Running staircase for {total_duration:.1f} seconds...")

    start_time = time.time()
    last_step_time = start_time + 0.5  # 0.5s baseline
    current_step_idx = 0
    data_points = []

    try:
        while time.time() - start_time < (total_duration + 0.5):
            now = time.time()

            if now >= last_step_time and current_step_idx < len(sequence):
                target_duty = sequence[current_step_idx]
                cmd = f"DUTY {target_duty}\n".encode('ascii')
                ser.write(cmd)
                print(f"[{now - start_time:5.1f}s] Applied DUTY {target_duty:.2f}")
                last_step_time = now + args.step_time
                current_step_idx += 1

            line = ser.readline().decode('ascii', errors='ignore').strip()
            if line.startswith("SYSID,"):
                parts = line.split(',')
                # Format: SYSID,Time_ms,Target_Duty,Duty_Pct,RPM,Rad_s,Dosed_mL
                if len(parts) >= 6:
                    data_points.append({
                        'Time_ms': float(parts[1]),
                        'Target_Duty': float(parts[2]),
                        'Duty_Pct': float(parts[3]),
                        'RPM': float(parts[4]),
                        'Rad_s': float(parts[5]),
                        'Dosed_mL': float(parts[6]) if len(parts) > 6 else 0.0
                    })
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    print("Sequence complete. Stopping motor...")
    ser.write(b"STOP\n")
    time.sleep(0.1)
    ser.write(b"SYSID OFF\n")
    time.sleep(0.1)
    ser.close()

    if not data_points:
        print("[ERROR] No SYSID points received. Check wiring and board connection.")
        sys.exit(1)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    t0_ms = data_points[0]['Time_ms']

    with open(args.output, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Time_s', 'Target_Duty', 'Duty_Pct', 'RPM', 'Rad_s', 'Dosed_mL'])
        for dp in data_points:
            t_s = (dp['Time_ms'] - t0_ms) / 1000.0
            writer.writerow([
                f"{t_s:.4f}",
                f"{dp['Target_Duty']:.4f}",
                f"{dp['Duty_Pct']:.2f}",
                f"{dp['RPM']:.2f}",
                f"{dp['Rad_s']:.4f}",
                f"{dp['Dosed_mL']:.3f}"
            ])

    print(f"[OK] Successfully saved {len(data_points)} samples to {args.output}")

if __name__ == '__main__':
    main()
