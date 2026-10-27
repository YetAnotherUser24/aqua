#include "control_engine.h"
#include "config.h"

ControlEngine::ControlEngine() {
  _config.target_do_mg_l = DEFAULT_TARGET_DO_MG_L;
  _config.kp = DEFAULT_KP;
  _config.ki = DEFAULT_KI;
  _config.kd = DEFAULT_KD;
  _config.min_rpm = MIN_MOTOR_RPM;
  _config.max_rpm = MAX_MOTOR_RPM;
  _config.failsafe_rpm = FAILSAFE_MOTOR_RPM;
  _config.manual_throttle_pct = 0.0f;
#if defined(AERADOR_VIRTUAL) && AERADOR_VIRTUAL
  _config.mode = MODE_MANUAL;
#else
  _config.mode = MODE_PID;
#endif
}

void ControlEngine::configure(const ControlConfig& config) {
  portENTER_CRITICAL(&_mux);
  if (_config.mode != config.mode) {
    _integral = 0.0f;
    _lastSampleMs = 0;
  }
  _config = config;
  if (_config.max_rpm < _config.min_rpm) _config.max_rpm = _config.min_rpm + 500.0f;
  _config.manual_throttle_pct = constrain(_config.manual_throttle_pct, 0.0f, 100.0f);
  portEXIT_CRITICAL(&_mux);
}

ControlConfig ControlEngine::getConfig() {
  ControlConfig copy;
  portENTER_CRITICAL(&_mux);
  copy = _config;
  portEXIT_CRITICAL(&_mux);
  return copy;
}

void ControlEngine::setManualThrottle(float throttlePct) {
  portENTER_CRITICAL(&_mux);
  _config.mode = MODE_MANUAL;
  _config.manual_throttle_pct = constrain(throttlePct, 0.0f, 100.0f);
  _emergencyStopActive = false;
  portEXIT_CRITICAL(&_mux);
}

void ControlEngine::triggerEmergencyStop() {
  portENTER_CRITICAL(&_mux);
  _config.mode = MODE_MANUAL;
  _config.manual_throttle_pct = 0.0f;
  _emergencyStopActive = true;
  portEXIT_CRITICAL(&_mux);
}

bool ControlEngine::isEmergencyStopActive() {
  bool active;
  portENTER_CRITICAL(&_mux);
  active = _emergencyStopActive;
  portEXIT_CRITICAL(&_mux);
  return active;
}

void ControlEngine::clearEmergencyStop() {
  portENTER_CRITICAL(&_mux);
  _emergencyStopActive = false;
  portEXIT_CRITICAL(&_mux);
}

void ControlEngine::setExperimentLogging(bool active) {
  portENTER_CRITICAL(&_mux);
  _experimentLogging = active;
  portEXIT_CRITICAL(&_mux);
}

bool ControlEngine::isExperimentLogging() {
  bool active;
  portENTER_CRITICAL(&_mux);
  active = _experimentLogging;
  portEXIT_CRITICAL(&_mux);
  return active;
}

void ControlEngine::setArmed(bool armed) {
  portENTER_CRITICAL(&_mux);
  _isArmed = armed;
  if (!armed) {
    _integral = 0.0f;
    _lastCommandedRpm = 0.0f;
    _lastSampleMs = 0;
  }
  portEXIT_CRITICAL(&_mux);
}

bool ControlEngine::isArmed() {
  bool armed;
  portENTER_CRITICAL(&_mux);
  armed = _isArmed;
  portEXIT_CRITICAL(&_mux);
  return armed;
}

void ControlEngine::updateProcessVariable(float measuredDoMgL, float waterTempC) {
  portENTER_CRITICAL(&_mux);
  _lastMeasuredDo = measuredDoMgL;
  _lastWaterTemp = waterTempC;
  _lastPacketTimeMs = millis();
  _hasFirstSample = true;
  _failsafeActive = false;
  _newSampleAvailable = true;
  portEXIT_CRITICAL(&_mux);
}

float ControlEngine::getLastMeasuredDo() {
  float val;
  portENTER_CRITICAL(&_mux);
  val = _lastMeasuredDo;
  portEXIT_CRITICAL(&_mux);
  return val;
}

float ControlEngine::getLastWaterTemp() {
  float val;
  portENTER_CRITICAL(&_mux);
  val = _lastWaterTemp;
  portEXIT_CRITICAL(&_mux);
  return val;
}

bool ControlEngine::isFailsafeActive() {
  bool active;
  portENTER_CRITICAL(&_mux);
  active = _failsafeActive;
  portEXIT_CRITICAL(&_mux);
  return active;
}

