/**
 * @file sensor_bus_virtual.cpp
 * @brief Virtual `IOdSensor` + `IBatteryMonitor`: binds OdSim to the node interface.
 *
 * Build: selected by `-DSENSOR_VIRTUAL` (env `virtual` / `virtual-s3`). Speaks the same
 * semantics as the real OPTOD so `sensor_engine.cpp` decoding runs unchanged.
 */

#include "sensor_bus.h"
#include "sensor_sim.h"
#include "n_logger_config.h"
#include "log.h"

namespace {

class VirtualOdSensor : public IOdSensor {
public:
  VirtualOdSensor() {
    OdSimSpec spec;
    spec.tempC = 24.5f;
    spec.doMgL = 7.8f;
    spec.satPct = 95.0f;
    _sim.setSpec(spec);
  }

  void powerOn() override { _sim.powerOn(); }
  void powerOff() override { _sim.powerOff(); }
  void tick(uint32_t nowMs) override { _sim.tick(nowMs); }

  bool read(OdReading& out, uint32_t nowMs) override {
    _sim.tick(nowMs);
    int16_t t = 0, s = 0, d = 0, p3 = 0, p4 = 0;
    if (!_sim.read(nowMs, t, s, d, p3, p4)) {
      out.valid = false;
      return false;
    }
    out.tempCenti = t;
    out.satDeciPct = s;
    out.doMilliMgL = d;
    out.p3Centi = p3;
    out.p4Centi = p4;
    // Plausibility gate mirrors the real decoder's rejection of out-of-range values.
    out.valid = (t >= -4000 && t <= 12500) && (d >= 0 && d <= 500000);
    if (!out.valid) {
      Logln("[ODSim] implausible measurement rejected");
    }
    return out.valid;
  }

private:
  OdSim _sim;
};

class VirtualBatteryMonitor : public IBatteryMonitor {
public:
  uint32_t readMilliVolts() override { return _sim.batteryMilliVolts(); }
  void powerDown() override {}

private:
  OdSim _sim;
};

VirtualOdSensor g_sensor;
VirtualBatteryMonitor g_battery;

}  // namespace

IOdSensor& odSensor() { return g_sensor; }
IBatteryMonitor& batteryMonitor() { return g_battery; }
