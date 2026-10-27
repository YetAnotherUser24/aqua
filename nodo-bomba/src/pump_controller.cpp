/**
 * @file pump_controller.cpp
 * @brief Discrete 100 Hz PI Speed Controller, Virtual Flow Estimator, and H-Bridge Suck-back.
 */

#include "pump_controller.h"
#include "pump_bus.h"
#include "config.h"

#include <math.h>
#include <Wire.h>
#include <Preferences.h>

namespace {
constexpr float TWO_PI_F = 6.283185307179586f;
constexpr float TICKS_PER_REV = 4096.0f;
constexpr float RAD_S_TO_RPM = 60.0f / TWO_PI_F;
constexpr float RPM_TO_RAD_S = TWO_PI_F / 60.0f;

// Fast Pade approximation of tanh(x) for continuous, chatter-free zero-crossing
static inline float tanh_approx(float x) {
  if (x > 3.0f) return 1.0f;
  if (x < -3.0f) return -1.0f;
  return x * (27.0f + x * x) / (27.0f + 9.0f * x * x);
}
}

PumpController g_pumpCtrl;

PumpController::PumpController() {
  _telem.mode = PUMP_CTRL_STOPPED;
}

void PumpController::loadFromNvs() {
  Preferences prefs;
  if (prefs.begin("pump_ctrl", true)) {
    if (prefs.isKey("kp")) _params.Kp = prefs.getFloat("kp", _params.Kp);
    if (prefs.isKey("ki")) _params.Ki = prefs.getFloat("ki", _params.Ki);
    if (prefs.isKey("kd")) _params.Kd = prefs.getFloat("kd", _params.Kd);
    if (prefs.isKey("ml_rev")) _mlPerRev = prefs.getFloat("ml_rev", _mlPerRev);
    if (prefs.isKey("gear")) _gearRatio = prefs.getFloat("gear", _gearRatio);
    if (prefs.isKey("alpha")) _filterAlpha = prefs.getFloat("alpha", _filterAlpha);
    if (prefs.isKey("retracted")) _meniscusRetracted = prefs.getBool("retracted", _meniscusRetracted);
    prefs.end();
  }
}

void PumpController::saveToNvs() {
  Preferences prefs;
  if (prefs.begin("pump_ctrl", false)) {
    prefs.putFloat("kp", _params.Kp);
    prefs.putFloat("ki", _params.Ki);
    prefs.putFloat("kd", _params.Kd);
    prefs.putFloat("ml_rev", _mlPerRev);
    prefs.putFloat("gear", _gearRatio);
    prefs.putFloat("alpha", _filterAlpha);
    prefs.putBool("retracted", _meniscusRetracted);
    prefs.end();
  }
}

void PumpController::setMeniscusRetracted(bool retracted) {
  if (_meniscusRetracted == retracted) return; // Skip redundant flash write
  _meniscusRetracted = retracted;
  Preferences prefs;
  if (prefs.begin("pump_ctrl", false)) {
    prefs.putBool("retracted", _meniscusRetracted);
    prefs.end();
  }
}

void PumpController::resetNvsDefaults() {
  Preferences prefs;
  if (prefs.begin("pump_ctrl", false)) {
    prefs.clear();
    prefs.end();
  }
  _params.Kp = PUMP_PID_KP;
  _params.Ki = PUMP_PID_KI;
  _params.Kd = 0.0000f;
  _mlPerRev = ML_PER_REV;
  _gearRatio = 1.0f;
  _filterAlpha = 0.08f;
  _meniscusRetracted = true;
}

void PumpController::init(float mlPerRev, uint32_t doseTimeoutMs) {
  _mlPerRev = mlPerRev;
  _doseTimeoutMs = doseTimeoutMs;

  _params.Kp = PUMP_PID_KP;
  _params.Ki = PUMP_PID_KI;
  _params.Kd = 0.0000f; // Pure PI: derivative action causes chattering on discrete AS5600 ticks
  _params.minDuty = PUMP_MIN_DUTY_PERCENT / 100.0f;
  _params.maxSlewRate = PUMP_PID_MAX_SLEW;

  loadFromNvs();

  stop();
}

void PumpController::stop() {
  _testMode = false;
  _telem.mode = PUMP_CTRL_STOPPED;
  _telem.targetRpm = 0.0f;
  _telem.targetRadS = 0.0f;
  _telem.dutyPercent = 0.0f;
  _integral = 0.0f;
  _prevOutput = 0.0f;
  _doseStartMs = 0;
  pumpActuator().setDutyPercent(0.0f);
}

void PumpController::setTargetRadS(float rad_s) {
  if (fabsf(rad_s) > PUMP_MAX_RAD_S) {
    rad_s = (rad_s > 0.0f) ? PUMP_MAX_RAD_S : -PUMP_MAX_RAD_S;
  }

  if (fabsf(rad_s) < 0.05f) {
    stop();
    return;
  }

  // Forward rotation flushes nozzle to the tip
  if (rad_s > 0.05f) {
    setMeniscusRetracted(false);
  }

  _telem.targetRadS = rad_s;
  _telem.targetRpm = rad_s * RAD_S_TO_RPM;
  _telem.mode = PUMP_CTRL_CLOSED_LOOP;
}

