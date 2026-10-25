/**
 * @file pump_bus_real.cpp
 * @brief Real `IPwmActuator` (LEDC MOSFET) + `IRotaryEncoder` (AS5600 over I2C).
 */

#include "pump_bus.h"
#include "config.h"

#include <Arduino.h>
#include <Wire.h>

namespace {

constexpr uint8_t AS5600_ADDR = 0x36;
constexpr uint8_t AS5600_RAW_ANGLE_REG = 0x0C;
constexpr uint16_t AS5600_TICKS_PER_REV = 4096;

class Tb6612PwmActuator : public IPwmActuator {
public:
  void init() override {
    // 1. Configure TB6612 direction and standby control pins
    pinMode(TB6612_STBY_PIN, OUTPUT);
    pinMode(TB6612_AIN1_PIN, OUTPUT);
    pinMode(TB6612_AIN2_PIN, OUTPUT);
    pinMode(TB6612_PWMA_PIN, OUTPUT);

    // Active mode: STBY = HIGH (driver awake), Coasting: AIN1=LOW, AIN2=LOW
    digitalWrite(TB6612_STBY_PIN, HIGH);
    digitalWrite(TB6612_AIN1_PIN, LOW);
    digitalWrite(TB6612_AIN2_PIN, LOW);

    // 2. Configure PWM on PWMA pin
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcAttach(TB6612_PWMA_PIN, PUMP_PWM_FREQ_HZ, PUMP_PWM_RES_BITS);
#else
    uint32_t f = ledcSetup(PUMP_PWM_CHANNEL, PUMP_PWM_FREQ_HZ, PUMP_PWM_RES_BITS);
    ledcAttachPin(TB6612_PWMA_PIN, PUMP_PWM_CHANNEL);
    Serial.printf("[Actuator] TB6612 init: STBY=%d AIN1=%d AIN2=%d PWMA=%d | LEDC chan=%d freq=%u res=%d\n",
                  TB6612_STBY_PIN, TB6612_AIN1_PIN, TB6612_AIN2_PIN, TB6612_PWMA_PIN,
                  PUMP_PWM_CHANNEL, f, PUMP_PWM_RES_BITS);
#endif
    setDutyPercent(0.0f);
  }

  void setDutyPercent(float percent) override {
    percent = constrain(percent, -100.0f, 100.0f);
    _duty = percent;
    const uint32_t maxDuty = (1u << PUMP_PWM_RES_BITS) - 1u;

    if (fabsf(percent) < 0.1f) {
      // Idle / Coast (Toshiba Table Row 4): IN1 = LOW, IN2 = LOW, STBY = HIGH
      // Both output channels high-impedance (coasting). Prevents continuous short-circuit
      // thermal stress on low-side MOSFETs when pump is sitting idle.
      digitalWrite(TB6612_AIN1_PIN, LOW);
      digitalWrite(TB6612_AIN2_PIN, LOW);
      digitalWrite(TB6612_STBY_PIN, HIGH);
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
      ledcWrite(TB6612_PWMA_PIN, 0);
#else
      ledcWrite(PUMP_PWM_CHANNEL, 0);
#endif
      return;
    }

    const uint32_t dutyTicks = (uint32_t)((fabsf(percent) / 100.0f) * maxDuty);

    // Ensure driver is awake
    digitalWrite(TB6612_STBY_PIN, HIGH);

    if (percent > 0.0f) {
      // Forward (CW / Dosing): AIN1 = HIGH, AIN2 = LOW, PWMA = PWM
      digitalWrite(TB6612_AIN2_PIN, LOW);
      digitalWrite(TB6612_AIN1_PIN, HIGH);
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
      ledcWrite(TB6612_PWMA_PIN, dutyTicks);
#else
      ledcWrite(PUMP_PWM_CHANNEL, dutyTicks);
#endif
    } else {
      // Reverse (CCW / Suck-Back): AIN1 = LOW, AIN2 = HIGH, PWMA = PWM
      digitalWrite(TB6612_AIN1_PIN, LOW);
      digitalWrite(TB6612_AIN2_PIN, HIGH);
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
      ledcWrite(TB6612_PWMA_PIN, dutyTicks);
#else
      ledcWrite(PUMP_PWM_CHANNEL, dutyTicks);
#endif
    }
  }

  float dutyPercent() const override { return _duty; }

private:
  float _duty = 0.0f;
};

class As5600Encoder : public IRotaryEncoder {
public:
  void init() override {
    Wire.setClock(400000); // 400 kHz Fast-Mode I2C: ultra-fast non-blocking angle sampling
    Wire.setTimeOut(25);   // 25 ms timeout
    // Fast tracking DSP configuration (CONF = 0x07C1):
    //   - SF = 01 (Fast filter, latency ~0.28 ms vs 2.2 ms slow filter)
    //   - FTH = 111 (10 LSB threshold)
    //   - HYST = 01 (1 LSB hysteresis)
    setConfig(0x07C1);
  }

  bool setConfig(uint16_t conf) override {
    return writeReg16(0x07, conf);
  }

  uint16_t readConfig() override {
    return readReg16(0x07);
  }

