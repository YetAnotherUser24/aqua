#pragma once
/**
 * @file sensor_sim.h
 * @brief Declarative model of the Aqualabo OPTOD + battery ADC. Host-testable (no Arduino).
 *
 * The simulator models the *sensor*, not the node: a small register bank with the OPTOD's
 * semantics (trigger, sampling delay, 5 IEEE-754 floats, 9999/9998/9996 error codes) so the
 * node's real decoding path is exercised unchanged.
 *
 * Faults are data-driven: a schedule of `(atMs, kind)` entries switches the device into a
 * failure mode at a given time.
 */

#include <stdint.h>

enum class OdSimFault : uint8_t {
  None = 0,
  NoResponse,     // bus dead: the read times out (node -> status bit 22)
  DigisensError,  // sensor returns 9999/9998/9996 sentinels
  Implausible,    // out-of-range float (node rejects it)
};

/** Fault schedule entry. */
struct OdSimFaultEvent {
  uint32_t atMs;
  OdSimFault kind;
};

/** Declarative description of the simulated sensor + battery. */
struct OdSimSpec {
  float tempC = 24.5f;        // Base water temperature (degC)
  float doMgL = 7.8f;         // Base dissolved oxygen (mg/L)
  float satPct = 95.0f;       // Base oxygen saturation (%)
  float tempAmp = 0.7f;       // Diurnal-ish amplitude applied via tick()
  float doAmp = 0.8f;
  float satAmp = 6.0f;
  float periodMs = 60000.0f;  // Slow oscillation period
  uint16_t samplingDelayMs = 250;  // Value reported by register 0x00A4
  uint32_t batteryMv = 4120;       // Nominal battery voltage (mV)
};

class OdSim {
public:
  explicit OdSim(const OdSimSpec& spec = OdSimSpec());

  void reset();
  void setSpec(const OdSimSpec& spec) { _spec = spec; }

  /** Advance the slow oscillation and fire any due fault events. */
  void tick(uint32_t nowMs);

  /** Power gating (mirrors the node's rail control; a no-op for the model state). */
  void powerOn() { _powered = true; }
  void powerOff() { _powered = false; }
  bool powered() const { return _powered; }

  /**
   * Simulated Modbus transaction. Returns the 5 decoded floats in node units.
   * @return true on success, false on NoResponse.
   */
  bool read(uint32_t nowMs, int16_t& tempCenti, int16_t& satDeciPct, int16_t& doMilliMgL,
            int16_t& p3Centi, int16_t& p4Centi);

  uint32_t batteryMilliVolts() const { return _spec.batteryMv; }

  float currentDoMgL() const { return _curDo; }
  float currentTempC() const { return _curTemp; }
  OdSimFault currentFault() const { return _fault; }

  void addFaultEvent(const OdSimFaultEvent& ev);
  void clearFaultSchedule() { _faultCount = 0; }
  void setFault(OdSimFault kind) { _fault = kind; }

  static constexpr uint16_t kSamplingDelayReg = 0x00A4;

private:
  OdSimSpec _spec;
  bool _powered = false;
  float _curTemp = 24.5f;
  float _curDo = 7.8f;
  float _curSat = 95.0f;
  uint32_t _lastMs = 0;
  OdSimFault _fault = OdSimFault::None;
  uint32_t _faultFired = 0;

  static constexpr uint8_t kMaxFaults = 8;
  OdSimFaultEvent _faults[kMaxFaults] = {};
  uint8_t _faultCount = 0;
};