void PumpController::setTargetRpm(float rpm) {
  setTargetRadS(rpm * RPM_TO_RAD_S);
}

void PumpController::setOpenLoopDuty(float dutyFraction) {
  dutyFraction = constrain(dutyFraction, -1.0f, 1.0f);
  if (dutyFraction > 0.01f) {
    setMeniscusRetracted(false);
  }
  _telem.mode = PUMP_CTRL_OPEN_LOOP;
  _telem.targetRpm = 0.0f;
  _telem.targetRadS = 0.0f;
  _telem.dutyPercent = dutyFraction * 100.0f;
  _integral = 0.0f;
  _prevOutput = dutyFraction;
  pumpActuator().setDutyPercent(_telem.dutyPercent);
}

void PumpController::startDose(float targetMl, float rad_s) {
  if (targetMl <= 0.0f) return;
  if (fabsf(rad_s) <= 0.05f) rad_s = DOSE_TARGET_RAD_S;
  if (fabsf(rad_s) > PUMP_MAX_RAD_S) rad_s = PUMP_MAX_RAD_S;

  // If meniscus is parked inside nozzle, pre-charge offset so the prime volume is not counted in delivery
  if (_meniscusRetracted && SUCKBACK_REVS > 0.01f) {
    _telem.dosedMl = -(SUCKBACK_REVS * _mlPerRev);
    Serial.printf("[DOSE] Priming meniscus compensation: %.3f mL (%.2f revs) before volume tally\n",
                  -_telem.dosedMl, SUCKBACK_REVS);
  } else {
    _telem.dosedMl = 0.0f;
  }
  setMeniscusRetracted(false); // Nozzle is fluid-filled during forward dosing

  _telem.targetMl = targetMl;
  _telem.doseCompleted = false;
  _telem.doseTimedOut = false;
  _telem.statusCode = 0;
  _telem.targetRadS = rad_s;
  _telem.targetRpm = rad_s * RAD_S_TO_RPM;
  _telem.mode = PUMP_CTRL_DOSE;
  _integral = 0.0f;
  _prevOutput = 0.0f;
  _doseStartMs = millis();
}

void PumpController::triggerSuckback(float revs, float rad_s) {
  if (revs <= 0.001f) {
    stop();
    return;
  }
  _telem.mode = PUMP_CTRL_SUCKBACK;
  _suckbackTargetRevs = revs;
  _suckbackStartRev = _telem.totalRevolutions;
  _telem.targetRadS = -fabsf(rad_s);
  _telem.targetRpm = _telem.targetRadS * RAD_S_TO_RPM;
  _integral = 0.0f;
  _prevOutput = 0.0f;
  Serial.printf("[SUCK-BACK] Retracting %.2f revolutions at %.2f rad/s (%.1f RPM) to pull meniscus 3-5 mm...\n",
                revs, _telem.targetRadS, _telem.targetRpm);
}

void PumpController::tunePid(float kp, float ki, float kd) {
  _params.Kp = kp;
  _params.Ki = ki;
  _params.Kd = kd;
  _integral = 0.0f;
  saveToNvs();
}

void PumpController::setMlPerRev(float mlPerRev) {
  if (mlPerRev > 0.001f) {
    _mlPerRev = mlPerRev;
    saveToNvs();
  }
}

void PumpController::setFilterAlpha(float alpha) {
  _filterAlpha = constrain(alpha, 0.01f, 1.0f);
  saveToNvs();
}

void PumpController::setGearRatio(float ratio) {
  if (ratio > 0.01f) {
    _gearRatio = ratio;
    saveToNvs();
  }
}

void PumpController::setSysidMode(bool enable) {
  _sysidMode = enable;
  if (_sysidMode) {
    Serial.println("SYSID_MODE,Time_ms,Target_Duty,Duty_Pct,RPM,Rad_s,Dosed_mL");
  } else {
    Serial.println("[OK] SysID Mode OFF. Normal telemetry restored.");
  }
}

void PumpController::setDebugStream(bool enable) {
  _debugStream = enable;
  if (_debugStream) {
    Serial.println("[OK] Debug Stream ON (2Hz periodic telemetry active).");
  } else {
    Serial.println("[OK] Debug Stream OFF (Monitor quiet).");
  }
}

void PumpController::updateAngleTracker() {
  uint16_t rawAngle = pumpEncoder().readRawAngle();
  _telem.rawAngle = rawAngle;

  if (!_anglePrimed) {
    _prevRawAngle = rawAngle;
    _anglePrimed = true;
    return;
  }

  int32_t delta = (int32_t)rawAngle - (int32_t)_prevRawAngle;
  // Handle 12-bit wrap around
  if (delta > 2048) delta -= 4096;
  else if (delta < -2048) delta += 4096;

#if PUMP_ENCODER_INVERT
  delta = -delta;
#endif

  _prevRawAngle = rawAngle;
  _accumTicks += delta;
  _telem.totalTicks += delta;

  float revDelta = (float)delta / (float)TICKS_PER_REV;
  _telem.totalRevolutions += revDelta;

  // Track volume delivered forward only during active dose (scaled by gearbox ratio)
  if (revDelta > 0.0f && _telem.mode == PUMP_CTRL_DOSE) {
    float headRevs = revDelta / _gearRatio;
    _telem.dosedMl += headRevs * _mlPerRev;
  }
}

