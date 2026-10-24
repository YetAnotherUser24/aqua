# Aqualabo DIGISENS Optical Dissolved Oxygen (OPTOD / OD) Sensor - Measurements

## Overview
Communicates via Modbus RTU over RS-485 at 9600 baud (8N1).
DIGISENS sensors require an explicit measurement order before reading registers.

## Measurement Sequence
1. **Order Measurement**: Write `0x001F` to holding register `0x0001` (Modbus Function Code `0x06` - Write Single Register).
2. **Sampling Delay**: Read holding register `0x00A4` (sampling delay in ms, default ~250ms) or wait 250ms.
3. **Read Results**: Read 10 consecutive holding registers starting at `0x0053` (Modbus Function Code `0x03` - Read Holding Registers).

## Register Map

| Register | PLC Address | Parameter | Type | Format | Telemetry Slot | Scale / Conversion | Units |
|----------|-------------|-----------|------|--------|----------------|--------------------|-------|
| 0x0053-0x0054 | 40084-40085 | Temperature | float (32-bit) | IEEE 754 | `sensorReadings[0]` | `temp * 100` (centi-°C) | °C |
| 0x0055-0x0056 | 40086-40087 | Parameter 1: DO Saturation | float (32-bit) | IEEE 754 | `sensorReadings[1]` | `Sat * 10` (deci-%Sat) | % |
| 0x0057-0x0058 | 40088-40089 | Parameter 2: Dissolved Oxygen | float (32-bit) | IEEE 754 | `sensorReadings[2]` | `DO * 1000` (milli-mg/L) | mg/L (ppm) |
| 0x0059-0x005A | 40090-40091 | Parameter 3: Raw / Reserved | float (32-bit) | IEEE 754 | `sensorReadings[3]` | `P3 * 100` | — |
| 0x005B-0x005C | 40092-40093 | Parameter 4: Raw / Reserved | float (32-bit) | IEEE 754 | `sensorReadings[4]` | `P4 * 100` | — |

## Error Codes
The sensor returns specific numeric codes in place of measurement values on fault conditions:
- `9999`: Value out of range (check calibration / fluid characteristics)
- `9998`: Init default value at power up (no measurement available yet)
- `9996`: Measurement overflowed SDI12 digits

## Protocol Settings
- **Interface**: Modbus-RTU over RS-485
- **Default Baud**: 9600, 8 data bits, no parity, 1 stop bit (8N1)
- **Default Slave ID**: 10 (0x0A)
- **Status Bit**: `STATUS_OD_SENSOR_BIT` (Bit 22)