  bool resetFactoryDefaults() override {
    bool ok1 = writeReg16(0x01, 0x0000);
    bool ok2 = writeReg16(0x03, 0x0000);
    bool ok3 = writeReg16(0x05, 0x0000);
    bool ok4 = writeReg16(0x07, 0x07C1);
    return (ok1 && ok2 && ok3 && ok4);
  }

  uint16_t readRawAngle() override {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(AS5600_RAW_ANGLE_REG);
    if (Wire.endTransmission(true) != 0) return _last;
    if (Wire.requestFrom(static_cast<uint8_t>(AS5600_ADDR), static_cast<uint8_t>(2)) != 2) return _last;
    const uint16_t hi = Wire.read();
    const uint16_t lo = Wire.read();
    _last = ((hi & 0x0F) << 8) | lo;  // 12-bit
    return _last;
  }

  uint8_t readStatus() override {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(0x0B);
    if (Wire.endTransmission(true) != 0) return 0;
    if (Wire.requestFrom(static_cast<uint8_t>(AS5600_ADDR), static_cast<uint8_t>(2)) != 2) return 0;
    uint8_t st = Wire.read();
    Wire.read();
    return st;
  }

  uint8_t readAgc() override {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(0x1A);
    if (Wire.endTransmission(true) != 0) return 0;
    if (Wire.requestFrom(static_cast<uint8_t>(AS5600_ADDR), static_cast<uint8_t>(2)) != 2) return 0;
    uint8_t agc = Wire.read();
    Wire.read();
    return agc;
  }

  uint16_t readMagnitude() override {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(0x1B);
    if (Wire.endTransmission(true) != 0) return 0;
    if (Wire.requestFrom(static_cast<uint8_t>(AS5600_ADDR), static_cast<uint8_t>(2)) != 2) return 0;
    const uint16_t hi = Wire.read();
    const uint16_t lo = Wire.read();
    return ((hi & 0x0F) << 8) | lo;
  }

  bool writeReg16(uint8_t reg, uint16_t val) {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(reg);
    Wire.write((uint8_t)((val >> 8) & 0xFF));
    Wire.write((uint8_t)(val & 0xFF));
    return (Wire.endTransmission(true) == 0);
  }

  uint16_t readReg16(uint8_t reg) {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(true) != 0) return 0xFFFF;
    if (Wire.requestFrom(static_cast<uint8_t>(AS5600_ADDR), static_cast<uint8_t>(2)) != 2) return 0xFFFF;
    uint16_t hi = Wire.read();
    uint16_t lo = Wire.read();
    return (hi << 8) | lo;
  }

  uint8_t readReg8(uint8_t reg) {
    Wire.beginTransmission(AS5600_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(true) != 0) return 0xFF;
    if (Wire.requestFrom(static_cast<uint8_t>(AS5600_ADDR), static_cast<uint8_t>(1)) != 1) return 0xFF;
    return Wire.read();
  }

  void dumpRegisters() override {
    uint8_t zmco = readReg8(0x00);
    uint16_t zpos = readReg16(0x01) & 0x0FFF;
    uint16_t mpos = readReg16(0x03) & 0x0FFF;
    uint16_t mang = readReg16(0x05) & 0x0FFF;
    uint16_t conf = readReg16(0x07);
    uint8_t status = readReg8(0x0B);
    uint16_t rawAngle = readReg16(0x0C) & 0x0FFF;
    uint16_t angle = readReg16(0x0E) & 0x0FFF;
    uint8_t agc = readReg8(0x1A);
    uint16_t mag = readReg16(0x1B) & 0x0FFF;

    Serial.println("\n========== AS5600 REGISTER DUMP ==========");
    Serial.printf("  ZMCO (Burn Count) : %u / 3  (%s)\n", zmco, zmco == 0 ? "Factory Virgin" : "OTP Burned");
    Serial.printf("  ZPOS (Zero Pos)   : %u (0x%03X)\n", zpos, zpos);
    Serial.printf("  MPOS (Max Pos)    : %u (0x%03X)\n", mpos, mpos);
    Serial.printf("  MANG (Max Angle)  : %u (0x%03X) %s\n", mang, mang, mang == 0 ? "(Full 360 deg)" : "");
    Serial.printf("  CONF              : 0x%04X\n", conf);
    Serial.printf("  STATUS            : 0x%02X [MD=%d, ML=%d, MH=%d]\n",
                  status, (status & 0x20) != 0, (status & 0x10) != 0, (status & 0x08) != 0);
    Serial.printf("  RAW ANGLE         : %u / 4095\n", rawAngle);
    Serial.printf("  ANGLE (Filtered)  : %u / 4095\n", angle);
    Serial.printf("  AGC Gain          : %u / 255\n", agc);
    Serial.printf("  Magnitude         : %u / 4095\n", mag);
    Serial.println("==========================================\n");
  }

private:
  uint16_t _last = 0;
  uint8_t _consecutiveErrors = 0;
  uint32_t _lastBusCheckMs = 0;
};

Tb6612PwmActuator g_actuator;
As5600Encoder g_encoder;

}  // namespace

IPwmActuator& pumpActuator() { return g_actuator; }
IRotaryEncoder& pumpEncoder() { return g_encoder; }