void PumpController::updateVelocity(uint32_t nowMs) {
  (void)nowMs;
  uint32_t nowUs = micros();
  if (_lastVelocityUs == 0) {
    _lastVelocityUs = nowUs;
    return;
  }

  uint32_t elapsedUs = nowUs - _lastVelocityUs;
  if (elapsedUs < 10000) return; // 100 Hz velocity update (aligned with 100 Hz PI loop)
  _lastVelocityUs = nowUs;
  float dt = (float)elapsedUs / 1000000.0f;

  int32_t deltaTicks = _accumTicks;
  _accumTicks = 0;

  float deltaRad = (float)deltaTicks * (TWO_PI_F / (float)TICKS_PER_REV);
  float instRadS = (dt > 0.0f) ? (deltaRad / dt) : 0.0f;

  // 1. Classical low-pass filter directly in canonical SI units (rad/s)
  _filteredRadS = (_filteredRadS * (1.0f - _filterAlpha)) + (instRadS * _filterAlpha);

  // 2. 2nd-order Luenberger State Observer for continuous position, velocity and load torque in SI units
  // Plant model: dTheta/dt = omega; dOmega/dt = (K*u - omega)/tau + d; dd/dt = 0
  constexpr float PLANT_K = PUMP_MAX_RAD_S; // 398.0 rad/s at u=1.0 (12V)
  constexpr float PLANT_TAU = 0.18f;        // 180 ms electrical/mechanical time constant
  constexpr float OBS_L1 = 40.0f;           // Position correction gain (rad/s per rad error)
  constexpr float OBS_L2 = 400.0f;          // Velocity correction gain (rad/s^2 per rad error)
  constexpr float OBS_L3 = 1000.0f;         // Disturbance torque estimation gain (rad/s^3 per rad error)

  float measRad = _telem.totalRevolutions * TWO_PI_F;
  float posErr = measRad - _obsTheta;

  // Observer state integration
  float uEff = _prevOutput;
  float accelModel = (PLANT_K * uEff - _obsOmega) / PLANT_TAU;
  _obsTheta += (_obsOmega + OBS_L1 * posErr) * dt;
  _obsOmega += (accelModel + _obsDist + OBS_L2 * posErr) * dt;
  _obsDist  += (OBS_L3 * posErr) * dt;
  _obsDist = constrain(_obsDist, -3000.0f, 3000.0f); // Bound disturbance estimate (rad/s^2)

  // Report real physical velocity from encoder in rad/s
  if (fabsf(_filteredRadS) < 0.05f) {
    _filteredRadS = 0.0f;
    _obsOmega = 0.0f;
  }

  _telem.actualRadS = _filteredRadS;
  _telem.actualRpm = _filteredRadS * RAD_S_TO_RPM;
  _telem.distTorque = _obsDist;

  // Virtual Flow Sensor: Q (mL/min) = (Head rad/s * 60 / 2pi) * ML_PER_REV
  float headRadS = _telem.actualRadS / _gearRatio;
  float headRpm = headRadS * RAD_S_TO_RPM;
  _telem.flowRateMlMin = headRpm * _mlPerRev;
}

