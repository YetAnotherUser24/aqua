# Architecture & System Design

This document explains the complete architecture of the **n-logger multi-sensor platform** running on LilyGO A7670SA hardware, including module interactions, data flow, storage design, BLE commands, and MQTT synchronization.

---

# 1. High-Level System Overview

The device is designed for **autonomous, low‑power, offline‑resilient multi-sensor monitoring**.

It performs:

1. **Periodic measurement** (via compile-time selected sensors)
2. **Local storage to EEPROM**
3. **Batch flushing to SD (CSV)**
4. **MQTT sync to ThingsBoard**
5. **Remote configuration via BLE**
6. **Deep sleep for power saving**
7. **Solar charging**

The platform supports compile-time togglable features via PlatformIO build flags:
- **SD_MODULE** — SD card storage
- **GPRS_MODULE** — LTE modem / MQTT communication
- **BLE** — Always included (non-toggleable)
- **EEPROM** — Always included (non-toggleable)

Sensors are enabled via descriptive build flags (any combination):
- **SENSOR_MAXBOTIX_UART** — MaxBotix MB12xx ultrasonic
- **SENSOR_I2C_TEMPERATURE** — I2C temperature/humidity
- **SENSOR_RADAR** — Radar distance sensor
Each sensor defines its own pins, number of readings, and warmup time.

---

# 2. System Architecture Diagram
```mermaid
flowchart TD

    subgraph SENSOR[Sensor Engine]
        API[sensorReadAll()]
        S1[MaxBotix UART]
        S2[I2C Temp]
        S3[Radar]
    end

    subgraph STORAGE[Local Storage]
        EEPROM[EEPROM Buffer - binary pages]
        SD[SD Card CSV Files]
    end

    subgraph MCU[ESP32 Core]
        MAIN[main.cpp Entry Point]
        CFG[n_logger_config.h Central Types]
        MEAS[Measurement Engine]
        RTC[RTC DS3231]
        BAT[Battery Monitor]
        STATUS[Status Bits]
        BLE[BLE Manager]
        MODEM[Modem and MQTT Client]
        SLEEP[Power Manager]
    end

    subgraph CLOUD[Cloud Platform]
        TB[ThingsBoard MQTT Broker]
    end

    MAIN --> CFG
    MAIN --> MEAS
    API --> MEAS
    RTC --> MEAS
    BAT --> MEAS
    STATUS --> MEAS

    MEAS --> EEPROM
    EEPROM --> SD
    SD --> MODEM
    EEPROM --> MODEM

    BLE <---> MEAS
    MODEM --> TB
    SLEEP --> MEAS
```

---

# 3. Module Responsibilities

Below is the detailed breakdown of every `.cpp` / `.h` module in the project.

---

## 3.1 `n_logger_config.h` (NEW)

🔹 **Central type and constant definitions**

Replaces duplicated structs, status bits, EEPROM constants, and pin definitions previously scattered across 4+ files.

Contains:
- `ModuleConfig` — Runtime scheduling configuration
- `TelemetryPayload` — Telemetry payload with `sensorReadings[MAX_SENSOR_READINGS]`
- Status bit definitions (`STATUS_INTERNET_BIT`, `STATUS_SD_CARD_BIT`, etc.)
- EEPROM geometry constants (`EEPROM_PAGE_SIZE`, `EEPROM_ADDRESS`, etc.)
- Pin defaults (`LED_PIN`, `VBAT_PIN`, etc.)
- `changeBit()` utility (moved from .ino)
- Device identity (`N_LOGGER_DEVICE_NAME`, `CODE_VERSION`)
- Per-sensor configuration blocks (`#ifdef SENSOR_MAXBOTIX_UART`, etc.) with sensor-specific pins (`MAXBOTIX_RX_PIN`, `MAXBOTIX_PWR_PIN`) and `MAX_SENSOR_READINGS` auto-computed from all enabled sensors
- `SENSOR_NAME` string mapped from enabled sensor flags

All defines use `#ifndef` guards allowing per-build overrides via PlatformIO `build_flags`.
Multiple sensor flags can be combined — `MAX_SENSOR_READINGS` sums each sensor's reading count.

---

## 3.2 `sensor_engine.cpp` / `.h` (NEW)

🔹 **Compile-time sensor abstraction layer**

Provides a single-function sensor API using descriptive `#ifdef SENSOR_XXX` blocks. No virtual functions — all dispatch is resolved at compile time, saving flash.

