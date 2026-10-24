#pragma once
/**
 * @file pump_bus.h
 * @brief Device-boundary interface for the peristaltic dosing pump (MOSFET PWM + AS5600).
 *
 * The dosing loop talks to the pump only through this seam. The real build binds it to LEDC PWM
 * and the AS5600 I2C encoder; the virtual build binds it to a duty->flow + encoder model.
 * `main.cpp` is identical in both builds.
 */

#include <stdint.h>

class IPwmActuator {
public:
  virtual ~IPwmActuator() = default;
  /** Initialize the PWM stage (idempotent). */
  virtual void init() = 0;
  /** Set duty in percent [0, 100]. */
  virtual void setDutyPercent(float percent) = 0;
  /** Last commanded duty (%). */
  virtual float dutyPercent() const = 0;
};

class IRotaryEncoder {
public:
  virtual ~IRotaryEncoder() = default;
  /** Initialize the encoder bus (idempotent). */
  virtual void init() = 0;
  /** Raw 12-bit angle (0..4095). */
  virtual uint16_t readRawAngle() = 0;
  /** AS5600 magnetic status register (0x0B). */
  virtual uint8_t readStatus() { return 0; }
  /** AS5600 AGC register (0x1A, 0..255). */
  virtual uint8_t readAgc() { return 0; }
  /** AS5600 magnitude register (0x1B..0x1C). */
  virtual uint16_t readMagnitude() { return 0; }
  /** Restore AS5600 registers to factory default state (full 360 deg, 0-offset, default CONF). */
  virtual bool resetFactoryDefaults() { return false; }
  /** Write 16-bit CONF register (0x07..0x08). */
  virtual bool setConfig(uint16_t conf) { (void)conf; return false; }
  /** Read 16-bit CONF register (0x07..0x08). */
  virtual uint16_t readConfig() { return 0; }
  /** Dump all AS5600 configuration and status registers. */
  virtual void dumpRegisters() {}
  /** Advance the model (virtual only; no-op on hardware). */
  virtual void tick(uint32_t nowMs) {}
};

/** Compile-time factories. Each build provides exactly one definition of each. */
IPwmActuator& pumpActuator();
IRotaryEncoder& pumpEncoder();