unsigned long ControlEngine::getLastPacketAgeMs(unsigned long nowMs) {
  unsigned long age = 0xFFFFFFFFUL;
  portENTER_CRITICAL(&_mux);
  if (_hasFirstSample) {
    age = (nowMs >= _lastPacketTimeMs) ? (nowMs - _lastPacketTimeMs) : 0;
  }
  portEXIT_CRITICAL(&_mux);
  return age;
}

float ControlEngine::computeOutputRpm(unsigned long nowMs) {
  // 1. Thread-safe snapshot of state
  ControlConfig cfg;
  float measuredDo;
  bool hasSample;
  unsigned long lastPacketMs;
  bool eStop;
  bool isNewSample;

  portENTER_CRITICAL(&_mux);
  cfg = _config;
  measuredDo = _lastMeasuredDo;
  hasSample = _hasFirstSample;
  lastPacketMs = _lastPacketTimeMs;
  eStop = _emergencyStopActive;
  isNewSample = _newSampleAvailable;
  _newSampleAvailable = false;
  portEXIT_CRITICAL(&_mux);

  // 2. Emergency stop has top priority
  if (eStop) {
    _lastCommandedRpm = 0.0f;
    return 0.0f;
  }

  // 3. Manual override
  if (cfg.mode == MODE_MANUAL) {
    portENTER_CRITICAL(&_mux);
    _failsafeActive = false;
    portEXIT_CRITICAL(&_mux);
    float throttle = cfg.manual_throttle_pct / 100.0f;
    _lastCommandedRpm = constrain(throttle * cfg.max_rpm, cfg.min_rpm, cfg.max_rpm);
    return _lastCommandedRpm;
  }

  // 4. Safe startup: keep motor at 0 RPM until first valid sensor packet arrives
  if (!hasSample) {
    portENTER_CRITICAL(&_mux);
    _failsafeActive = true;
    portEXIT_CRITICAL(&_mux);
    _lastCommandedRpm = 0.0f;
    return 0.0f;
  }

  // 5. Watchdog if sensor link drops out during operation (> 35s)
  if (nowMs - lastPacketMs > FAILSAFE_TIMEOUT_MS) {
    portENTER_CRITICAL(&_mux);
    _failsafeActive = true;
    portEXIT_CRITICAL(&_mux);
    _lastCommandedRpm = cfg.failsafe_rpm;
    return cfg.failsafe_rpm;
  }

  portENTER_CRITICAL(&_mux);
  _failsafeActive = false;
  portEXIT_CRITICAL(&_mux);

  // 6. Timing and discrete calculation
  if (_lastSampleMs == 0) {
    _lastSampleMs = nowMs;
    _prevDoSample = measuredDo;
    _prevError = cfg.target_do_mg_l - measuredDo;
    if (cfg.mode == MODE_PID) {
      _lastCommandedRpm = runPid(measuredDo, 5.0f, cfg);
    } else if (cfg.mode == MODE_FUZZY) {
      _lastCommandedRpm = runFuzzy(_prevError, 0.0f, cfg);
    }
    return constrain(_lastCommandedRpm, cfg.min_rpm, cfg.max_rpm);
  }

  // Execute control update on new sample or if outer sampling period elapsed (Ts >= 5.0s)
  if (isNewSample || (nowMs - _lastSampleMs >= 5000UL)) {
    float dt = (nowMs - _lastSampleMs) / 1000.0f;
    _lastSampleMs = nowMs;
    if (dt <= 0.001f || dt > 15.0f) {
      dt = 5.0f;
    }

    if (cfg.mode == MODE_PID) {
      _lastCommandedRpm = runPid(measuredDo, dt, cfg);
    } else if (cfg.mode == MODE_FUZZY) {
      float error = cfg.target_do_mg_l - measuredDo;
      float dError = (error - _prevError) / dt;
      _lastCommandedRpm = runFuzzy(error, dError, cfg);
    }

    _prevDoSample = measuredDo;
    _prevError = cfg.target_do_mg_l - measuredDo;
  }

  // Zero-order hold (ZOH) between samples
  return constrain(_lastCommandedRpm, cfg.min_rpm, cfg.max_rpm);
}

