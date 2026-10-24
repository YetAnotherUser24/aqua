# Modbus RTU 485 Guide

This document describes the Modbus RTU helper module used by the firmware to
communicate with RS-485 devices.

It covers session setup, timing control, request/response behavior, broadcast
handling, and the result structures returned by the helper API.

---

# 1. Overview

The Modbus helper provides a small RTU master API for:

- Starting a UART-backed Modbus session
- Controlling half-duplex direction pins
- Reading holding/input registers
- Writing a single holding register
- Validating CRC and Modbus exception frames

The implementation is kept in `src/ModbusRtu.cpp` and the public interface
is exposed from `src/ModbusRtu.h`.

---

# 2. Public API

The module exposes a `ModbusRtu` class with instance methods:

```cpp
modbus_rtu::ModbusRtu::Config config;
modbus_rtu::ModbusRtu modbus(Serial2, config);

modbus.begin(9600, RX_PIN, TX_PIN);
```

Common API calls:

- `configure(config)`
- `begin(baud, rxPin, txPin)`
- `setTiming(responseTimeoutMs, txEnableGuardUs, txDisableGuardUs, interFrameGapMs, rxTurnaroundUs)`
- `setDirectionControl(directionPin, manualDirection, txActiveHigh)`
- `readRegisters(slaveId, functionCode, startAddress, registerCount)`
- `writeSingleRegister(slaveId, registerAddress, value)`

---

# 3. Instance Configuration

### `ModbusRtu::Config`

The configuration structure controls UART direction and timing:

| Field | Meaning |
|-------|---------|
| `directionPin` | GPIO used for DE/RE control on the RS-485 transceiver |
| `manualDirection` | Enables manual transmit/receive switching |
| `txActiveHigh` | Direction-pin polarity for transmit mode |
| `responseTimeoutMs` | Maximum time to wait for a Modbus reply |
| `txEnableGuardUs` | Delay before transmitting after enabling the driver |
| `txDisableGuardUs` | Delay after transmission before switching back to receive |
| `interFrameGapMs` | Silent-gap threshold used to detect end of frame |
| `rxTurnaroundUs` | Additional delay after switching to receive mode |

### `ModbusRtu`

`ModbusRtu` stores:

- A pointer to the active `HardwareSerial`
- The current `Config`

The request lifecycle and protocol validation are handled as private methods.

---

# 4. Request Flow

Every request follows the same basic sequence:

1. Clear any pending bytes from the RX buffer
2. Switch the RS-485 transceiver into transmit mode
3. Send an 8-byte Modbus RTU request frame
4. Flush the UART
5. Switch back to receive mode
6. Collect the response until a silent inter-frame gap is detected

The helper then validates:

- CRC16
- Slave address
- Function code
- Modbus exception responses

---

# 5. Read Register Requests

### API

```cpp
ModbusRtu::ReadResult readRegisters(uint8_t slaveId,
                                    uint8_t functionCode,
                                    uint16_t startAddress,
                                    uint16_t registerCount);
```

### Behavior

- `registerCount` must be between 1 and `ModbusRtu::MaxRegisters`
- The function code is passed in by the caller
- The response byte count must be even
- Register values are returned in `result.values[]`

### `ReadResult`

| Field | Meaning |
|-------|---------|
| `success` | `true` when the request completed successfully |
| `isException` | `true` when the slave returned a Modbus exception |
| `exceptionCode` | Modbus exception code, if present |
| `responderId` | Slave ID that answered |
| `error` | Short status string such as `ok`, `timeout or short response`, or `crc mismatch` |
| `values` | Register values returned by the slave |
| `count` | Number of valid register values copied into `values` |

---

# 6. Write Register Requests

### API

```cpp
ModbusRtu::WriteResult writeSingleRegister(uint8_t slaveId,
                                           uint16_t registerAddress,
                                           uint16_t value);
```

### Behavior

- Uses Modbus function code `0x06`
- Verifies the echoed address and echoed value on success
- Broadcast writes do not require a reply
- Broadcast slave IDs accepted by the helper are `0x00` and `0xFF`

### `WriteResult`

| Field | Meaning |
|-------|---------|
| `success` | `true` when the write completed successfully |
| `isException` | `true` when the slave returned a Modbus exception |
| `exceptionCode` | Modbus exception code, if present |
| `responderId` | Slave ID that answered |
| `registerAddress` | Echoed register address |
| `registerValue` | Echoed register value |
| `error` | Short status string such as `ok`, `timeout or short response`, or `write echo mismatch` |

---

# 7. Frame Format

### Request frame

All request frames are 8 bytes:

```text
[slaveId][function][addrHi][addrLo][valueHi/countHi][valueLo/countLo][crcLo][crcHi]
```

### Response frame

Responses are validated as Modbus RTU frames with CRC16 appended in low-byte
first order.

For read operations, the data frame layout is:

```text
[slaveId][function][byteCount][data...][crcLo][crcHi]
```

For write acknowledgements, the frame layout is:

```text
[slaveId][function][addrHi][addrLo][valueHi][valueLo][crcLo][crcHi]
```

---

# 8. Broadcast Handling

Broadcast addresses are treated specially:

- `0x00` and `0xFF` are accepted as broadcast addresses
- Broadcast writes are considered successful even if no slave replies
- Broadcast reads are not useful in standard Modbus RTU usage

---

# 9. Error Handling

The helper reports a short error string instead of throwing exceptions.

Common values include:

- `invalid registerCount`
- `timeout or short response`
- `crc mismatch`
- `unexpected slave id`
- `unexpected function code`
- `modbus exception`
- `unexpected register payload size`
- `write echo mismatch`

This keeps the API easy to inspect on embedded targets where exceptions are not
used.

---

# 10. Example Usage

```cpp
#include <ModbusRtu.h>

modbus_rtu::ModbusRtu modbus(Serial2);

void setup() {
  modbus_rtu::ModbusRtu::Config config;
  config.directionPin = 4;
  config.manualDirection = true;
  config.txActiveHigh = true;

  modbus.configure(config);
  modbus.begin(9600, 16, 17);
  modbus.setTiming(180, 250, 250, 4, 0);
}

void loop() {
  auto read = modbus.readRegisters(1, 0x03, 0x0000, 2);
  if (read.success) {
    // Use read.values[0..read.count-1]
  }
}
```

---

# 11. Related Files

- `src/ModbusRtu.h`
- `src/ModbusRtu.cpp`
- `docs/ARCHITECTURE.md`
- `docs/INSTALLATION.md`
