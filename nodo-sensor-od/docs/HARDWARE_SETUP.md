# Hardware Setup & Board Modifications

This document describes all required hardware connections, recommended wiring,
and the board-level modifications needed for reliable operation of the
**LilyGO T-A7670SA Water Level Telemetry System**.

---

# 1. Overview of Required Components

### Mandatory Components
- LilyGO **T-A7670SA** board (ESP32 + SIM7670 modem)
- **SD Card** (FAT32 formatted)
- **Water level sensor** (UART-based)
- **Li-ion/LiPo battery** compatible with LilyGO power input

### Optional Components
- Debug USB-UART adapter (if needed)
- SMD soldering tools (for resistor modification)

---

# 2. ESP32 / LilyGO Board Pin References

Most pins are set inside `utilities.h`.

### Sensor Interface
- `SENSOR_PWR_PIN` – Powers the water level sensor  
- `SENSOR_RX_PIN` – UART RX  
- `SENSOR_TX_PIN` – UART TX

### LED Indicator
- `LED_PIN` is used for:
  - Status blinks  
  - Error codes (via `error()` function)

---

# 3. Water Level Sensor Wiring

Expected UART sensor at 9600 baud.

| Sensor Pin | ESP32 Pin |
|------------|-----------|
| VCC        | SENSOR_PWR_PIN |
| GND        | GND |
| TX         | SENSOR_RX_PIN |
| RX         | SENSOR_TX_PIN |

Sensor flow:
1. Power on via SENSOR_PWR_PIN  
2. Start UART  
3. Collect N samples  
4. Power off  

---

# 4. Battery Voltage Sensing

Voltage read via ADC:

```
analogReadMilliVolts(BOARD_BAT_ADC_PIN) * 2
```

---

# 5. ***Important Board Modification*** (Resistor Removal)

## Why this is required
The LilyGO T-A7670SA shares the ESP32 UART0 (USB serial) with the SIM7670 modem. Check https://github.com/Xinyuan-LilyGO/LilyGo-Modem-Series/tree/main/examples/ModemSleep#readme

This causes:
- Garbled Serial Monitor output  
- Modem AT command failures  
- Debugging instability  

## Fix  
Remove the **small SMD resistor** that bridges ESP32 UART0 ↔ modem UART.

Usually labeled **R6**, depending on board revision.

### Warning
- Requires SMD soldering skills  
- May void warranty  
- Remove **only** the resistor bridging UART0 & modem  

After removal:
- USB serial becomes dedicated to ESP32 debug
- Modem uses an alternate UART defined in `modem_mqtt.cpp`

---

# 6. Power Considerations

- For deployments: use good-quality LiPo battery  
- Ensure all grounds are common  

---

# 7. Deployment Checklist

### Before flashing:
✔ ESP32 configured in Arduino IDE  
✔ Partition = Huge APP  
✔ Required libraries installed
✔ SD card formatted  

### Before deploying:
✔ RTC time set (`D-YYYY-MM-DD-HH-MM-SS`)  
✔ Timing profile set (`T-X-Y-Z`)  
✔ Test measurement  
✔ Verified EEPROM writes  
✔ Verified SD flush  
✔ MQTT telemetry confirmed  

---

# 8. Related Documentation
- INSTALLATION.md  
- BLE_COMMANDS.md
- ARCHITECTURE.md  
- EEPROM_SD_FORMAT.md  