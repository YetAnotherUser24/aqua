#!/usr/bin/env python3
"""
calibrate_flow.py
Interactive Gravimetric Calibration Script for Peristaltic Pump.
Establishes the Flow Rate (Q in mL/min) vs Angular Velocity (RPM) relationship
and computes the volumetric constant (mL/rev) for Sodium Sulfite dosing.

Hydraulic context:
- 70 cm suction tubing + 70 cm discharge tubing (~1.4 m total).
- Internal volume of tubing (~5-10 mL) must be fully primed before running.
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

def query_pump_revolutions(ser):
    """Sends STATUS command and extracts total revolutions and pulses from telemetry."""
    ser.write(b"STATUS\n")
    time.sleep(0.15)
    total_revs = 0.0
    while ser.in_waiting > 0:
        line = ser.readline().decode('ascii', errors='ignore').strip()
        if "Total Revs:" in line:
            parts = line.split()
            try:
                for i, p in enumerate(parts):
                    if p == "Revs:":
                        total_revs = float(parts[i + 1])
                        break
            except Exception:
                pass
    return total_revs

def main():
    parser = argparse.ArgumentParser(description="Peristaltic Pump Flow/Velocity Calibration")
    parser.add_argument('--port', type=str, help='Serial/COM port (e.g., /dev/ttyACM0 or COM3)')
    parser.add_argument('--rpms', type=str, default='25,50,75,100,120', help='Comma-separated RPM test points')
    parser.add_argument('--duration', type=float, default=60.0, help='Duration of each run in seconds (default 60s)')
    parser.add_argument('--density', type=float, default=1.00, help='Solution density in g/mL (water=1.00, 10%% Na2SO3 ~1.08)')
    parser.add_argument('--output', type=str, default='scripts/data/flow_calibration.csv', help='Output CSV path')
    args = parser.parse_args()

    port = args.port or find_com_port()
    if not port:
        print("[ERROR] Serial port not found. Specify with --port.")
        sys.exit(1)

    rpm_list = [float(x.strip()) for x in args.rpms.split(',') if x.strip()]

    print("==================================================================")
    print("   AQUACONTROL: CALIBRACIÓN GRAVIMÉTRICA CAUDAL / VELOCIDAD       ")
    print("==================================================================")
    print(f"Puerto:            {port}")
    print(f"Puntos RPM:        {rpm_list}")
    print(f"Duración por test: {args.duration} s")
    print(f"Densidad solución: {args.density:.4f} g/mL")
    print("------------------------------------------------------------------")
    print("IMPORTANTE (Mangueras de 70 cm entrada + 70 cm salida):")
    print("1. Antes de iniciar, asegúrate de haber PURGADO COMPLETAMENTE las líneas.")
    print("   No debe haber burbujas de aire en los 140 cm de tubería.")
    print("2. Ten lista una balanza analítica o de precisión con tara en 0.00 g.")
    print("3. La boquilla de salida debe verter sin tocar el líquido del vaso recolector.")
    print("------------------------------------------------------------------\n")

    input("Presiona [ENTER] cuando la bomba esté cebada y lista para comenzar...")

    try:
        ser = serial.Serial(port=port, baudrate=115200, timeout=0.1)
        ser.setDTR(False)
        ser.setRTS(False)
    except Exception as e:
        print(f"[ERROR] No se pudo abrir {port}: {e}")
        sys.exit(1)

    time.sleep(1.5)
    ser.write(b"STOP\n")
    time.sleep(0.2)

    results = []

    for idx, target_rpm in enumerate(rpm_list, start=1):
        print(f"\n--------------------------------------------------------------")
        print(f" ENSAYO {idx}/{len(rpm_list)}: VELOCIDAD OBJETIVO = {target_rpm:.1f} RPM")
        print(f"--------------------------------------------------------------")
        input("Coloca el vaso colector vacío en la balanza, taralo a 0.00g y presiona [ENTER]...")

        rev_start = query_pump_revolutions(ser)

        print(f"Iniciando bomba a {target_rpm:.1f} RPM durante {args.duration:.1f} s...")
        ser.write(f"RPM {target_rpm}\n".encode('ascii'))
        start_t = time.time()

        while time.time() - start_t < args.duration:
            elapsed = time.time() - start_t
            remain = args.duration - elapsed
            line = ser.readline().decode('ascii', errors='ignore').strip()
            print(f"\rProgreso: {elapsed:4.1f}s / {args.duration:4.1f}s (Restan: {remain:4.1f}s) | {line[:60]}", end="")
            time.sleep(0.05)

        print("\nTiempo cumplido! Deteniendo bomba...")
        ser.write(b"STOP\n")
        time.sleep(0.5)

        rev_end = query_pump_revolutions(ser)
        revs_delivered = rev_end - rev_start
        if revs_delivered <= 0.0:
            # Fallback estimation if revolutions query was unparsed
            revs_delivered = (target_rpm / 60.0) * args.duration

        print(f"[INFO] Revoluciones medidas por AS5600: {revs_delivered:.2f} rev")

        while True:
            try:
                raw_mass = input(f"Ingresa la masa líquida pesada en la balanza (gramos): ")
                mass_g = float(raw_mass.strip())
                if mass_g < 0:
                    print("Por favor ingresa un valor positivo.")
                    continue
                break
            except ValueError:
                print("Valor numérico inválido. Intenta de nuevo.")

        volume_ml = mass_g / args.density
        flow_ml_min = (volume_ml / args.duration) * 60.0
        ml_per_rev = volume_ml / revs_delivered if revs_delivered > 0 else 0.0

        print(f"-> Volumen entregado: {volume_ml:.3f} mL")
        print(f"-> Caudal obtenido:   {flow_ml_min:.3f} mL/min")
        print(f"-> Constante puntual: {ml_per_rev:.4f} mL/rev")

        results.append({
            'Target_RPM': target_rpm,
            'Duration_s': args.duration,
            'Revolutions': revs_delivered,
            'Mass_g': mass_g,
            'Volume_mL': volume_ml,
            'Flow_mL_min': flow_ml_min,
            'mL_per_rev': ml_per_rev
        })

    ser.close()

    # Regression and Summary
    print("\n==============================================================")
    print("            RESUMEN DE CALIBRACIÓN CAUDAL vs RPM              ")
    print("==============================================================")
    print(" RPM    | Caudal (mL/min) | Masa (g) | Vol (mL) | Const (mL/rev)")
    print("--------+-----------------+----------+----------+---------------")
    total_vol = 0.0
    total_rev = 0.0
    for r in results:
        print(f" {r['Target_RPM']:5.1f}  | {r['Flow_mL_min']:13.3f}   | {r['Mass_g']:7.2f}  | {r['Volume_mL']:7.2f}  | {r['mL_per_rev']:10.4f}")
        total_vol += r['Volume_mL']
        total_rev += r['Revolutions']

    avg_ml_per_rev = total_vol / total_rev if total_rev > 0 else 1.0

    # Linear regression Q = a * RPM + b
    n = len(results)
    x = [r['Target_RPM'] for r in results]
    y = [r['Flow_mL_min'] for r in results]
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(x[i] * y[i] for i in range(n))
    sum_xx = sum(x[i]**2 for i in range(n))

    slope = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x**2) if (n * sum_xx - sum_x**2) != 0 else avg_ml_per_rev
    intercept = (sum_y - slope * sum_x) / n

    print("--------------------------------------------------------------")
    print(f"Ecuación ajustada:  Q (mL/min) = {slope:.4f} * RPM + ({intercept:.4f})")
    print(f"Constante volumétrica promedio: ML_PER_REV = {avg_ml_per_rev:.4f} mL/rev")
    print(f"\nActualiza en config.h:")
    print(f"  #define ML_PER_REV  {avg_ml_per_rev:.4f}f")
    print("==============================================================\n")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    print(f"[OK] Datos guardados en {args.output}")

if __name__ == '__main__':
    main()

