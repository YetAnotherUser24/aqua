# OD-Logger: Aqualabo DIGISENS Optical Dissolved Oxygen Telemetry System

> **Repositorio:** [github.com/YetAnotherUser24/nodo-sensor-od](https://github.com/YetAnotherUser24/nodo-sensor-od)

Firmware for the **OD-Logger** telemetry system based on ESP32, dedicated to monitoring water parameters using the **Aqualabo DIGISENS Optical Dissolved Oxygen (OPTOD / OD)** sensor over RS-485 Modbus RTU.

Hardware platform:
- **LilyGO T3 LoRa32 (v1.6)**: ESP32-PICO-D4 + HAT SP3485EN for RS-485, MicroSD, DS3231 RTC, onboard SSD1306 OLED. Battery read via the internal ADC divider (`SENSOR_BATTERY_ADC`, GPIO 35).

> This is the only board currently supported (thesis bench). The Natek-SD / Natek-GPRS paths were removed.
>
> 🤖 **Working with AI Agents?** See [AGENTS.md](AGENTS.md) for the authoritative architecture, memory rules, and development guide.

---

## Main Features

- 💧 **Optical Dissolved Oxygen (OD) Sensing**
  - Aqualabo DIGISENS OPTOD sensor over Modbus RTU (RS-485 at 9600 baud, 8N1, Slave ID 10).
  - Explicit measurement order trigger (`0x001F` to holding register `0x0001`).
  - Measures 5 float parameters:
    - **Water Temperature**: `sensorReadings[0]` (centi-°C, scale x100)
    - **DO Saturation**: `sensorReadings[1]` (deci-%Sat, scale x10)
    - **Dissolved Oxygen (DO)**: `sensorReadings[2]` (milli-mg/L, scale x1000)
    - **Parameter 3**: `sensorReadings[3]` (centi, scale x100)
    - **Parameter 4**: `sensorReadings[4]` (centi, scale x100)
  - Continuous sampling optimization: Keeps sensor energized between samples to eliminate 1500 ms warmup latency, achieving up to **2-second continuous sampling**.

- 🌐 **Cloud Web Dashboard Control (`AquaControl`)**
  - Real-time HTTPS telemetry streaming to `aquacontrol-two.vercel.app` via `/api/telemetry/sensor`.
  - Bidirectional remote control: `start_experiment`, `stop_experiment`, `start_monitor`, `stop_monitor`, `set_sampling_rate`, `manual_sample`.
  - Detailed protocol guide in [docs/WEB_MASTER_PROTOCOL.md](docs/WEB_MASTER_PROTOCOL.md).

- 🔬 **Dedicated Experiment Logging on MicroSD**
  - Records continuous research experiments to RFC 4180 CSV files (`/EXP_01.CSV`, `/EXP_02.CSV`).
  - Flush-on-stop safety to prevent data corruption during unexpected disconnects.

- 📺 **OLED Live Visualization (SH1106 1.3")**
  - Real-time layout showing live DO (mg/L), Saturation (%Sat), Water Temperature (°C), Battery voltage, RTC time, and sampling interval.

- 💡 **Visual LED State Indicators**
  - **3 quick pulses** (100 ms ON / 100 ms OFF x3): Signals entry into **IDLE state** (ready and waiting for commands).
  - **2 pulses** (100 ms ON / 100 ms OFF x2): Signals transition into **Deep Sleep**.

- 🔋 **Battery Management**
  - Read through the internal ADC divider on GPIO 35 (`sensorReadings[5]`). ADC-only.

- 💾 **Resilient Offline Storage**
  - External I2C EEPROM (24LCxxx) ring buffer.
  - MicroSD backup logging daily records to `/YYYYMMDD.CSV`.
  - Store-and-forward telemetry synchronization to ThingsBoard via MQTT over 4G/GPRS.

---

## Build Targets (PlatformIO)

`default_envs = lora32`, so plain `pio run -t upload` already flashes the thesis board.

```bash
# 1. Placa de la tesis: LilyGO T3 LoRa32 (ESP32-PICO-D4) con HAT SP3485EN  [ENTORNO POR DEFECTO]
pio run -t upload
pio device monitor -b 115200
pio run -e lora32-ota -t upload        # por WiFi en od-logger-lora.local

# 2. Simulación de sensor (sin hardware RS-485)
pio run -e virtual -t upload

# 3. Diagnóstico: sonda del bus RS-485 (DE/RE, polaridad, baudios, esclavo)
pio run -e probe -t upload

# 6. Diagnóstico: escáner del bus I2C
pio run -e i2c-scan -t upload
```

> ¿Placa equivocada? Un entorno `natek-sd` sobre un **LilyGO T3 LoRa32** asigna el RS-485 a
> GPIO16/17 y cuelga el ESP32-PICO-D4 en cada medición. Confirma el chip en el log de arranque
> (`chip=ESP32-PICO-D4`) y consulta `src/board.h` antes de flashear.

---

## CSV File Formats

### 1. Dedicated Experiment File (`/EXP_XX.CSV`)
```csv
Timestamp,RTC_Sec,DO_mg_L,Sat_Pct,Temp_C,Batt_V
2026-09-12T05:14:00-05:00,842512440,7.850,98.50,25.00,3.95
```

### 2. Daily Continuous Offline Log (`/YYYYMMDD.CSV`)
```csv
datetime, seconds_since_2000, water_temp_centi, do_sat_deci_pct, do_milli_mg_l, param3_centi, param4_centi, battery_mv, rtc_temp_centi, status, sent
2026/09/02 19:40:00, 841779600, 2150, 985, 8456, 120, 0, 3840, 2300, 0, 1
```

---

## Documentation Links
* [AGENTS.md](AGENTS.md) — Comprehensive developer & AI agent instruction manual.
* [docs/WEB_MASTER_PROTOCOL.md](docs/WEB_MASTER_PROTOCOL.md) — AquaControl REST API & JSON wire protocol.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Modular software architecture and storage design.
* [docs/HARDWARE_SETUP.md](docs/HARDWARE_SETUP.md) — Wiring and board schematic setup.
