# AGENTS.md — Developer & AI Agent Guide for OD-Logger

Welcome to **OD-Logger**. This document is the authoritative specification and architectural guide for AI coding agents and developers working on this codebase.

---

## 1. Project Overview & Mission

**OD-Logger** is an autonomous, industrial-grade telemetry firmware written in C++ (Arduino/ESP-IDF framework under PlatformIO) targeting ESP32 microcontrollers.

Its primary purpose is real-time and periodic monitoring of water quality parameters using the **Aqualabo DIGISENS Optical Dissolved Oxygen (OPTOD)** sensor over RS-485 Modbus RTU, with dual operational modes:
1. **Cloud Web Dashboard (`AquaControl`)**: Real-time 24/7 telemetry streaming and bidirectional remote control via HTTPS REST over WiFi.
2. **Offline Field Logging**: Periodic sampling and direct daily CSV logging to MicroSD (`/YYYYMMDD.CSV`), with deep-sleep power cycling.

---

## 2. Hardware Platform

The codebase abstracts the board via [src/board.h](src/board.h). Only the **LilyGO T3 LoRa32**
(thesis bench) is supported; the Natek-SD / Natek-GPRS paths were removed, and the battery is
read through the internal ADC divider (`SENSOR_BATTERY_ADC`).

| Platform | Build Flag | Target MCU | Key Peripherals | Power / Sensor Pins |
| :--- | :--- | :--- | :--- | :--- |
| **LilyGO T3 LoRa32** | `-DBOARD_LILYGO_LORA32` | LilyGO T3 LoRa32 v1.6 | SX1278 LoRa, onboard 0.96" SSD1306 OLED (`0x3C`), DS3231 RTC, MicroSD, RS-485 | `LORA_CS=18`, `LORA_RST=23`, `LORA_DIO0=26`, `OD_PWR=25`, `LED_PIN=25` |

### Critical Electrical & Power Rail Rules
* **OLED Controller**: The onboard 0.96" SSD1306 requires 0-pixel column offset (`0x00`), charge pump enable (`0x8D 0x14`), and page addressing mode (`0x20 0x02`).
* **Dual SPI Controllers (LilyGO T3 LoRa32)**:
  - **VSPI (`SPI3`)**: Dedicated to LoRa SX1278 (`SCK=5`, `MISO=19`, `MOSI=27`, `CS=18`, `RST=23`, `DIO0=26`). Initialized via default `SPI` object or `loraSpiInit()`.
  - **HSPI (`SPI2`)**: Dedicated to MicroSD card slot (`SCK=14`, `MISO=2`, `MOSI=15`, `CS=13`). Driven independently by `sdSPI`.
  - Both buses have zero shared pins, preventing CS contention and allowing simultaneous transceiver and logging transactions.
* **Sensor Warmup Optimization**: The OPTOD sensor requires ~1500 ms to boot from a cold power-on. During continuous monitoring or experiments (`experimentActive || webMonitorActive`), the sensor rail is **kept energized** between samples, enabling up to **2-second continuous sampling rates**. Call `stopPrimarySensorRealtime()` upon stopping to cut power.

### Device bus (hardware abstraction)

All sensor hardware access goes through `src/sensor_bus.h` (`IOdSensor` + `IBatteryMonitor`,
chosen at link time). Real builds use `sensor_bus_real.cpp` (Modbus OPTOD + ADC); virtual builds
use `sensor_bus_virtual.cpp` bound to the declarative `sensor_sim.{h,cpp}` model. See
[VIRTUAL_NODES.md](../VIRTUAL_NODES.md).

---

## 3. PlatformIO Environments Matrix

`default_envs = lora32`, so plain `pio run` / `pio run -t upload` already target the thesis board.
Inspect [platformio.ini](platformio.ini) before running builds:

