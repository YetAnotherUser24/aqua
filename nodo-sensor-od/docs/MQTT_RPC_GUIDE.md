# MQTT & RPC Integration Guide  
This document describes how the LilyGO A7670SA Water Level Telemetry System communicates with **ThingsBoard**.  
It includes telemetry format, timestamp rules, attributes, server-side RPC, modem logic, and reconnection behavior.

---

# 1. Overview

The device performs **batch MQTT uploads** after collecting measurements stored in SD/EEPROM.

Key features:

- MQTT publish with timestamps  
- JSON payloads generated from CSV data  
- Offline buffering (EEPROM → SD)  
- Automatic retry logic  
- Server-side RPC (for future expansion)  

---

# 2. MQTT Broker Configuration

Default settings:

```
Host: thingsboard.cloud
Port: 1883 (non-TLS)
TLS Port: 8883 (optional)
Client ID: LilyGO-A7670SA
Username: <Device Access Token>
Password: (empty)
```

Topic used for telemetry:

```
v1/devices/me/telemetry
```

---

# 3. Telemetry Payload Format

Each CSV line is converted into the following JSON payload:

```json
{
  "ts": 1764376680000,
  "values": {
    "waterLevelSensor": [128,129,128,130,131],
    "battery": 3980,
    "temperature": 2450,
    "status": 0,
    "eepromFreePages": 1500,
    "sdFreeMB": 28700
  }
}
```

### Timestamp Rules
- `ts` is in **milliseconds**, converted from RTC timestamp.
- If RTC is invalid, device marks status bit and skips upload.

---

# 4. Telemetry Fields

| Field | Source | Description |
|-------|--------|-------------|
| waterLevelSensor | Sensor samples | Raw water level readings |
| battery | ADC | Battery voltage (mV) |
| temperature | RTC | Temperature (centi °C) |
| status | firmware | Bitmask of health flags |

---

# 5. Upload Cycle Logic

1. Enumerate **pending CSV files**  
2. For each file:
   - Open file  
   - Process each row  
   - Convert row → JSON  
   - Publish JSON  
   - Mark row as `sent=1`  
3. After all rows in file are marked:
   - Rename `_P.CSV` → `.CSV` (archive)

Uploads are **incremental** and safe against resets.

---

# 6. MQTT Connection Flow (Modem)

Modem sequence in `modem_mqtt.cpp`:

```
POWER ON modem
Initialize modem
Set APN
Check network registration
Open MQTT session
Publish packets
Close session
Enter modem sleep mode
```

Retries are performed if:
- APN fails  
- Signal weak  
- Broker does not respond  
- Packet publish fails  

Consecutive connection failures are counted in `failedTransmissionCount`.

When failures reach `MAX_FAILED_TRANSMISSIONS = 3`:

1. `forceModemReset` is set to `true`
2. Counter is reset to `0`
3. Device sleeps and waits for next GPRS schedule
4. On next GPRS window, `initA7670()` performs full modem hard reset (reset + PWRKEY pulse)

This gives a controlled recovery path without dropping buffered telemetry.

---

# 7. Server-Side RPC

The firmware allows future addition of RPC handlers.

ThingsBoard RPC topic:

```
v1/devices/me/rpc/request/+
```

Possible supported RPCs:

| RPC | Description |
|------|-------------|
| `T-5-1-10` | Set interval times |
| `D-2025-11-28-23-38-00` | Set RTC timestamp |


Example RPC request:

```json
{
  "method": "T-5-1-10",
  "params": {}
}
```

Device response:
```json
{
  "success": true
}
```

---

# 8. Handling Upload Failures

If MQTT publish fails:

- Row is not marked as sent  
- CSV file remains in `_P.CSV` stage  
- Upload is retried on next GPRS window  

This guarantees **no data loss** unless SD is corrupted.

---

# 9. Related Documentation

- **EEPROM_SD_FORMAT.md**  
- **BLE_COMMANDS.md**  
- **ARCHITECTURE.md**  
- **INSTALLATION.md**