/**
 * 2-DOF PI con Anti-Windup por Back-Calculation y Frenado Predictivo (Script ganador).
 *
 * Ecuaciones discretas (Ts = 5.0 s):
 *  1. Predictor: C_pred = C_meas + tau_lag * (dC/dt).
 *     Si C_pred >= SP y dC/dt > 0: frenado a 0 RPM y drenado de integral (prevención sobreimpulso limpio).
 *  2. Consigna ponderada en P: e_P = beta * SP - C_meas (beta = 0.35 para eliminar el proportional kick).
 *  3. Acción proporcional: u_unsat = Kp * e_P + I(k-1).
 *  4. Saturación física: u_sat = sat(u_unsat, min_rpm, max_rpm).
 *  5. Integración con Back-Calculation:
 *     I(k) = I(k-1) + Ki * e * dt + (dt / Tt) * (u_sat - u_unsat)
 */
float ControlEngine::runPid(float measuredDo, float dt, const ControlConfig& cfg) {
  float dC_dt = (dt > 0.001f) ? ((measuredDo - _prevDoSample) / dt) : 0.0f;
  float cPred = measuredDo + cfg.tau_lag * dC_dt;

  // 1. Corte preventivo por inercia química y retardo de transporte
  if (cPred >= cfg.target_do_mg_l && dC_dt > 0.0001f) {
    _integral = fmaxf(0.0f, _integral - 50.0f * dt);
    return 0.0f;
  }

  // 2. Error convencional y error de acción proporcional ponderada (2-DOF)
  float error = cfg.target_do_mg_l - measuredDo;
  float error_p = (cfg.beta * cfg.target_do_mg_l) - measuredDo;

  float u_unsat = (cfg.kp * error_p) + _integral;
  if (cfg.kd > 0.0001f) {
    u_unsat -= cfg.kd * dC_dt; // Derivada sobre la medición (Derivative on PV)
  }

  // 3. Saturación
  float u_sat = constrain(u_unsat, cfg.min_rpm, cfg.max_rpm);

  // 4. Actualización integral con Back-Calculation anti-windup
  float tt = (cfg.tt > 0.1f) ? cfg.tt : 100.0f;
  _integral += (cfg.ki * error * dt) + ((u_sat - u_unsat) * (dt / tt));
  _integral = constrain(_integral, -2.0f * cfg.max_rpm, 2.0f * cfg.max_rpm);

  return u_sat;
}

float ControlEngine::runFuzzy(float error, float dError, const ControlConfig& cfg) {
  float u_neg = (error <= 0.0f) ? 1.0f : ((error < 0.5f) ? (0.5f - error) / 0.5f : 0.0f);
  float u_low = (error > 0.0f && error <= 0.8f) ? (error / 0.8f) :
                ((error > 0.8f && error < 1.8f) ? (1.8f - error) / 1.0f : 0.0f);
  float u_med = (error > 0.8f && error <= 1.8f) ? (error - 0.8f) / 1.0f :
                ((error > 1.8f && error < 3.0f) ? (3.0f - error) / 1.2f : 0.0f);
  float u_high = (error >= 3.0f) ? 1.0f : ((error > 1.8f) ? (error - 1.8f) / 1.2f : 0.0f);

  float u_improving = (dError <= -0.1f) ? 1.0f : ((dError < 0.0f) ? (-dError) / 0.1f : 0.0f);
  float u_steady    = (fabsf(dError) <= 0.1f) ? 1.0f - (fabsf(dError) / 0.1f) : 0.0f;
  float u_worsening = (dError >= 0.1f) ? 1.0f : ((dError > 0.0f) ? dError / 0.1f : 0.0f);

  const float RPM_OFF  = cfg.min_rpm;
  const float RPM_LOW  = cfg.min_rpm + (cfg.max_rpm - cfg.min_rpm) * 0.25f;
  const float RPM_MED  = cfg.min_rpm + (cfg.max_rpm - cfg.min_rpm) * 0.55f;
  const float RPM_HIGH = cfg.min_rpm + (cfg.max_rpm - cfg.min_rpm) * 0.80f;
  const float RPM_MAX  = cfg.max_rpm;

  float num = 0.0f;
  float den = 0.0f;

  auto addRule = [&](float weight, float singletonRpm) {
    if (weight > 0.0001f) {
      num += weight * singletonRpm;
      den += weight;
    }
  };

  addRule(u_neg, RPM_OFF);
  addRule(fminf(u_low, u_improving), RPM_OFF);
  addRule(fminf(u_low, u_steady), RPM_LOW);
  addRule(fminf(u_low, u_worsening), RPM_MED);
  addRule(fminf(u_med, u_improving), RPM_LOW);
  addRule(fminf(u_med, u_steady), RPM_MED);
  addRule(fminf(u_med, u_worsening), RPM_HIGH);
  addRule(u_high, RPM_MAX);

  if (den < 0.0001f) return cfg.min_rpm;
  return num / den;
}
