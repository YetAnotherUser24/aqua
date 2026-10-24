#!/usr/bin/env python3
"""
Diagnóstico en tiempo real del Nodo Aireador (ESP32 + ODrive S1)
AquaStation SCADA

Este script escucha la telemetría MQTT y decodifica automáticamente:
1. Códigos de error de ODrive S1 (active_errors bitmask)
2. Estado de armado / desarmado / E-Stop
3. Modo de control (PID, Fuzzy, Manual)
4. Tensión de bus (Vbus) y corriente (Ibus)
5. Eventos emitidos en aqua/events/aerator
"""

import sys
import json
import time
import argparse
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Instalando paho-mqtt...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paho-mqtt"])
    import paho.mqtt.client as mqtt

# Mapeo de bits de error de ODrive S1 (ODrive active_errors)
ODRIVE_ERRORS = {
    0x00000001: "INITIALIZING",
    0x00000002: "SYSTEM_LEVEL",
    0x00000004: "TIMING_ERROR",
    0x00000008: "MISSING_ESTIMATE",
    0x00000010: "BAD_CONFIG",
    0x00000020: "DRV_FAULT (Fallo en puente inversor MOSFETs / DRV8316)",
    0x00000040: "MISSING_INPUT",
    0x00000080: "DC_BUS_OVER_VOLTAGE (Sobretensión en bus DC)",
    0x00000100: "DC_BUS_UNDER_VOLTAGE (Tensión insuficiente en bus DC < umbral arm)",
    0x00000200: "DC_BUS_OVER_CURRENT (Sobrecorriente en bus DC)",
    0x00000400: "DC_BUS_OVER_REGEN_CURRENT (Sobrecorriente de frenado)",
    0x00000800: "CURRENT_LIMIT_VIOLATION (Límite de corriente de fase excedido)",
    0x00001000: "MOTOR_OVER_TEMP (Sobretemperatura devanados motor)",
    0x00002000: "INVERTER_OVER_TEMP (Sobretemperatura MOSFETs ODrive)",
    0x00004000: "VELOCITY_LIMIT_VIOLATION (Sobrevelocidad > límite físico)",
    0x00008000: "POSITION_LIMIT_VIOLATION (Límite de posición)",
    0x00010000: "WATCHDOG_TIMER_EXPIRED (Watchdog de comunicación UART expiró)",
    0x00020000: "ESTOP_REQUESTED (Parada de emergencia / E-Stop activa)",
    0x00040000: "SPINOUT_DETECTED (Deslizamiento / motor trabado o atascado)",
    0x00080000: "BRAKE_RESISTOR_DISARMED",
    0x00100000: "THERMISTOR_DISCONNECTED (Sensor térmico desconectado)",
    0x00200000: "CALIBRATION_ERROR (Error de calibración FOC)",
}

ODRIVE_AXIS_STATES = {
    0: "UNDEFINED",
    1: "IDLE (En Reposo)",
    2: "STARTUP",
    3: "FULL_CALIBRATION",
    4: "MOTOR_CALIBRATION",
    6: "ENCODER_INDEX_SEARCH",
    7: "ENCODER_OFFSET_CALIBRATION",
    8: "CLOSED_LOOP (Armado)",
    9: "LOCKIN_SPIN",
    10: "ENCODER_DIR_FIND",
    11: "HOMING",
}

def decode_odrive_error(code: int) -> str:
    if code == 0:
        return "OK (Sin errores)"
    active = []
    for mask, desc in ODRIVE_ERRORS.items():
        if code & mask:
            active.append(f"0x{mask:08X}: {desc}")
    if not active:
        return f"0x{code:08X}: ERROR DESCONOCIDO"
    return " | ".join(active)

parser = argparse.ArgumentParser(description="Diagnóstico MQTT del Nodo Aireador")
parser.add_argument("host", nargs="?", default="192.168.4.1", help="IP del broker MQTT (default: 192.168.4.1)")
parser.add_argument("--port", "-p", type=int, default=1883, help="Puerto MQTT (default: 1883)")
parser.add_argument("--user", "-u", default="aerator", help="Usuario MQTT (default: aerator)")
parser.add_argument("--password", "-P", default="cNTSrH2JC7gI3pdGdUEwIZ8r", help="Contraseña MQTT (default: clave aerator)")
args = parser.parse_args()