void PumpController::updatePid(uint32_t nowMs) {
  if (_testMode) return;

  if (_lastPidMs == 0) {
    _lastPidMs = nowMs;
    return;
  }

  uint32_t elapsedMs = nowMs - _lastPidMs;
  if (elapsedMs < 10) return; // 100 Hz loop
  _lastPidMs = nowMs;
  float dt = (float)elapsedMs / 1000.0f;

  if (_telem.mode == PUMP_CTRL_STOPPED) {
    pumpActuator().setDutyPercent(0.0f);
    _telem.dutyPercent = 0.0f;
    return;
  }

  if (_telem.mode == PUMP_CTRL_OPEN_LOOP) {
    pumpActuator().setDutyPercent(_telem.dutyPercent);
    return;
  }

  // Dosing completion check
  if (_telem.mode == PUMP_CTRL_DOSE) {
    bool reached = (_telem.targetMl > 0.0f) && (_telem.dosedMl >= _telem.targetMl);
    bool timeout = (_doseStartMs > 0) && ((nowMs - _doseStartMs) >= _doseTimeoutMs);

    if (reached || timeout) {
      _telem.doseCompleted = reached;
      _telem.doseTimedOut = timeout;
      _telem.statusCode = reached ? 0 : 2;
      Serial.printf("\n[DOSE COMPLETE] Volume: %.2f / %.2f mL | Status: %s\n",
                    _telem.dosedMl, _telem.targetMl, reached ? "SUCCESS" : "TIMEOUT");

      // Trigger automatic anti-drip suck-back if configured with H-Bridge
      if (SUCKBACK_REVS > 0.01f) {
        triggerSuckback(SUCKBACK_REVS, SUCKBACK_RAD_S);
      } else {
        stop();
      }
      return;
    }
  }

  // Suck-back completion check (Reverse rotation)
  if (_telem.mode == PUMP_CTRL_SUCKBACK) {
    float revsDone = _suckbackStartRev - _telem.totalRevolutions;
    if (revsDone >= _suckbackTargetRevs) {
      stop();
      setMeniscusRetracted(true);
      Serial.printf("[SUCK-BACK COMPLETE] Meniscus retracted %.2f revs. Motor stopped. (State: RETRACTED)\n", revsDone);
      return;
    }
  }

  // Closed-loop PI execution (direction-aware)
  float errorRadS = _telem.targetRadS - _telem.actualRadS;

  // Proportional
  float pTerm = _params.Kp * errorRadS;

  // Integral with anti-windup clamping
  _integral += _params.Ki * errorRadS * dt;
  _integral = constrain(_integral, _params.integralMin, _params.integralMax);

  // Pure PI: Derivative term removed (prevents noise amplification and roller ripple)
  float dTerm = 0.0f;
  if (_params.Kd > 0.0001f && dt > 0.0f) {
    dTerm = -_params.Kd * ((_telem.actualRadS - _prevRadS) / dt);
  }
  _prevRadS = _telem.actualRadS;

  // Raw control effort [-1.0, 1.0]
  float uRaw = pTerm + _integral + dTerm;

  // Stiction feedforward with smooth continuous hyperbolic tangent deadband compensation
  // Eliminates deadband chatter, high-current spikes at zero-crossing, and limit cycling
  float uEff = 0.0f;
  if (fabsf(_telem.targetRadS) > 0.05f) {
    float deadband = _params.minDuty;
    float signU = tanh_approx(uRaw / 0.03f); // Smooth C^inf transition across [-0.03, 0.03]
    uEff = (signU * deadband) + uRaw * (_params.maxDuty - deadband);
  } else {
    uEff = 0.0f;
    _integral = 0.0f;
  }
  uEff = constrain(uEff, -_params.maxDuty, _params.maxDuty);

  // Slew rate limiter (protects motor gearbox, H-bridge and 12V rail)
  float maxDelta = _params.maxSlewRate * dt;
  float delta = constrain(uEff - _prevOutput, -maxDelta, maxDelta);
  float uSlewed = _prevOutput + delta;
  _prevOutput = uSlewed;

  _telem.dutyPercent = uSlewed * 100.0f;
  pumpActuator().setDutyPercent(_telem.dutyPercent);
}

void PumpController::update(uint32_t nowMs) {
  // Let the encoder bus step if simulated
  pumpEncoder().tick(nowMs);

  updateAngleTracker();
  updateVelocity(nowMs);
  updatePid(nowMs);

  // Periodic Telemetry / SysID stream
  uint32_t teleInterval = _sysidMode ? 20 : 500; // 50 Hz for SysID, 2 Hz for CLI
  if (nowMs - _lastTelemetryMs >= teleInterval) {
    _lastTelemetryMs = nowMs;

    if (_sysidMode) {
      Serial.printf("SYSID,%lu,%.4f,%.2f,%.2f,%.4f,%.3f\n",
                    nowMs,
                    _prevOutput,
                    _telem.dutyPercent,
                    _telem.actualRpm,
                    _telem.actualRadS,
                    _telem.dosedMl);
    } else if (_debugStream) {
      const char* modeStr =
          (_telem.mode == PUMP_CTRL_STOPPED) ? "STOPPED" :
          (_telem.mode == PUMP_CTRL_OPEN_LOOP) ? "OPEN_LOOP" :
          (_telem.mode == PUMP_CTRL_CLOSED_LOOP) ? "SPEED_PI" :
          (_telem.mode == PUMP_CTRL_SUCKBACK) ? "SUCK_BACK" : "DOSING";

      Serial.printf("[TELEMETRY] Measured: %6.2f rad/s (%5.1f RPM) | Target: %6.2f rad/s (%5.1f RPM) | Err: %+5.2f rad/s | Duty: %+5.1f%% | Flow: %5.2f mL/m | Vol: %5.2f / %5.2f mL\n",
                    _telem.actualRadS,
                    _telem.actualRpm,
                    _telem.targetRadS,
                    _telem.targetRpm,
                    _telem.targetRadS - _telem.actualRadS,
                    _telem.dutyPercent,
                    _telem.flowRateMlMin,
                    _telem.dosedMl,
                    _telem.targetMl);
    }
  }
}