| Environment | Board | Primary Modules | Description |
| :--- | :--- | :--- | :--- |
| `lora32` | LilyGO T3 LoRa32 | WiFi + SD + ADC + OLED + Web Master | **Placa de la tesis y entorno por defecto.** ESP32-PICO-D4 con el HAT SP3485EN. |
| `lora32-ota` | LilyGO T3 LoRa32 | same as `lora32` | Igual, pero flasheado por WiFi en `od-logger-lora.local`. |
| `virtual` | LilyGO T3 LoRa32 | WiFi + Virtual Sensor + Web Master | Simulación sin sensor OD físico: el bus de sensores (`sensor_bus.h`) enlaza un modelo declarativo del OPTOD (`sensor_sim.cpp`) en lugar del Modbus real. La batería es **solo ADC**. Ver `VIRTUAL_NODES.md` en la raíz. |
| `probe` | LilyGO T3 LoRa32 | Standalone diagnostic | Barre el pin DE/RE del RS-485, su polaridad, los baudios y la dirección Modbus esclava para traer a punto el bus. |
| `i2c-scan` | LilyGO T3 LoRa32 | Standalone diagnostic | Escanea el bus I2C y anota los dispositivos conocidos (`0x3C`, `0x50`, `0x68`). |

> Los entornos de diagnóstico (`probe`, `i2c-scan`) sustituyen por completo las fuentes del firmware
> (`build_src_filter = -<*> +<...>`), así que **no** deben usarse para operar el nodo.

---

## 4. Operational Modes

The firmware operates in one of two fundamental architectures based on `#if defined(WEB_MASTER_MODE)`:

### Mode A: Web Master Mode (`WEB_MASTER_MODE`)
1. **Always Awake**: The ESP32 never enters deep sleep.
2. **IDLE State**:
   - Both `experimentActive` and `webMonitorActive` are `false`.
   - Sensor rails are powered off.
   - The device polls `GET /api/commands/pending` every **2000 ms**.
   - Signals entry into IDLE with a **3-pulse LED blink** (`ledSignalIdle()`).
3. **ACTIVE State (`experimentActive || webMonitorActive`)**:
   - Keeps sensor rails energized.
   - Samples sensors at the configured interval (`experimentIntervalMs` or `webMonitorIntervalMs`, default 2s or 5s).
   - If `experimentActive`, appends row to `/EXP_XX.CSV` on MicroSD in RFC 4180 format.
   - Updates SH1106 OLED live display.
   - Posts telemetry to `POST /api/telemetry/sensor`.
   - **Command Piggybacking**: Server responses to `POST /api/telemetry/sensor` carry any pending stop/control commands directly in the response body.
   - Transitions back to IDLE upon `stop_experiment` or `stop_monitor`.

### Mode B: Deep Sleep Cycling Mode (Field Production)
1. Wakes from deep sleep via DS3231 RTC interrupt or internal timer.
2. Samples OD sensor, RTC, and battery.
3. Appends reading directly to daily CSV `/YYYYMMDD.CSV` on MicroSD.
4. If WiFi available, connects and sends pending CSV rows to AquaControl.
5. Isolates GPIO pins via `gpio_hold_en()` and enters deep sleep for `moduleCfg.measure_time` minutes.
6. Signals deep sleep transition with a **2-pulse LED blink**.

---

## 5. Web Dashboard REST API Protocol (`AquaControl`)

Configured in [src/config.h](src/config.h):
- Host: `aquacontrol-two.vercel.app`
- Authentication Header: `X-Device-Key: <AQUACONTROL_DEVICE_KEY>`

### 1. Telemetry Upload (`POST /api/telemetry/sensor` & `POST /api/telemetry/sensor_bulk`)
The logger streams data using a Dual-Path Data Architecture depending on the experiment state:

- **Realtime Mode (1 Hz):** Uploads standard single JSON object to `/api/telemetry/sensor`.
```json
{
  "datetime": "2026-09-12T05:14:00-05:00",
  "seconds_since_2000": 842512440,
  "water_temp_centi": 2500,
  "do_sat_deci_pct": 985,
  "do_milli_mg_l": 7850,
  "param3_centi": 120,
  "param4_centi": 0,
  "battery_mv": 3950,
  "rtc_temp_centi": 2450,
  "status": 0,
  "sent": true
}
```