Supported sensor flags (any combination):
- **SENSOR_MAXBOTIX_UART** — MaxBotix ultrasonic via UART (existing implementation)
- **SENSOR_I2C_TEMPERATURE** — I2C Temperature (placeholder)
- **SENSOR_RADAR** — Radar distance sensor (placeholder)

### Public API — 1 function
- `sensorReadAll(TelemetryPayload& tp)` — Full measurement cycle:
  For each enabled sensor: powers on → waits warmup → reads N samples → powers off → pads remaining slots with zero.

### Per-sensor design
Each sensor type lives in its own `#ifdef` block with:
- Own static `HardwareSerial` instance (if UART)
- Own `_ReadSingle()` function (e.g. `maxbotixReadSingle()`)
- Per-sensor pins from `n_logger_config.h` (`MAXBOTIX_RX_PIN`, `MAXBOTIX_PWR_PIN`, etc.)
- Power on/off/warmup are inline `digitalWrite()` + `delay()` — no separate functions
- I2C sensors share the bus already initialized for RTC/EEPROM — no power/warmup needed

---

## 3.3 `main.cpp` (NEW — replaces `n_level_gprs.ino`)

🔹 **Lightweight entry point composing all modules**

Replaces the monolithic 657-line `.ino` sketch. Responsible for:
- Defining all remaining globals (`device_ID`, `eepromWritePage`, `bootCount`, state flags, BLE objects, modem objects, SD objects, RTC objects)
- `setup()` — Initialization flow with feature guards
- `loop()` — BLE window management and deep sleep transitions
- `handleBleLedPattern()` — LED status indication
- `toFinalName()` — CSV filename conversion
- `extractJSON()` — HTTP response JSON extraction (preserved for compatibility)

Feature toggle handling:
- `#ifdef SD_MODULE` — SD card initialization
- `#ifdef GPRS_MODULE` — Modem/MQTT initialization
- Serial banner shows enabled modules and sensor type name

---

## 3.4 `measurement_storage.cpp` / `.h`

🔹 **Core of the data pipeline**

Handles:
- Sensor sampling (via `sensor_engine.h` API)
- Battery & temperature acquisition
- RTC timestamping
- EEPROM page creation
- SD flushing (guarded by `#ifdef SD_MODULE`)
- CSV → JSON conversion (renamed `waterLevelSensor` → `sensorReadings` in JSON)
- MQTT publishing (guarded by `#ifdef GPRS_MODULE`)

### Key functions
- `takeMeasurement()` — Full measurement cycle using `sensorReadAll()`
- `dumpEEPROM()` — Write EEPROM pages to SD
- `sendSDPendingToThingsboard()` — Upload pending CSV files
- `sendEEPROMToThingsboard()` — Upload unsent EEPROM pages
- `csvLineToJson()` — CSV to JSON with `sensorReadings` key
- `markLineSent()` — Mark CSV line as sent

### EEPROM page format (32 bytes)
```
0–3     = seconds since 2000
4–13    = sensor readings (MAX_SENSOR_READINGS × int16)
14–15   = battery (mV)
16–17   = temperature (°C x100)
18–21   = status flags (uint32)
31      = sent flag (0/1)
```

---

## 3.5 `eeprom_i2c.cpp` / `.h`

🔹 **Low‑level EEPROM driver (always included)**

Provides:
- Page writes (32 bytes)
- Byte read/write
- Buffer reads
- Header reset (bitmap of used pages)

The EEPROM acts as a **short-term ring buffer**, minimizing SD writes.

---

## 3.6 `ble_manager.cpp` / `.h`

🔹 **Bluetooth Low Energy configuration interface (always included)**

Implements:
- BLE GATT server
- Read/Write characteristic
- Command parser:
  - `T-<measure>-<ble>-<gprs>` — update timing configuration
  - `D-YYYY-MM-DD-HH-MM-SS` — set RTC date/time
  - `DATA` — placeholder
- BLE window timeout logic

BLE device name is configurable via `N_LOGGER_BLE_NAME` build flag.
BLE is **only on when needed** to save battery.

---

## 3.7 `modem_mqtt.cpp` / `.h`

🔹 **SIM7670 modem power management + MQTT client (guarded by `#ifdef GPRS_MODULE`)**

Responsible for:
- Initializing modem
- Configuring APN
- Connecting to mobile network
- Establishing MQTT session
- Publishing telemetry packets

Recovery behavior:
- Uses `failedTransmissionCount` to track consecutive GPRS/MQTT connection failures
- Uses `MAX_FAILED_TRANSMISSIONS = 3` as recovery threshold
- Sets `forceModemReset = true` when threshold is reached
- Performs full modem hard reset on the next GPRS window (`initA7670()` reset branch)
- Keeps telemetry data in EEPROM/SD while offline (no data discard)

