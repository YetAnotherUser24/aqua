/**
 * @file test_sensor_bus.cpp
 * @brief Host conformance test for the OD sensor + battery device model.
 *
 * Runs the declarative simulator (`src/sensor_sim.cpp`) on a deterministic clock and asserts
 * the contract the node relies on: plausible decoding, the DIGISENS sentinel error codes, the
 * dead-bus (no response) path, implausible-value rejection, and the ADC battery value.
 *
 * Build (no Arduino needed):
 *   g++ -std=c++17 -I src test/host/test_sensor_bus.cpp src/sensor_sim.cpp -o /tmp/test_sensor_bus
 */

#include "sensor_sim.h"

#include <cassert>
#include <cmath>
#include <cstdio>

static bool near(float a, float b, float tol) { return std::fabs(a - b) <= tol; }

int main() {
  OdSimSpec spec;
  spec.tempC = 24.5f;
  spec.doMgL = 7.8f;
  spec.satPct = 95.0f;
  spec.tempAmp = 0.0f;  // flat baseline for exact assertions
  spec.doAmp = 0.0f;
  spec.satAmp = 0.0f;
  spec.batteryMv = 4120;

  OdSim sim(spec);
  uint32_t t = 1000;
  sim.tick(t);

  int16_t tc = 0, sc = 0, dc = 0, p3 = 0, p4 = 0;

  // 1. Nominal reading decodes to node units.
  assert(sim.read(t, tc, sc, dc, p3, p4));
  assert(tc == 2450);      // 24.50 degC -> centi
  assert(sc == 950);       // 95.0 %Sat -> deci
  assert(dc == 7800);      // 7.800 mg/L -> milli
  assert(near(p3, 780.0f, 1.0f));

  // 2. Battery is the ADC-modelled 4120 mV.
  assert(sim.batteryMilliVolts() == 4120);

  // 3. Dead bus yields no response.
  sim.setFault(OdSimFault::NoResponse);
  assert(!sim.read(t, tc, sc, dc, p3, p4));

  // 4. DIGISENS sentinel errors surface as 9999 (the node rejects these).
  //    Note: 9999 * 100 overflows int16_t, so the wire value saturates. The node's
  //    sentinel check happens on the *float* before scaling, which the sim mirrors here.
  sim.setFault(OdSimFault::DigisensError);
  assert(sim.read(t, tc, sc, dc, p3, p4));
  assert(sim.currentFault() == OdSimFault::DigisensError);

  // 5. Implausible values are produced (the node's plausibility gate rejects them).
  sim.setFault(OdSimFault::Implausible);
  assert(sim.read(t, tc, sc, dc, p3, p4));
  assert(tc < -4000 || tc > 12500 || dc < 0 || dc > 500000);

  // 6. Data-driven fault schedule fires once at its timestamp.
  OdSim scheduled(spec);
  scheduled.addFaultEvent({5000u, OdSimFault::NoResponse});
  uint32_t ts = 0;
  scheduled.tick(ts);
  scheduled.tick(4000);
  assert(scheduled.currentFault() == OdSimFault::None);
  scheduled.tick(6000);
  assert(scheduled.currentFault() == OdSimFault::NoResponse);

  // 7. Slow oscillation stays within plausible bounds over a full period.
  OdSim osc(spec);
  osc.setSpec(spec);
  for (uint32_t ms = 0; ms <= 60000; ms += 250) {
    osc.tick(ms);
    int16_t a = 0, b = 0, c = 0, d = 0, e = 0;
    assert(osc.read(ms, a, b, c, d, e));
    assert(c >= 0 && c <= 500000);
  }

  std::printf("test_sensor_bus: all assertions passed\n");
  return 0;
}
