#include "sensor_sim.h"

#include <math.h>

namespace {
// DIGISENS sentinel error codes emitted as the float value.
constexpr float kErr9999 = 9999.0f;
constexpr float kErr9998 = 9998.0f;
constexpr float kErr9996 = 9996.0f;
}  // namespace

OdSim::OdSim(const OdSimSpec& spec) : _spec(spec) {
  reset();
}

void OdSim::reset() {
  _powered = false;
  _curTemp = _spec.tempC;
  _curDo = _spec.doMgL;
  _curSat = _spec.satPct;
  _lastMs = 0;
  _fault = OdSimFault::None;
  _faultFired = 0;
}

void OdSim::addFaultEvent(const OdSimFaultEvent& ev) {
  if (_faultCount < kMaxFaults) {
    _faults[_faultCount++] = ev;
  }
}

void OdSim::tick(uint32_t nowMs) {
  // Data-driven faults fire once at their timestamp.
  for (uint8_t i = 0; i < _faultCount; ++i) {
    const bool fired = (_faultFired & (1u << i)) != 0;
    if (!fired && nowMs >= _faults[i].atMs) {
      _faultFired |= (1u << i);
      _fault = _faults[i].kind;
    }
  }

  if (_spec.periodMs <= 0.0f) return;
  const float phase = (2.0f * 3.14159265f) * ((float)(nowMs % (uint32_t)_spec.periodMs) / _spec.periodMs);
  _curTemp = _spec.tempC + _spec.tempAmp * cosf(phase);
  _curDo = _spec.doMgL + _spec.doAmp * sinf(phase);
  _curSat = _spec.satPct + _spec.satAmp * sinf(phase);
}

bool OdSim::read(uint32_t /*nowMs*/, int16_t& tempCenti, int16_t& satDeciPct, int16_t& doMilliMgL,
                 int16_t& p3Centi, int16_t& p4Centi) {
  // A dead bus yields no response.
  if (_fault == OdSimFault::NoResponse) {
    return false;
  }

  float temp = _curTemp;
  float doVal = _curDo;
  float sat = _curSat;

  if (_fault == OdSimFault::DigisensError) {
    // Emit the documented error sentinels: temp reports the code, DO/Sat follow.
    temp = kErr9999;
    doVal = kErr9999;
    sat = kErr9999;
  } else if (_fault == OdSimFault::Implausible) {
    temp = -999.0f;   // outside [-40, 125]
    doVal = 9000.0f;  // outside [0, 500]
    sat = 9000.0f;
  }

  // Node decoding/scaling: centi-degC, deci-%, milli-mg/L, centi.
  tempCenti = (int16_t)lroundf(temp * 100.0f);
  satDeciPct = (int16_t)lroundf(sat * 10.0f);
  doMilliMgL = (int16_t)lroundf(doVal * 1000.0f);
  p3Centi = (int16_t)lroundf(doVal * 100.0f);  // ppm-ish
  p4Centi = 500;                               // arbitrary stable placeholder
  return true;
}