void PumpController::printHelp() {
  Serial.println("\n--- AquaControl: Peristaltic Pump Controller CLI (TB6612FNG Driver) ---");
  Serial.println("  SPEED <rad/s>       : Closed-loop angular velocity (e.g. 'SPEED 6.28')");
  Serial.println("  RPM <rpm>           : Closed-loop speed in mechanical RPM (e.g. 'RPM 60')");
  Serial.println("  <number>            : Shorthand: typing a raw number sets closed-loop RPM directly");
  Serial.println("  DUTY <-1.0 - 1.0>   : Open-loop throttle override (e.g. 'DUTY 0.25' or 'DUTY 25%')");
  Serial.println("  DOSE <ml> [rpm]     : Dose volume (mL) with short-brake and auto suck-back");
  Serial.println("  SUCKBACK [rev] [rpm]: Trigger anti-drip meniscus retraction (default 0.35 rev)");
  Serial.println("  REVERSE [rpm]       : Spin pump in reverse (drain line back to bottle)");
  Serial.println("  STOP                : Safely stop the pump (engages active Short Brake)");
  Serial.println("  TUNE <Kp> <Ki> [Kd] : Live update PI gains in SI units [duty/(rad/s)]");
  Serial.println("  CALIB <ml_per_rev>  : Set mL per revolution calibration factor");
  Serial.println("  MENISCUS [RETRACT|FLUSH]: Query or override nozzle meniscus state in NVS");
  Serial.println("  SYSID ON / OFF      : Toggle 50 Hz CSV streaming for system identification");
  Serial.println("  DEBUG ON / OFF      : Toggle periodic 2 Hz telemetry stream in serial monitor");
  Serial.println("  STATUS              : Print full diagnostic snapshot");
  Serial.println("  DIAG                : Check AS5600 magnet health, AGC gain, and raw angle stream");
  Serial.println("  DUMP                : Dump all internal AS5600 registers");
  Serial.println("  RESET_SENSOR        : Restore AS5600 registers to factory default");
  Serial.println("  FILTER <FAST|BALANCED|SMOOTH|alpha> : Set velocity filter responsiveness");
  Serial.println("  GEAR <ratio>        : Set reduction ratio (e.g. 'GEAR 30.0' for 1:30 gearbox)");
  Serial.println("  HELP                : Display this menu");
  Serial.println("--------------------------------------------------------------------\n");
}

void PumpController::printStatus() {
  uint8_t st = pumpEncoder().readStatus();
  uint8_t agc = pumpEncoder().readAgc();
  uint16_t mag = pumpEncoder().readMagnitude();
  bool md = (st & 0x20) != 0;
  bool ml = (st & 0x10) != 0;
  bool mh = (st & 0x08) != 0;
  float headRadS = _telem.actualRadS / _gearRatio;
  float headRpm = _telem.actualRpm / _gearRatio;

  Serial.println("\n========== PERISTALTIC PUMP STATUS ==========");
  Serial.printf("  Mode:            %s\n",
                (_telem.mode == PUMP_CTRL_STOPPED) ? "STOPPED" :
                (_telem.mode == PUMP_CTRL_OPEN_LOOP) ? "OPEN_LOOP_DUTY" :
                (_telem.mode == PUMP_CTRL_CLOSED_LOOP) ? "CLOSED_LOOP_SPEED_PI" :
                (_telem.mode == PUMP_CTRL_SUCKBACK) ? "SUCK_BACK_ACTIVE" : "DOSING");
  Serial.printf("  Target Speed:    %6.2f rad/s (%6.1f RPM)\n", _telem.targetRadS, _telem.targetRpm);
  Serial.printf("  Measured Speed:  %6.2f rad/s (%6.1f RPM)\n", _telem.actualRadS, _telem.actualRpm);
  Serial.printf("  Velocity Error:  %+6.2f rad/s\n", _telem.targetRadS - _telem.actualRadS);
  if (_gearRatio > 1.01f) {
    Serial.printf("  Head Speed:      %6.2f rad/s (%5.2f RPM) [Gearbox 1:%.1f]\n", headRadS, headRpm, _gearRatio);
  }
  Serial.printf("  Filter Alpha:    %.2f (Step settling ~%.1f ms)\n",
                _filterAlpha, (1.0f / (_filterAlpha * 500.0f)) * 1000.0f);
  Serial.printf("  Virtual Flow:    %6.2f mL/min\n", _telem.flowRateMlMin);
  Serial.printf("  Volume Dosed:    %6.2f / %6.2f mL\n", _telem.dosedMl, _telem.targetMl);
  Serial.printf("  Total Revs:      %6.2f rev (%lu ticks)\n", _telem.totalRevolutions, _telem.totalTicks);
  Serial.printf("  Encoder Angle:   %4u / 4095 (12-bit AS5600)\n", _telem.rawAngle);
  Serial.printf("  Magnet Health:   Status=0x%02X [MD=%d, ML=%d, MH=%d] | AGC=%u/255 | Mag=%u\n",
                st, md, ml, mh, agc, mag);
  Serial.printf("  Duty Output:     %+5.1f%%\n", _telem.dutyPercent);
  Serial.printf("  Driver Hardware: TB6612FNG [STBY=%d, AIN2=%d, AIN1=%d, PWMA=%d] @ 12V\n",
                TB6612_STBY_PIN, TB6612_AIN2_PIN, TB6612_AIN1_PIN, TB6612_PWMA_PIN);
  Serial.printf("  Calibration:     %.4f mL/rev\n", _mlPerRev);
  Serial.printf("  Meniscus State:  %s [Auto-offset: %.3f mL (%.2f revs)]\n",
                _meniscusRetracted ? "RETRACTED (Dry Nozzle, Compensating)" : "FLUSH (At Tip, Ready)",
                SUCKBACK_REVS * _mlPerRev, SUCKBACK_REVS);
  Serial.printf("  PI Parameters:   Kp=%.6f, Ki=%.6f, Kd=%.6f, Int=%.4f [Persisted in Flash NVS]\n",
                _params.Kp, _params.Ki, _params.Kd, _integral);
  Serial.println("=============================================\n");
}

