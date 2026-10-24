# BLE Command Guide

This document defines the complete BLE command protocol used to configure and
interact with the **LilyGO A7670SA Water Level Telemetry System**.  
All commands are exchanged through a single BLE characteristic in plain text.

---

# 1. Overview

BLE is used for:

- Setting measurement intervals  
- Setting BLE and GPRS windows  
- Setting RTC date/time  
- Forcing manual upload  
- Live sensor streaming via Serial-to-BLE mirroring  
- Reading current configuration  

BLE activates only when requested and shuts down automatically to save power.

---

# 2. BLE Device Information

### Device Name
```
n-logger-BLE
```

### Service UUID
```
4fafc201-1fb5-459e-8fcc-c5c9c331914b
```

### Characteristic UUID (Read/Write/Notify)
```
beb5483e-36e1-4688-b7f5-ea07361b26a8
```

All commands are written to this characteristic. Streaming data is pushed
to the same characteristic via BLE Notify.

---

# 3. Command Format

All commands follow the syntax:

```
<COMMAND>-<ARGS>
```

Commands are case-insensitive (the firmware uppercases them internally).

Examples:
```
T-5-1-10
D-2025-11-28-23-38-00
START
STOP
```

---

# 4. Timing Configuration Command

### Format
```
T-<MEASURE_INTERVAL>-<BLE_WINDOW>-<GPRS_WINDOW>
```

### Parameters

| Field | Meaning |
|-------|---------|
| MEASURE_INTERVAL | minutes between measurements |
| BLE_WINDOW | minutes BLE stays active after a command session |
| GPRS_WINDOW | minutes between modem upload attempts |

### Example
```
T-5-1-10
```

Meaning:
- Take one measurement every **5 minutes**  
- Keep BLE active for **1 minute**  
- Connect modem every **10 minutes** to upload data  

---

# 5. RTC Time‑Setting Command

### Format
```
D-YYYY-MM-DD-HH-MM-SS
```

### Example
```
D-2025-11-28-23-38-00
```

On success, the device confirms via BLE Notify:
```
New date: 2025-11-28 23:38:00
```

The RTC time is used for:
- EEPROM timestamps  
- CSV naming  
- MQTT payload timestamps  

---

---

# 6. Continuous Monitoring & Streaming Commands

### Continuous Monitoring & Recording (`MONITOR` or `SEND`)
```
MONITOR
MONITOR-5
MONITOR-10
SEND
```
- Starts continuous measurement and data recording.
- **Default interval**: 5 seconds (or specify custom interval like `MONITOR-10` for 10 seconds).
- **Data Saving**: Each interval runs `takeMeasurement()`, saving complete measurement records (RTC timestamp, temperature, sensor channels, battery, status) into **EEPROM** and automatically rotating into **SD card CSV** files.
- **OLED 1.3" Visualization**: Constantly renders real-time sensor measurements (e.g. DO mg/L, %Sat, Temperature, Battery voltage, interval, RTC clock) onto an attached 1.3" SH1106 I2C OLED display.
- **BLE Notification**: Pushes the live reading (e.g. Dissolved Oxygen) to connected BLE client characteristics (`...a8` and `...a9`).
- Keeps BLE connection active while monitoring.

### Enable Serial-to-BLE Log Mirroring (`START`)
```
START
```
When issued, the device sets a persistent flag (`bleStreamingActive = true`
in RTC backup memory) that survives deep sleep. On every subsequent wake cycle:
1. **BLE is automatically turned on**.
2. All `Log()`/`Logln()`/`Logf()` output is pushed to the connected BLE client via Notify.

### Stop Monitoring & Streaming (`STOP`)
```
STOP
```
- Disables continuous monitoring/recording and log streaming.
- Restores normal scheduled deep-sleep power saving mode.

---

# 7. Reserved Commands

### DATA
```
DATA
```
Placeholder for future data download functionality.

---

# 8. BLE Troubleshooting

### 1. Device does not appear in scan  
✔ Ensure BLE window is open (send `T-1-5-10` to make it appear every
   minute for 5 minutes)  
✔ Ensure board is not sleeping  
✔ Check LED indicator (steady ON = BLE active, blinking = BLE active
   but no client connected)

### 2. Commands not accepted  
✔ Ensure correct format  
✔ Check MTU settings in your BLE app  
✔ Three LED blinks = unknown command

### 3. Streaming stops abruptly  
✔ BLE window expired (default 30 seconds)  
✔ Weak signal  
✔ Device entered measurement or upload cycle  

---

# 9. Related Documentation

- **ARCHITECTURE.md** – how BLE integrates with whole system  
- **INSTALLATION.md** – setup and building  
- **HARDWARE_SETUP.md** – wiring and board modification  
- **EEPROM_SD_FORMAT.md** – data storage formats  
- **MQTT_RPC_GUIDE.md** – cloud integrations