print("=" * 72)
print("  DIAGNÓSTICO EN VIVO - NODO AIREADOR (ESP32 + ODrive S1)")
print(f"  Broker: {args.host}:{args.port}  |  Usuario: '{args.user}'")
print("=" * 72)

def on_connect(*cb_args, **cb_kwargs):
    # Compatible con paho-mqtt v1 (4 args) y v2 (5 args)
    if len(cb_args) >= 4:
        rc = cb_args[3]
    else:
        rc = cb_args[-1]
    
    rc_val = rc.value if hasattr(rc, "value") else rc
    client = cb_args[0]
    
    if rc_val == 0:
        print("[MQTT] Autenticado y conectado exitosamente.")
        client.subscribe("aqua/tlm/aerator")
        client.subscribe("aqua/events/aerator")
        client.subscribe("aqua/presence/#")
        print("[MQTT] Escuchando telemetría, eventos y presencia...")
        print("-" * 72)
    elif rc_val == 5:
        print(f"[MQTT] Error 5: Credenciales no autorizadas (usuario: '{args.user}').")
        print("       Prueba especificando las credenciales de la Pi:")
        print("       python3 diagnose_aerator.py 192.168.4.1 -u gateway -P <clave_gateway>")
    else:
        print(f"[MQTT] Error de conexión: código {rc_val}")

def on_message(client, userdata, msg):
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    topic = msg.topic
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[{now}] [{topic}] Error al decodificar JSON: {e}")
        return

    if topic == "aqua/tlm/aerator":
        samples = data.get("samples", [])
        if not samples:
            return
        
        # Analizar la muestra más reciente del lote
        s = samples[-1]
        rpm = s.get("drv_rpm", 0.0)
        target = s.get("drv_rpm_target", 0.0)
        vbus = s.get("drv_vbus_v", 0.0)
        ibus = s.get("drv_ibus_a", 0.0)
        power = s.get("drv_power_w", 0.0)
        running = s.get("drv_running", False)
        status_code = s.get("drv_status", 0)
        drv_state = s.get("drv_state", None)
        disarm_reason = s.get("drv_disarm_reason", 0)
        mode = s.get("ctl_mode", "unknown")
        failsafe = s.get("ctl_failsafe", False)
        peer_do = s.get("peer_do_mg_l", 0.0)

        err_str = decode_odrive_error(status_code)
        disarm_str = decode_odrive_error(disarm_reason) if disarm_reason != 0 else None
        state_label = ODRIVE_AXIS_STATES.get(drv_state, f"STATE_{drv_state}") if drv_state is not None else ("ARMED" if running else "IDLE")
        status_tag = "🚨 ERROR" if status_code != 0 else ("🟢 ARMED" if running else "⚪ IDLE")

        print(f"[{now}] [TLM] {status_tag} ({state_label}) | Modo: {mode.upper()} | SP: {target:.0f} RPM | Act: {rpm:.1f} RPM | Bus: {vbus:.2f}V, {ibus:.2f}A ({power:.1f}W)")
        if status_code != 0:
            print(f"         🚨 FALLA ODRIVE: {err_str}")
        if disarm_str:
            print(f"         ⚠️  MOTIVO ÚLTIMO DESARME: {disarm_str}")
        if failsafe:
            print(f"         ⚠️  MODO FAILSAFE ACTIVO (sin telemetría reciente de OD)")

    elif topic == "aqua/events/aerator":
        event_type = data.get("event_type", "unknown")
        detail = data.get("detail", "")
        print(f"[{now}] 📢 [EVENTO AIREADOR] {event_type} -> {detail}")

    elif "presence" in topic:
        online = data.get("online", False)
        reason = data.get("reason", "")
        role = data.get("role", topic.split("/")[-1])
        print(f"[{now}] 📡 [PRESENCIA] {role}: {'ONLINE' if online else 'OFFLINE'} ({reason})")

# Compatibilidad paho-mqtt v1 y v2
if hasattr(mqtt, "CallbackAPIVersion"):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="diag_aerator_" + str(int(time.time())))
else:
    client = mqtt.Client(client_id="diag_aerator_" + str(int(time.time())))

if args.user:
    client.username_pw_set(args.user, args.password)

client.on_connect = on_connect
client.on_message = on_message

try:
    client.connect(args.host, args.port, 60)
    client.loop_forever()
except KeyboardInterrupt:
    print("\n[INFO] Monitor detenido por el usuario.")
except Exception as exc:
    print(f"\n[ERROR] No se pudo conectar a {args.host}:{args.port}: {exc}")
