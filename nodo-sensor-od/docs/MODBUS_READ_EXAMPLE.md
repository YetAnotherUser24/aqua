# Modbus RTU Read Example

This file shows a practical example for reading registers from a Modbus sensor
using the helper module in this project.

---

## 1. What this example does

- Initializes an RS-485 Modbus RTU session on `Serial2`
- Configures DE/RE direction control pin
- Reads 2 holding registers from slave address `1`
- Prints decoded values and detailed errors to Serial

---

## 2. Example Sketch

```cpp
#include <Arduino.h>
#include <ModbusRtu.h>

// UART pins for ESP32 Serial2 (adjust to your wiring)
static constexpr int8_t kRs485RxPin = 16;
static constexpr int8_t kRs485TxPin = 17;

// DE/RE pin for RS-485 transceiver (MAX485, SP3485, etc.)
static constexpr uint8_t kRs485DirectionPin = 4;

// Sensor settings
static constexpr uint8_t kSensorSlaveId = 1;
static constexpr uint8_t kReadHoldingRegistersFn = 0x03;
static constexpr uint16_t kStartRegister = 0x0000;
static constexpr uint16_t kRegisterCount = 2;

modbus_rtu::ModbusRtu modbus(Serial2);

void printReadError(const modbus_rtu::ModbusRtu::ReadResult &result) {
  Serial.print("Read failed: ");
  Serial.println(result.error);

  if (result.isException) {
    Serial.print("Modbus exception code: ");
    Serial.println(result.exceptionCode);
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  modbus_rtu::ModbusRtu::Config cfg;
  cfg.directionPin = kRs485DirectionPin;
  cfg.manualDirection = true;
  cfg.txActiveHigh = true;

  // Timing values that usually work for low/medium baud RS-485 links.
  cfg.responseTimeoutMs = 300;
  cfg.txEnableGuardUs = 250;
  cfg.txDisableGuardUs = 250;
  cfg.interFrameGapMs = 4;
  cfg.rxTurnaroundUs = 0;

  modbus.configure(cfg);
  modbus.begin(9600, kRs485RxPin, kRs485TxPin, SERIAL_8N1);

  Serial.println("Modbus session started");
}

void loop() {
  modbus_rtu::ModbusRtu::ReadResult result = modbus.readRegisters(
      kSensorSlaveId,
      kReadHoldingRegistersFn,
      kStartRegister,
      kRegisterCount);

  if (!result.success) {
    printReadError(result);
    delay(2000);
    return;
  }

  Serial.print("Responder ID: ");
  Serial.println(result.responderId);

  Serial.print("Registers read: ");
  Serial.println(result.count);

  for (uint8_t i = 0; i < result.count; ++i) {
    Serial.print("reg[");
    Serial.print(i);
    Serial.print("] = ");
    Serial.println(result.values[i]);
  }

  // Example scaling (depends on your sensor map):
  // if reg0 is temperature in 0.1 C units:
  // float tempC = result.values[0] / 10.0f;
  // Serial.print("Temperature C: ");
  // Serial.println(tempC, 1);

  delay(2000);
}
```

---

## 3. Wiring notes

- Connect `A/B` RS-485 lines with correct polarity.
- Connect GND between ESP32 and sensor/transceiver.
- If your transceiver uses separate DE and RE pins, tie them together and use
  one GPIO (`kRs485DirectionPin`).
- If your hardware controls direction automatically, set:
  - `manualDirection = false`
  - `directionPin = modbus_rtu::ModbusRtu::InvalidDirectionPin`

---

## 4. If reading fails

- Verify slave ID, baudrate, parity, stop bits, and function code.
- Increase `responseTimeoutMs` for slow sensors.
- Increase `txEnableGuardUs` and `txDisableGuardUs` if your transceiver needs
  more settle time.
- Check register address and quantity against the sensor datasheet.
