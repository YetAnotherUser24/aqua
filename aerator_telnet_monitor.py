#!/usr/bin/env python3
"""
Monitor Telnet en vivo del Nodo Aireador (ESP32)
Conexión directa al socket TCP 23 del ESP32 en 192.168.4.10
Muestra los logs internos de FreeRTOS Core 1 (ODrive UART, bucle de control, E-Stop y fallas)
"""

import sys
import socket
import time

AERATOR_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.4.10"
TELNET_PORT = 23

print("=" * 70)
print(f"  CONSOLA TELNET REMOTA - ESP32 AIREADOR ({AERATOR_IP}:{TELNET_PORT})")
print("=" * 70)

while True:
    try:
        print(f"[CONECTANDO] Intentando conectar a {AERATOR_IP}:{TELNET_PORT}...")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.connect((AERATOR_IP, TELNET_PORT))
        s.settimeout(None)
        print(f"[CONECTADO] Conectado exitosamente. Recibiendo logs en tiempo real:\n")

        while True:
            data = s.recv(1024)
            if not data:
                print("\n[AVISO] Conexión cerrada por el ESP32.")
                break
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n[SALIR] Monitor terminado por el usuario.")
        break
    except Exception as e:
        print(f"[ERROR] Conexión fallida: {e}")
        print("[REINTENTO] Reintentando en 3 segundos (Ctrl+C para salir)...")
        time.sleep(3)