void PumpController::handleCliCommand(char* cmd) {
  while (*cmd == ' ') cmd++;
  if (strlen(cmd) == 0) return;

  if (strncasecmp(cmd, "STOP", 4) == 0) {
    stop();
    Serial.println("[OK] Pump STOPPED (Active Short Brake).");
  }
  else if (strncasecmp(cmd, "SPEED", 5) == 0) {
    float val = atof(cmd + 5);
    setTargetRadS(val);
    Serial.printf("[OK] Set Closed-Loop Target: %.2f rad/s (%.1f RPM)\n", _telem.targetRadS, _telem.targetRpm);
  }
  else if (strncasecmp(cmd, "RPM", 3) == 0) {
    float val = atof(cmd + 3);
    setTargetRpm(val);
    Serial.printf("[OK] Set Closed-Loop Target: %.1f RPM (%.2f rad/s)\n", _telem.targetRpm, _telem.targetRadS);
  }
  else if (strncasecmp(cmd, "REVERSE", 7) == 0) {
    float val = atof(cmd + 7);
    if (val <= 0.0f) val = SUCKBACK_RPM;
    float rad_s = (val > 25.0f) ? (val * RPM_TO_RAD_S) : val;
    setTargetRadS(-rad_s);
    Serial.printf("[OK] Reversing at %.1f RPM (%.2f rad/s) (Drain / Reverse Flow)\n", -_telem.targetRpm, -_telem.targetRadS);
  }
  else if (strncasecmp(cmd, "SUCKBACK", 8) == 0 || strncasecmp(cmd, "RETRACT", 7) == 0) {
    float revs = SUCKBACK_REVS;
    float spd = SUCKBACK_RPM;
    char* arg = cmd + ((strncasecmp(cmd, "SUCKBACK", 8) == 0) ? 8 : 7);
    int matched = sscanf(arg, "%f %f", &revs, &spd);
    if (revs <= 0.0f) revs = SUCKBACK_REVS;
    if (matched < 2 || spd <= 0.0f) spd = SUCKBACK_RPM;
    float rad_s = (spd > 25.0f) ? (spd * RPM_TO_RAD_S) : spd;
    triggerSuckback(revs, rad_s);
  }
  else if (strncasecmp(cmd, "DUTY", 4) == 0) {
    float val = atof(cmd + 4);
    if (fabsf(val) > 1.0f) {
      val = val / 100.0f; // Accept both 0.35 and 35.0 as 35%
    }
    setOpenLoopDuty(val);
    Serial.printf("[OK] Open-Loop Throttle: %+.1f%%\n", _telem.dutyPercent);
  }
  else if (strncasecmp(cmd, "DOSE", 4) == 0) {
    float ml = 0.0f;
    float spd = DOSE_TARGET_RPM;
    int matched = sscanf(cmd + 4, "%f %f", &ml, &spd);
    if (matched >= 1 && ml > 0.0f) {
      float rad_s;
      if (matched < 2 || spd <= 0.0f) {
        rad_s = DOSE_TARGET_RAD_S;
      } else if (spd > 25.0f) {
        rad_s = spd * RPM_TO_RAD_S;
      } else {
        rad_s = spd;
      }
      startDose(ml, rad_s);
      Serial.printf("[OK] Starting Dose of %.2f mL at %.1f RPM (%.2f rad/s) with auto suck-back\n",
                    ml, _telem.targetRpm, _telem.targetRadS);
    } else {
      Serial.println("[ERROR] Usage: DOSE <target_ml> [target_rpm]");
    }
  }
  else if (strncasecmp(cmd, "TUNE DEFAULT", 12) == 0 || strncasecmp(cmd, "RESET_GAINS", 11) == 0) {
    resetNvsDefaults();
    Serial.printf("[OK] Restored factory default gains (NVS cleared): Kp=%.6f, Ki=%.6f, Kd=%.6f\n",
                  _params.Kp, _params.Ki, _params.Kd);
  }
  else if (strncasecmp(cmd, "TUNE", 4) == 0) {
    float kp = 0, ki = 0, kd = 0;
    int matched = sscanf(cmd + 4, "%f %f %f", &kp, &ki, &kd);
    if (matched >= 2) {
      tunePid(kp, ki, (matched == 3) ? kd : 0.0f);
      Serial.printf("[OK] PI Tuned & Saved to Flash (NVS): Kp=%.6f, Ki=%.6f, Kd=%.6f\n", _params.Kp, _params.Ki, _params.Kd);
    } else {
      Serial.println("[ERROR] Usage: TUNE <Kp> <Ki> [Kd] (or 'TUNE DEFAULT')");
    }
  }
  else if (strncasecmp(cmd, "CALIB", 5) == 0) {
    float cal = atof(cmd + 5);
    if (cal > 0.001f) {
      setMlPerRev(cal);
      Serial.printf("[OK] Calibration set to %.4f mL/rev\n", _mlPerRev);
    } else {
      Serial.println("[ERROR] Invalid calibration factor");
    }
  }
  else if (strncasecmp(cmd, "MENISCUS", 8) == 0) {
    char* arg = cmd + 8;
    while (*arg == ' ') arg++;
    if (strncasecmp(arg, "RETRACT", 7) == 0 || strcmp(arg, "1") == 0 || strncasecmp(arg, "ON", 2) == 0) {
      setMeniscusRetracted(true);
      Serial.printf("[OK] Meniscus State: RETRACTED (Next DOSE will prime %.3f mL / %.2f revs first).\n",
                    SUCKBACK_REVS * _mlPerRev, SUCKBACK_REVS);
    } else if (strncasecmp(arg, "FLUSH", 5) == 0 || strcmp(arg, "0") == 0 || strncasecmp(arg, "OFF", 3) == 0) {
      setMeniscusRetracted(false);
      Serial.println("[OK] Meniscus State: FLUSH (Next DOSE will count immediately from zero).");
    } else {
      Serial.printf("[MENISCUS] State: %s | Auto-offset: %.3f mL (%.2f revs)\n",
                    _meniscusRetracted ? "RETRACTED (Offset Active)" : "FLUSH (At Tip, No Offset)",
                    SUCKBACK_REVS * _mlPerRev, SUCKBACK_REVS);
    }
  }
  else if (strncasecmp(cmd, "SYSID ON", 8) == 0) {
    setSysidMode(true);
  }
  else if (strncasecmp(cmd, "SYSID OFF", 9) == 0) {
    setSysidMode(false);
  }
  else if (strncasecmp(cmd, "DEBUG", 5) == 0) {
    const char* arg = cmd + 5;
    while (*arg == ' ') arg++;
    if (strcasecmp(arg, "ON") == 0 || strcmp(arg, "1") == 0) {
      setDebugStream(true);
    } else if (strcasecmp(arg, "OFF") == 0 || strcmp(arg, "0") == 0) {
      setDebugStream(false);
    } else {
      setDebugStream(!_debugStream);
    }
  }
  else if (strncasecmp(cmd, "STATUS", 6) == 0) {
    printStatus();
  }
  else if (strncasecmp(cmd, "DIAG", 4) == 0) {
    Wire.beginTransmission(0x36);
    int e1 = Wire.endTransmission(true);
    Wire.beginTransmission(0x36);
    Wire.write(0x0C);
    int e2 = Wire.endTransmission(true);
    int r1 = Wire.requestFrom((uint8_t)0x36, (uint8_t)2);
    uint8_t st = pumpEncoder().readStatus();
    uint8_t agc = pumpEncoder().readAgc();
    uint16_t mag = pumpEncoder().readMagnitude();
    bool md = (st & 0x20) != 0;
    bool ml = (st & 0x10) != 0;
    bool mh = (st & 0x08) != 0;
    Serial.println("\n========== AS5600 DIAGNOSTIC REPORT ==========");
    Serial.printf("  I2C Hardware Probe: pingErr=%d, writeRegErr=%d, reqBytes=%d\n", e1, e2, r1);
    Serial.printf("  Magnet Status: 0x%02X [MD=%d (Detected), ML=%d (Too Weak), MH=%d (Too Strong)]\n",
                  st, md, ml, mh);
    Serial.printf("  AGC Gain:      %u / 255 (Optimal: 60..180. 0=Too close, 255=Too far/No magnet)\n", agc);
    Serial.printf("  Magnitude:     %u / 4095\n", mag);
    if (!md) {
      Serial.println("  [WARNING] Magnet is NOT detected! Check air gap (< 2mm) and alignment.");
    } else if (ml) {
      Serial.println("  [WARNING] Magnet field TOO WEAK! Move sensor closer to magnet face.");
    } else if (mh) {
      Serial.println("  [WARNING] Magnet field TOO STRONG! Move sensor slightly further away.");
    } else {
      Serial.println("  [HEALTH] Magnet alignment and field strength are OPTIMAL.");
    }
    Serial.println("  10 Rapid Angle Readings (dt ~1ms):");
    for (int i = 0; i < 10; ++i) {
      uint32_t t = micros();
      uint16_t a = pumpEncoder().readRawAngle();
      Serial.printf("    #%d: raw=%4u (t=%lu us)\n", i, a, t);
      delayMicroseconds(1000);
    }
    Serial.println("==============================================\n");
  }
  else if (strncasecmp(cmd, "DUMP", 4) == 0) {
    pumpEncoder().dumpRegisters();
  }
  else if (strncasecmp(cmd, "RESET_SENSOR", 12) == 0 || strncasecmp(cmd, "RESET", 5) == 0) {
    bool ok = pumpEncoder().resetFactoryDefaults();
    if (ok) {
      Serial.println("[OK] AS5600 restored to factory defaults: ZPOS=0, MPOS=0, MANG=0 (360 deg), CONF=0.");
      pumpEncoder().dumpRegisters();
    } else {
      Serial.println("[ERROR] Failed to write factory default registers to AS5600 over I2C.");
    }
  }
  else if (strncasecmp(cmd, "FILTER", 6) == 0) {
    char* arg = cmd + 6;
    while (*arg == ' ') arg++;
    if (strcasecmp(arg, "FAST") == 0) {
      setFilterAlpha(0.25f);
      pumpEncoder().setConfig(0x07C1);
      Serial.println("[OK] Filter: FAST (alpha=0.25, AS5600 SF=2x FTH=10 LSB, latency ~0.28ms)");
    } else if (strcasecmp(arg, "BALANCED") == 0) {
      setFilterAlpha(0.12f);
      pumpEncoder().setConfig(0x0481);
      Serial.println("[OK] Filter: BALANCED (alpha=0.12, AS5600 SF=4x FTH=6 LSB, latency ~0.55ms)");
    } else if (strcasecmp(arg, "SMOOTH") == 0) {
      setFilterAlpha(0.05f);
      pumpEncoder().setConfig(0x0001);
      Serial.println("[OK] Filter: SMOOTH (alpha=0.05, AS5600 SF=16x, latency ~2.2ms)");
    } else {
      float a = atof(arg);
      if (a > 0.005f && a <= 1.0f) {
        setFilterAlpha(a);
        Serial.printf("[OK] Filter alpha set to %.3f (Settling time: ~%.1f ms)\n",
                      _filterAlpha, (1.0f / (_filterAlpha * 500.0f)) * 1000.0f);
      } else {
        Serial.println("[ERROR] Usage: FILTER <FAST | BALANCED | SMOOTH | 0.01..1.0>");
      }
    }
  }
  else if (strncasecmp(cmd, "GEAR", 4) == 0) {
    float g = atof(cmd + 4);
    if (g > 0.1f) {
      setGearRatio(g);
      Serial.printf("[OK] Gearbox reduction ratio set to 1:%.2f (Motor RPM / %.2f = Head RPM)\n", g, g);
    } else {
      Serial.println("[ERROR] Usage: GEAR <reduction_ratio> (e.g. 'GEAR 30.0')");
    }
  }
  else if (strncasecmp(cmd, "TEST HIGH", 9) == 0 || strncasecmp(cmd, "TEST FWD", 8) == 0) {
    _testMode = true;
    _telem.mode = PUMP_CTRL_OPEN_LOOP;
    _telem.dutyPercent = 100.0f;
    ledcDetachPin(TB6612_PWMA_PIN);
    pinMode(TB6612_PWMA_PIN, OUTPUT);
    pinMode(TB6612_STBY_PIN, OUTPUT);
    pinMode(TB6612_AIN1_PIN, OUTPUT);
    pinMode(TB6612_AIN2_PIN, OUTPUT);
    digitalWrite(TB6612_STBY_PIN, HIGH);
    digitalWrite(TB6612_AIN1_PIN, HIGH);
    digitalWrite(TB6612_AIN2_PIN, LOW);
    digitalWrite(TB6612_PWMA_PIN, HIGH);
    Serial.println("[TEST] FORWARD DC 100% FORCED (STBY=HIGH, AIN1=HIGH, AIN2=LOW, PWMA=HIGH).");
  }
  else if (strncasecmp(cmd, "TEST LOW", 8) == 0 || strncasecmp(cmd, "TEST REV", 8) == 0) {
    _testMode = true;
    _telem.mode = PUMP_CTRL_OPEN_LOOP;
    _telem.dutyPercent = -100.0f;
    ledcDetachPin(TB6612_PWMA_PIN);
    pinMode(TB6612_PWMA_PIN, OUTPUT);
    pinMode(TB6612_STBY_PIN, OUTPUT);
    pinMode(TB6612_AIN1_PIN, OUTPUT);
    pinMode(TB6612_AIN2_PIN, OUTPUT);
    digitalWrite(TB6612_STBY_PIN, HIGH);
    digitalWrite(TB6612_AIN1_PIN, LOW);
    digitalWrite(TB6612_AIN2_PIN, HIGH);
    digitalWrite(TB6612_PWMA_PIN, HIGH);
    Serial.println("[TEST] REVERSE DC 100% FORCED (STBY=HIGH, AIN1=LOW, AIN2=HIGH, PWMA=HIGH).");
  }
  else if (strncasecmp(cmd, "TEST OFF", 8) == 0) {
    _testMode = false;
    digitalWrite(TB6612_PWMA_PIN, LOW);
    digitalWrite(TB6612_AIN1_PIN, LOW);
    digitalWrite(TB6612_AIN2_PIN, LOW);
    digitalWrite(TB6612_STBY_PIN, HIGH);
    ledcSetup(PUMP_PWM_CHANNEL, PUMP_PWM_FREQ_HZ, PUMP_PWM_RES_BITS);
    ledcAttachPin(TB6612_PWMA_PIN, PUMP_PWM_CHANNEL);
    stop();
    Serial.println("[TEST] STOPPED. Restored PWM and Coasted.");
  }
  else if (strncasecmp(cmd, "HELP", 4) == 0) {
    printHelp();
  }
  else {
    // Numeric shorthand fallback: treat as RPM (matching nodo-mezclador UX)
    float val = atof(cmd);
    if (fabsf(val) > 0.0f && fabsf(val) <= PUMP_MAX_RPM) {
      setTargetRpm(val);
      Serial.printf("[OK] Set Closed-Loop Target: %.1f RPM (%.2f rad/s)\n", _telem.targetRpm, _telem.targetRadS);
    } else {
      Serial.println("[ERROR] Unknown command. Type 'HELP' for options.");
    }
  }
}

void PumpController::processCli() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (_cliRxIndex > 0) {
        _cliRxBuf[_cliRxIndex] = '\0';
        handleCliCommand(_cliRxBuf);
        _cliRxIndex = 0;
      }
    } else if (_cliRxIndex < sizeof(_cliRxBuf) - 1) {
      _cliRxBuf[_cliRxIndex++] = c;
    }
  }
}
