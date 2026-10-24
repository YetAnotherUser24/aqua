# Web Master Telemetry & Command Protocol (AquaControl)

This document specifies the wire protocol between the **OD-Logger** ESP32 firmware running in `WEB_MASTER_MODE` and the **AquaControl** Next.js cloud web dashboard (`aquacontrol-two.vercel.app`).

---

## 1. Protocol Architecture Overview

Communication operates over **HTTPS REST** with JSON payloads.

```
                  +-------------------------+
                  |  AquaControl Dashboard  |
                  |     (Next.js / Vercel)  |
                  +-------------------------+
                       ^               ^
                       | (HTTPS POST)  | (HTTPS GET/POST)
                       v               v
            +-------------------------------------+
            |         OD-Logger (ESP32)           |
            |           WEB_MASTER_MODE           |
            +-------------------------------------+
                       |
        +--------------+---------------+
        |                              |
(RS-485 Modbus RTU)               (SPI Bus)
        v                              v
Aqualabo OPTOD Sensor           MicroSD Card (/EXP_XX.CSV)
```

The system employs a dual-channel command retrieval strategy:
1. **Active Streaming Channel (Piggybacking)**: During active monitoring or experiments, incoming telemetry upload responses (`POST /api/telemetry/sensor`) include any pending command in the JSON body.
2. **Idle Polling Channel**: When the logger is idle (no active experiment or monitoring), it polls `GET /api/commands/pending` every 2000 ms.

---

## 2. Authentication & Headers

All HTTP requests from the logger include:
* `Content-Type: application/json`
* `X-Device-Key: <AQUACONTROL_DEVICE_KEY>` (defined in `src/config.h`)

---

## 3. Endpoints Specification

### 3.1. Telemetry Upload
* **Endpoint**: `POST /api/telemetry/sensor`
* **Timing**: Every `activeInterval` (configured by `interval_sec`, typically 2000 ms or 5000 ms).
* **Request Payload**:
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
* **Response (No Command Pending)**:
  ```json
  {
    "success": true,
    "has_command": false,
    "message": "Telemetría de sensor recibida y procesada correctamente"
  }
  ```
* **Response (Command Pending)**:
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
      },
      "created_at": "2026-09-12T09:50:00.000Z"
    }
  }
  ```

---

### 3.2. Pending Commands Polling
* **Endpoint**: `GET /api/commands/pending`
* **Timing**: Every 2000 ms **only while in IDLE state** (`!experimentActive && !webMonitorActive`).
* **Response**: Returns either `{ "has_command": false }` or `{ "has_command": true, "command": { ... } }` matching the structure above.

---

### 3.3. Command Acknowledgment
* **Endpoint**: `POST /api/commands/{command_id}/acknowledge`
* **Timing**: Immediately upon executing a received command.
* **Request Payload**:
  ```json
  {
    "success": true,
    "sensor_state": {
      "monitor_active": false,
      "active_experiment": null
    }
  }
  ```
* **Response**: `{ "success": true }` (HTTP 200).

---

## 4. Control Actions Reference

| Action String | Payload Fields | Firmware Actions | State Returned in ACK |
| :--- | :--- | :--- | :--- |
| `start_experiment` | `experiment_id` (str)<br>`name` (str)<br>`csv_filename` (8.3 str)<br>`interval_sec` (int 1–60) | Creates `/EXP_XX.CSV` on SD.<br>Sets `experimentActive = true`, `webMonitorActive = true`.<br>Shows interval on OLED.<br>Keeps sensor rails energized. | `monitor_active: true`<br>`active_experiment: { ... }` |
| `stop_experiment` | `experiment_id` (str) | Flushes and closes SD file.<br>Sets `experimentActive = false`, `webMonitorActive = false`.<br>Powers off sensor rail via `stopPrimarySensorRealtime()`.<br>Fires `ledSignalIdle()` (3 pulses). | `monitor_active: false`<br>`active_experiment: null` |
| `start_monitor`<br>(or `start`) | `interval_sec` (int 1–60) | Sets `webMonitorActive = true`.<br>Shows interval on OLED.<br>Keeps sensor rails energized. | `monitor_active: true`<br>`monitor_interval_sec: N` |
| `stop_monitor`<br>(or `stop`) | *(none)* | Sets `webMonitorActive = false`, `experimentActive = false`.<br>Powers off sensor rail.<br>Fires `ledSignalIdle()` (3 pulses). | `monitor_active: false`<br>`active_experiment: null` |
| `set_sampling_rate`| `interval_sec` (int 1–60) | Updates `experimentIntervalMs` and `webMonitorIntervalMs`. | `monitor_interval_sec: N` |
| `manual_sample` | *(none)* | Sets `manualSampleRequested = true` for one-shot execution in `loop()`. | `manual_sample: "queued"` |
| `sleep` | `minutes` (int)<br>`indefinite` (bool) | Acknowledges command then enters deep sleep. | `monitor_active: false` |

---

## 5. Dedicated Experiment CSV Format

When an experiment is active, rows are appended to the file named in `csv_filename` (e.g. `/EXP_01.CSV`) on the MicroSD card.

### Header (Line 1)
```csv
Timestamp,RTC_Sec,DO_mg_L,Sat_Pct,Temp_C,Batt_V
```

### Data Row (RFC 4180 Compliant)
```csv
2026-09-12T05:14:00-05:00,842512440,7.850,98.50,25.00,3.95
```

* `Timestamp`: Local ISO 8601 string (America/Lima UTC-5).
* `RTC_Sec`: Seconds since 2000-01-01 (DS3231 timestamp).
* `DO_mg_L`: Dissolved oxygen in mg/L (3 decimal places).
* `Sat_Pct`: DO saturation in % (2 decimal places).
* `Temp_C`: Water temperature in °C (2 decimal places).
* `Batt_V`: Battery monitor voltage in Volts (2 decimal places).
