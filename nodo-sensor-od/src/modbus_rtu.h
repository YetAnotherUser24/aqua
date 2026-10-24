/**
 * @file modbus_rtu.h
 * @brief Modbus RTU helper module for the ESP32 water level firmware.
 *
 * The module provides a small, function-oriented API for configuring an RTU
 * master session, handling half-duplex direction control, and issuing common
 * register read/write operations.
 */

#pragma once

#include <Arduino.h>

namespace modbus_rtu {

class ModbusRtu {
public:
  static constexpr uint8_t MaxRegisters = 16;
  static constexpr uint8_t BroadcastAddressLow = 0x00U;
  static constexpr uint8_t BroadcastAddressHigh = 0xFFU;
  static constexpr uint8_t InvalidDirectionPin = 0xFFU;

  /**
   * @brief Result returned by a Modbus register read.
   */
  struct ReadResult {
    bool success = false;
    bool isException = false;
    uint8_t exceptionCode = 0;
    uint8_t responderId = 0;
    const char* error = "unknown";
    uint16_t values[MaxRegisters] = {};
    uint8_t count = 0;
  };

  /**
   * @brief Result returned by a Modbus single-register write.
   */
  struct WriteResult {
    bool success = false;
    bool isException = false;
    uint8_t exceptionCode = 0;
    uint8_t responderId = 0;
    uint16_t registerAddress = 0;
    uint16_t registerValue = 0;
    const char* error = "unknown";
  };

  /**
   * @brief Runtime settings for a Modbus RTU master session.
   */
  struct Config {
    uint8_t directionPin = InvalidDirectionPin;
    bool manualDirection = false;
    bool txActiveHigh = true;
    uint32_t responseTimeoutMs = 180;
    uint16_t txEnableGuardUs = 250;
    uint16_t txDisableGuardUs = 250;
    uint16_t interFrameGapMs = 4;
    uint16_t rxTurnaroundUs = 0;
  };

  explicit ModbusRtu(HardwareSerial& serialPort);
  ModbusRtu(HardwareSerial& serialPort, const Config& sessionConfig);

  void configure(const Config& config);
  void begin(uint32_t baud, int8_t rxPin, int8_t txPin, uint32_t serialConfig = SERIAL_8N1);

  void setTiming(uint32_t responseTimeoutMs, uint16_t txEnableGuardUs, uint16_t txDisableGuardUs,
                 uint16_t interFrameGapMs, uint16_t rxTurnaroundUs = 0);

  void setDirectionControl(uint8_t directionPin, bool manualDirection = true,
                           bool txActiveHigh = true);
  void setTxActiveHigh(bool txActiveHigh);
  void setManualDirection(bool manualDirection);

  void clearRxBuffer();
  void setReceiveMode();
  void setTransmitMode();

  static bool isBroadcastAddress(uint8_t address);
  static uint16_t crc16(const uint8_t* data, size_t length);

  ReadResult readRegisters(uint8_t slaveId, uint8_t functionCode, uint16_t startAddress,
                           uint16_t registerCount);
  WriteResult writeSingleRegister(uint8_t slaveId, uint16_t registerAddress, uint16_t value);

private:
  struct ResponseCheck {
    bool valid = false;
    bool isException = false;
    uint8_t responderId = 0U;
    uint8_t exceptionCode = 0U;
    const char* error = "unknown";
    size_t frameOffset = 0U;
    size_t frameLength = 0U;
  };

  void applyDirection(bool transmit);
  bool collectResponse(uint8_t* response, size_t responseCapacity, size_t& responseLength);
  bool sendRequestAndCollectResponse(const uint8_t* request, size_t requestLength,
                                     uint8_t* response, size_t responseCapacity,
                                     size_t& responseLength);

  static void buildRequestFrame(uint8_t slaveId, uint8_t functionCode, uint16_t firstValue,
                                uint16_t secondValue, uint8_t* request);
  static ResponseCheck validateResponse(uint8_t slaveId, uint8_t functionCode,
                                        const uint8_t* response, size_t responseLen);

  HardwareSerial* serial_ = nullptr;
  Config config_ = {};
};

} // namespace modbus_rtu