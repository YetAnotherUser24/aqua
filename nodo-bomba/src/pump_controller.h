/**
 * @file pump_controller.h
 * @brief Discrete 100 Hz PI Speed Controller, Virtual Flow Estimator, H-Bridge and Suck-back CLI.
 */

#pragma once

#include <stdint.h>
#include <Arduino.h>
#include "config.h"

enum PumpControlMode : uint8_t {
  PUMP_CTRL_STOPPED = 0,
  PUMP_CTRL_OPEN_LOOP = 1,
  PUMP_CTRL_CLOSED_LOOP = 2,
  PUMP_CTRL_DOSE = 3,
  PUMP_CTRL_SUCKBACK = 4,
  PUMP_CTRL_DRAIN = 5
};

struct PumpPidParams {
  float Kp = 0.003780f;             // Sintonizado óptimo SIMC para eje motor: duty / (rad/s)
  float Ki = 0.026888f;             // Lazo PI puro: duty / (rad/s * s)
  float Kd = 0.000000f;             // Sin derivada para evitar amplificación de ruido
  float Ts = 0.01f;                 // 100 Hz loop (10 ms)
  float maxSlewRate = 3.0f;         // Limitador de slew rate de aceleración (duty/s)
  float minDuty = 0.10f;            // Umbral de stiction (10% duty para vencer pellizco)
  float maxDuty = 1.00f;            // 100% duty max
  float integralMin = -0.5f;
  float integralMax = 1.0f;
};

struct PumpTelemetryData {
  float actualRadS = 0.0f;          // Estado canónico SI: velocidad angular real (rad/s)
  float targetRadS = 0.0f;          // Setpoint canónico SI: velocidad angular objetivo (rad/s)
  float actualRpm = 0.0f;           // Unidad secundaria derivada: RPM
  float targetRpm = 0.0f;           // Unidad secundaria derivada: RPM
  float flowRateMlMin = 0.0f;       // Sensor virtual de caudal: Q = (rad_s * 60 / 2pi / gear) * ML_PER_REV
  float dutyPercent = 0.0f;
  float dosedMl = 0.0f;
  float targetMl = 0.0f;
  float distTorque = 0.0f;          // Estimador de torque de perturbación (rad/s^2, Luenberger)
  uint16_t rawAngle = 0;
  uint32_t totalTicks = 0;
  float totalRevolutions = 0.0f;
  PumpControlMode mode = PUMP_CTRL_STOPPED;
  bool doseCompleted = false;
  bool doseTimedOut = false;
  uint32_t statusCode = 0;
};

class PumpController {
public:
  PumpController();

  void init(float mlPerRev = 1.00f, uint32_t doseTimeoutMs = 120000UL);
  void update(uint32_t nowMs);
  void processCli();

  // Control Commands (rad/s canonical)
  void stop();
  void setTargetRadS(float rad_s);
  void setTargetRpm(float rpm);
  void setOpenLoopDuty(float dutyFraction); // [-1.0, 1.0]
  void startDose(float targetMl, float rad_s = 6.283f);
  void triggerSuckback(float revs = 0.35f, float rad_s = 3.665f);
  void startDrain(float durationS = 35.0f, float rpm = 1000.0f);
  void tunePid(float kp, float ki, float kd = 0.0f); // Kp in duty/(rad/s), Ki in duty/(rad/s*s)
  void setMlPerRev(float mlPerRev);
  void setFilterAlpha(float alpha);
  void setGearRatio(float ratio);
  void setSysidMode(bool enable);
  void setDebugStream(bool enable);
  void loadFromNvs();
  void saveToNvs();
  void resetNvsDefaults();
  bool isMeniscusRetracted() const { return _meniscusRetracted; }
  void setMeniscusRetracted(bool retracted);

  // Status & Telemetry
  const PumpTelemetryData& telemetry() const { return _telem; }
  float rpm() const { return _telem.actualRpm; }
  float gearRatio() const { return _gearRatio; }
  float filterAlpha() const { return _filterAlpha; }
  float radS() const { return _telem.actualRadS; }
  float flowRateMlMin() const { return _telem.flowRateMlMin; }
  float dosedMl() const { return _telem.dosedMl; }
  float dutyPercent() const { return _telem.dutyPercent; }
  PumpControlMode mode() const { return _telem.mode; }
  bool isDoseCompleted() const { return _telem.doseCompleted; }
  bool isDoseTimedOut() const { return _telem.doseTimedOut; }
  bool isSysidActive() const { return _sysidMode; }
  bool isDebugStreamActive() const { return _debugStream; }

  void printStatus();
  void printHelp();

private:
  void updateAngleTracker();
  void updateVelocity(uint32_t nowMs);
  void updatePid(uint32_t nowMs);
  void handleCliCommand(char* cmd);

  PumpPidParams _params;
  PumpTelemetryData _telem;

  // Velocity and disturbance observer internals (Luenberger 2nd order)
  uint16_t _prevRawAngle = 0;
  bool _anglePrimed = false;
  int32_t _accumTicks = 0;
  uint32_t _lastVelocityUs = 0;
  float _filteredRadS = 0.0f;       // Primary filtered velocity in rad/s
  float _obsTheta = 0.0f;           // Estimated angular position (rad)
  float _obsOmega = 0.0f;           // Estimated angular velocity (rad/s)
  float _obsDist = 0.0f;            // Estimated load torque disturbance (rad/s^2)

  // PID internals
  float _integral = 0.0f;
  float _prevOutput = 0.0f;
  float _prevRadS = 0.0f;
  uint32_t _lastPidMs = 0;

  // Dosing internals
  float _mlPerRev = 1.00f;
  float _gearRatio = 1.0f;          // Motor revs per peristaltic head rev (e.g. 30.0 for 1:30 gearbox)
  float _filterAlpha = 0.08f;       // Optimal EMA weight: CV<4.5%, settling ~25ms at 500 Hz
  uint32_t _doseTimeoutMs = 120000UL;
  uint32_t _doseStartMs = 0;

  // Suck-back internals (H-Bridge reverse)
  float _suckbackTargetRevs = 0.0f;
  float _suckbackStartRev = 0.0f;
  bool _meniscusRetracted = true;   // True if meniscus is parked ~15-20mm inside nozzle
  uint32_t _drainDurationMs = 0;
  uint32_t _drainStartMs = 0;

  // SysID and CLI internals
  bool _sysidMode = false;
  bool _debugStream = (PUMP_SERIAL_DEBUG != 0);
  bool _testMode = false;
  uint32_t _lastTelemetryMs = 0;
  char _cliRxBuf[64];
  uint8_t _cliRxIndex = 0;
};

extern PumpController g_pumpCtrl;