- **Bulk Mode (0.2 Hz):** During `ACTIVE_EXPERIMENT`, the logger accumulates samples and uploads a JSON array (`[ { ... }, { ... } ]`) to `/api/telemetry/sensor_bulk` every 5 seconds to bypass Vercel edge latency.

Server Response with Piggybacked Command:
```json
{
  "success": true,
  "has_command": true,
  "pending_command": {
    "id": "2708b792-7f28-44dc-9d41-a1b7e408ecbb",
    "command_type": "stop",
    "payload": {
      "action": "stop_experiment",
      "experiment_id": "exp_1726117823901"
    }
  }
}
```

### 2. Idle Command Polling (`GET /api/commands/pending`)
Queried every 2000 ms during IDLE state. Returns the same payload structure as above.

### 3. Command Acknowledgment (`POST /api/commands/[id]/acknowledge`)
Request JSON:
```json
{
  "success": true,
  "sensor_state": {
    "monitor_active": false,
    "active_experiment": null
  }
}
```

### 4. Supported Control Actions
* `start_experiment`: Starts logging to dedicated SD CSV (`/EXP_XX.CSV`), activates continuous monitor at `interval_sec`.
* `stop_experiment`: Flushes and closes SD experiment file, turns off sensor power rail, enters IDLE state.
* `start_monitor` / `start`: Activates OLED & live telemetry stream without SD experiment file.
* `stop_monitor` / `stop`: Stops live telemetry stream, turns off sensor power rail, enters IDLE state.
* `set_sampling_rate`: Updates `experimentIntervalMs` / `webMonitorIntervalMs` (clamped between 1 and 60 seconds).
* `manual_sample`: Queues an immediate one-shot sample and telemetry upload.
* `sleep`: Puts device to sleep for `minutes` (or indefinite).

---

## 6. Coding Standards & Memory Rules (Ponytail Senior Dev Mode)

1. **Static Memory Control — NO Dynamic Heap Allocation**:
   - ArduinoJson 7 is installed in `lib/ArduinoJson`.
   - **Never** use unbounded dynamic `JsonDocument` in message handlers, as repeated `malloc`/`free` cycles fragment the ESP32 heap during 24/7 streaming.
   - Always use `StaticJsonDocument<N>` with explicit, verified capacities:
     - `StaticJsonDocument<1536> doc;` in `processTelemetryResponse()` (**mandatory**: do not reduce below 1536 or incoming server responses with UUIDs and metadata will fail with `NoMemory`).
     - `StaticJsonDocument<384> doc;` in `publishToAquaControl()`.
     - `StaticJsonDocument<384> pDoc;` in `onSensorCommandReceived()`.
     - `StaticJsonDocument<512> ackDoc;` in `acknowledgeSensorCommand()`.
     - `StaticJsonDocument<256> stateDoc;` for state JSON objects.
2. **Safe Logging Macros**:
   - Use `Log(...)`, `Logln(...)`, and `Logf(...)` from [src/log.h](src/log.h).
   - Never call `Serial.print()` directly in feature modules.
3. **SD Card FAT 8.3 Filenames**:
   - MicroSD driver uses `SdFat`. Always format filenames as `/XXXXXXXX.CSV` (8 chars max, uppercase, e.g. `/EXP_01.CSV`).
4. **Visual LED Signaling**:
   - `ledSignalIdle()`: 3 pulses (100 ms ON, 100 ms OFF) to indicate entry into IDLE.
   - Deep sleep transition: 2 pulses (100 ms ON, 100 ms OFF).

---

## 7. Build & Verification Commands

```powershell
# 1. Compile the default environment (lora32 = the thesis board)
pio run

# 2. Flash the connected board and open the serial monitor
pio run -t upload
pio device monitor -b 115200

# 3. Compile and run the protocol & experiment self-check (host, no hardware)
g++ -std=c++17 test/test_experiment_flow.cpp -o test/test_experiment_flow.exe
./test/test_experiment_flow.exe
Remove-Item -Force test/test_experiment_flow.exe
```
