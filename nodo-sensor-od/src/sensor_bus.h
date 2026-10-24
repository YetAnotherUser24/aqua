#pragma once
/**
 * @file sensor_bus.h
 * @brief Device-boundary interface for the OD-Logger's sensors.
 *
 * The node's measurement pipeline talks to the sensor only through this seam. The real build
 * binds it to the OPTOD Modbus driver + ADC battery monitor; the virtual build binds it to a
 * simulated sensor (register bank + ADC). `sensor_engine.cpp` is identical in both builds.
 *
 * Battery is **ADC-only** on this platform: there is deliberately no INA226 implementation here.
 */

#include <stdint.h>

/**
 * @brief Decoded OPTOD measurement in the units the node stores.
 * Values are already scaled: temp centi-degC, sat deci-%, DO milli-mg/L, p3/p4 centi.
 */
struct OdReading {
  int16_t tempCenti = 0;
  int16_t satDeciPct = 0;
  int16_t doMilliMgL = 0;
  int16_t p3Centi = 0;
  int16_t p4Centi = 0;
  bool valid = false;
};

class IOdSensor {
public:
  virtual ~IOdSensor() = default;

  /** Power the bus/sensor rails if needed (idempotent). */
  virtual void powerOn() = 0;
  /** De-energize the bus/sensor rails if allowed (idempotent). */
  virtual void powerOff() = 0;

  /** Trigger one measurement and read it back. Returns true when `out.valid`. */
  virtual bool read(OdReading& out, uint32_t nowMs) = 0;

  /** Advance the device model (virtual only; a no-op on hardware). */
  virtual void tick(uint32_t nowMs) {}
};

class IBatteryMonitor {
public:
  virtual ~IBatteryMonitor() = default;
  /** Battery voltage in millivolts. */
  virtual uint32_t readMilliVolts() = 0;
  /** Release any hardware resources (no-op on virtual). */
  virtual void powerDown() {}
};

/** Compile-time factories. Each build provides exactly one definition of each. */
IOdSensor& odSensor();
IBatteryMonitor& batteryMonitor();