MQTT client ID is configurable via `MQTT_CLIENT_ID` build flag (default: `"nLoggerClient"`).

When `GPRS_MODULE` is not defined, stub functions are provided that return `false` / no-op.

---

## 3.8 `rtc_utils.cpp` / `.h`

🔹 **RTC helper functions (always included)**

Main features:
- Fetch current timestamp
- Format timestamps (used in CSV)
- Provide seconds since 2000 (EEPROM format)
- Read RTC temperature

Also checks for invalid RTC state on boot.

---

## 3.9 `power_sleep.cpp` / `.h`

🔹 **Low‑power logic**

Manages:
- Selecting next wake-up interval
- SD bus suspend/resume (guarded)
- Entering deep sleep

Feature guards:
- `#ifdef SD_MODULE` — SD bus suspend/resume operations
- WiFi/BT radio shutdown always included (needed for deep sleep)

(Sensor power/serial control handled internally by `sensor_engine.cpp`.)

The system sleeps between:
- Measurement window
- BLE window
- GPRS/MQTT sync window

---

## 3.10 `error_sensor.cpp` / `.h`

🔹 **Error reporting + ISR handler (always included)**

Includes:
- `error(code, led_pin)` → LED pattern for fatal errors
- `InterruptServiceRoutine()` for GPIO events

Used for debugging and sensor wake-up events.

---

## 3.11 `utilities.h`

🔹 **LilyGO board pin definitions**

Contains board-specific pin mappings for various LilyGO T-series boards (`LILYGO_T_A7670`, `LILYGO_T_CALL_A7670_V1_0`, etc.). Does **not** define ModuleConfig, TelemetryPayload, or status bits (those moved to `n_logger_config.h`).

---

## 3.12 `config_template.h`

🔹 **Deployment configuration template**

Copy to `config.h` and edit for your specific deployment. Contains:
- SIM/APN credentials
- ThingsBoard MQTT host/token/topic
- BLE UUIDs and device name
- Sensor enablement documentation (flags set in platformio.ini)
- `N_LOGGER_DEVICE_NAME` and `MQTT_CLIENT_ID`

---

# 4. Compile-Time Feature Toggles

Feature toggles are passed as PlatformIO `build_flags`:

| Flag | Effect |
|------|--------|
| `-DSD_MODULE` | Enables SD card support (SdFat, CSV flushing) |
| `-DGPRS_MODULE` | Enables LTE modem and MQTT (TinyGSM, PubSubClient) |
| `-DSENSOR_MAXBOTIX_UART` | Enables MaxBotix MB12xx ultrasonic sensor (UART) |
| `-DSENSOR_I2C_TEMPERATURE` | Enables I2C temperature/humidity sensor |
| `-DSENSOR_RADAR` | Enables radar distance sensor placeholder |
| `-DN_LOGGER_DEVICE_NAME="..."` | Sets device name for serial banners |
| `-DMQTT_CLIENT_ID="..."` | Sets MQTT client ID |

Build environments:
- `n-logger-maxbotix-sd-ble-gprs` — Full feature (equivalent to original)
- `n-logger-maxbotix-sd-ble` — No modem (lighter binary)
- `n-logger-maxbotix-ble-gprs` — No SD card
- `n-logger-i2ctemp-sd-ble-gprs` — Temperature sensor variant
- `n-logger-radar-sd-ble-gprs` — Radar sensor variant

---

# 5. Data Flow Summary

### 5.1 Measurement Cycle

```
wake → sensorReadAll() → battery/temperature →
pack telemetry → write EEPROM page → sleep
```

### 5.2 Flush Cycle (SD)

Triggered when EEPROM write count reaches `EEPROM_FLUSH_AFTER`.

```
EEPROM pages → CSV lines in YYYYMMDD_P.CSV
```

### 5.3 Upload Cycle (MQTT)

```
CSV lines → JSON payloads (with "sensorReadings" key) → MQTT publish → mark sent → rename file
```

### 5.4 Failure Recovery Cycle (GPRS/MQTT)

```
initA7670() fails → failedTransmissionCount++
→ if count >= 3: forceModemReset = true and counter reset to 0
→ device sleeps
→ next scheduled GPRS window: initA7670() enters hard-reset path
→ if reconnect succeeds: upload resumes from buffered EEPROM/SD data
```

This strategy preserves data and avoids aggressive modem power toggling on transient network outages.

### 5.5 BLE Configuration

