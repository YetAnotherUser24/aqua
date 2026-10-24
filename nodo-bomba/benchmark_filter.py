#!/usr/bin/env python3
"""
tune_filter_benchmark.py
Automated characterization and optimization of AS5600 + ESP32-S3 velocity estimation
at high motor RPM (up to 3800 RPM).
"""

import sys
import time
import serial
import numpy as np

PORT = "/dev/ttyACM0"
BAUD = 115200

def run_benchmark():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1.0)
    except Exception as e:
        print(f"Error opening serial port {PORT}: {e}")
        return

    time.sleep(0.5)
    ser.reset_input_buffer()

    print("=== AS5600 VELOCITY FILTER OPTIMIZATION BENCHMARK ===")
    print("Testing combinations of Hardware DSP (CONF) and Software EMA Alpha...")

    # Enable SYSID 50 Hz streaming
    ser.write(b"SYSID ON\n")
    time.sleep(0.3)

    configs = [
        {"name": "FAST_0x07C1 (SF=2x, FTH=10LSB, HYST=1LSB)", "filter_cmd": "FAST", "alphas": [0.06, 0.10, 0.15, 0.20, 0.25]},
        {"name": "BALANCED_0x0481 (SF=4x, FTH=6LSB, HYST=1LSB)", "filter_cmd": "BALANCED", "alphas": [0.06, 0.10, 0.15, 0.20, 0.25]},
        {"name": "SMOOTH_0x0001 (SF=16x, HYST=1LSB)", "filter_cmd": "SMOOTH", "alphas": [0.06, 0.10, 0.15, 0.20, 0.25]},
    ]

    results = []

    for cfg in configs:
        hw_name = cfg["name"]
        f_cmd = cfg["filter_cmd"]
        # Apply base hardware preset
        ser.write(f"FILTER {f_cmd}\n".encode())
        time.sleep(0.3)

        for alpha in cfg["alphas"]:
            # Set specific alpha
            ser.write(f"FILTER {alpha:.2f}\n".encode())
            time.sleep(0.4) # Settling
            ser.reset_input_buffer()

            samples = []
            start_t = time.time()
            while len(samples) < 60 and (time.time() - start_t) < 2.5:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if line.startswith("SYSID,"):
                    parts = line.split(",")
                    if len(parts) >= 5:
                        try:
                            rpm = float(parts[4])
                            samples.append(rpm)
                        except ValueError:
                            pass

            if len(samples) >= 30:
                arr = np.array(samples)
                mean_rpm = np.mean(arr)
                std_rpm = np.std(arr)
                cv = (std_rpm / mean_rpm * 100.0) if mean_rpm > 1.0 else 0.0
                p2p = np.ptp(arr)
                # Settling time (3 time constants for 95% settling at fs=500 Hz):
                settling_ms = (3.0 / (alpha * 500.0)) * 1000.0

                score = (cv * 1.5) + (settling_ms * 0.1) # Combined penalty: jitter + delay

                results.append({
                    "hw": hw_name,
                    "f_cmd": f_cmd,
                    "alpha": alpha,
                    "mean_rpm": mean_rpm,
                    "std_rpm": std_rpm,
                    "cv_pct": cv,
                    "p2p_rpm": p2p,
                    "settling_ms": settling_ms,
                    "score": score
                })
                print(f"[{f_cmd:8s} | alpha={alpha:.2f}] Mean: {mean_rpm:6.1f} RPM | Std: {std_rpm:5.2f} RPM | CV: {cv:4.2f}% | P2P: {p2p:5.1f} RPM | Settling: {settling_ms:4.1f}ms")

    # Turn off SYSID
    ser.write(b"SYSID OFF\n")
    time.sleep(0.2)

    # Sort results by score (lowest penalty = best trade-off)
    results.sort(key=lambda r: r["score"])

    best = results[0]
    print("\n================== BENCHMARK WINNER ==================")
    print(f"Optimal Hardware DSP: {best['hw']}")
    print(f"Optimal Filter Alpha: {best['alpha']:.2f}")
    print(f"Achieved Mean Speed : {best['mean_rpm']:.1f} RPM")
    print(f"Standard Deviation  : {best['std_rpm']:.2f} RPM (Noise: {best['cv_pct']:.2f}%)")
    print(f"Peak-to-Peak Ripple : {best['p2p_rpm']:.1f} RPM")
    print(f"Step Settling Time  : {best['settling_ms']:.1f} ms")
    print("======================================================\n")

    # Apply winning configuration
    ser.write(f"FILTER {best['f_cmd']}\n".encode())
    time.sleep(0.2)
    ser.write(f"FILTER {best['alpha']:.2f}\n".encode())
    time.sleep(0.2)
    ser.write(b"STATUS\n")
    time.sleep(0.5)

    for _ in range(15):
        l = ser.readline().decode('utf-8', errors='ignore').strip()
        if l: print(l)

    ser.close()

if __name__ == "__main__":
    run_benchmark()