```
BLE window open → user sends command → config update → save config → close BLE
```

---

# 6. Status Bit System

Status bits act as a compact health indicator.

Examples:
- **Water level sensor failure** (bit 16)
- **SD card failure** (bit 17)
- **Temperature out of range** (bit 18)
- **GPRS/modem failure** (bit 19)

Published as:

```json
"status": <uint32>
```

Dashboards can decode and display alerts.

---

# 7. Power Management Strategy

- Minimize modem uptime
- Enable BLE only on user request
- Batch writes to SD (to reduce power spikes)
- Sleep between cycles
- Battery threshold triggers **failsafe mode**:
  - Slow measurements
  - Minimal BLE
  - Almost no GPRS

---

# 8. File Output Lifecycle

1. Raw measurement → EEPROM page
2. EEPROM → `_P.CSV`
3. `_P.CSV` → MQTT → mark lines as sent
4. `_P.CSV` → `.CSV` archive

This ensures:
- No data loss
- Recovery after power outage
- Offline operation for days/weeks

---

# 9. Module Interaction Diagram

```mermaid
sequenceDiagram
    autonumber
    participant ESP32
    participant RTC
    participant SENSOR
    participant EEPROM
    participant SD
    participant MODEM
    participant BLE
    participant CLOUD

    ESP32->>RTC: Get timestamp
    ESP32->>SENSOR: sensorReadAll()
    ESP32->>EEPROM: Store page
    ESP32->>SD: Flush if needed
    ESP32->>MODEM: Request upload window
    MODEM->>CLOUD: Publish telemetry
    CLOUD-->>MODEM: ACK
    ESP32->>BLE: Open config window (optional)
    ESP32->>ESP32: Deep sleep
```

---

# 10. Key Changes from Original Architecture

| Aspect | Original (n-level) | Refactored (n-logger) |
|--------|-------------------|----------------------|
| Entry point | `n_level_gprs.ino` | `main.cpp` |
| Central types | Duplicated across 4+ files | Single `n_logger_config.h` |
| Sensor code | Inline in .ino / measurement_storage | `sensor_engine.h` / `.cpp` |
| Sensor selection | `#define SENSOR_TYPE N` in config | Descriptive `-DSENSOR_MAXBOTIX_UART` build flags |
| Sensor API | 8+ functions (init/read/power/end) | 1 function: `sensorReadAll()` |
| Sensor pins | Global `SENSOR_PWR_PIN`, `SENSOR_RX_PIN` | Per-sensor: `MAXBOTIX_PWR_PIN`, `MAXBOTIX_RX_PIN` |
| Feature toggles | `#define SD_MODULE true` in .ino | `-DSD_MODULE` in platformio.ini |
| JSON key | `waterLevelSensor` | `sensorReadings` |
| MQTT client ID | Hardcoded `"lilygoClient"` | Configurable `MQTT_CLIENT_ID` |
| BLE device name | Hardcoded `"LILYGO_A7670_BLE"` | Configurable `N_LOGGER_BLE_NAME` |
| `changeBit()` | In .ino | In `n_logger_config.h` as inline |
| `extractJSON()` | In .ino | In `main.cpp` (preserved) |
| Build environments | 1 (`esp32dev`) | 5 differentiated environments |
| Unused code | `extractJSON()` | Preserved in `main.cpp` |

---

# 11. Why This Architecture Works Well

### ✔ Designed for field deployment
Works for months on battery with minimal supervision.

### ✔ Offline-first
Device does **not** rely on continuous network coverage.

### ✔ Data integrity
EEPROM → SD → cloud ensures *no single point of failure*.

### ✔ Extensible
Easy to add new sensors: add a new `#ifdef SENSOR_XXX` block in `sensor_engine.cpp` with pins/read function + hook into `sensorReadAll()`.
Easy to add new BLE/MQTT commands.
Multiple sensors can be combined by enabling multiple build flags.

### ✔ Flash-efficient
No virtual functions — compile-time polymorphism via `#ifdef` blocks.
Feature toggles allow smaller binaries when SD or GPRS is not needed.

### ✔ Debug-friendly
BLE streaming, LED error patterns, serial logs (after resistor fix).

---

# 12. Related Documentation

- **INSTALLATION.md** – how to build & flash
- **HARDWARE_SETUP.md** – wiring + resistor modification
- **BLE_COMMANDS.md** – command protocol
- **EEPROM_SD_FORMAT.md** – data storage formats
- **MQTT_RPC_GUIDE.md** – advanced cloud functions
- **TROUBLESHOOTING.md** – diagnostic guide