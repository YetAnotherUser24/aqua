/**
 * Port fiel de `nodo-aerador/src/control_engine.cpp`.
 *
 * Reproduce el orden de decisión EXACTO de `computeOutputRpm()`:
 *   e-stop -> MANUAL -> sin primera muestra -> watchdog 35 s -> PID/Fuzzy -> saturación 0-600.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * DEFECTO ENCONTRADO EN EL FIRMWARE (documentado aquí porque el gemelo lo reproduce)
 *
 * En `computeOutputRpm()`:
 *     float dError = (error - _prevError) / dt;
 *     _prevError = error;              // <-- se actualiza ANTES de llamar al PID
 *     ...
 *     outputRpm = runPid(error, dt, cfg);
 *
 * y dentro de `runPid()`:
 *     float dTerm = cfg.kd * (error - _prevError) / dt;   // (error - _prevError) == 0 SIEMPRE
 *
 * Como `_prevError` ya vale `error`, el término derivativo es **siempre cero**. Con `Kd = 10.0`
 * configurado y expuesto en la UI, su efecto real es nulo: el lazo es de hecho un PI.
 *
 * `derivativeMode`:
 *   'errorDelta' (por defecto) -> matemática corregida: dTerm = Kd * dError (usa el dError ya
 *                                 calculado, que sí emplea el _prevError anterior).
 *   'firmwareV1Dead'           -> reproduce el comportamiento flasheado (dTerm = 0), para poder
 *                                 comparar ambos y cuantificar el impacto.
 */
const { odrive: C } = require('./constants');

const MODE_PID = 0;
const MODE_FUZZY = 1;
const MODE_MANUAL = 2;

const constrain = (v, lo, hi) => (v < lo ? lo : (v > hi ? hi : v));

class ControlEngine {
  constructor({ mode = MODE_PID, derivativeMode = 'errorDelta', vbusNominal } = {}) {
    this._config = {
      mode,
      target_do_mg_l: C.DEFAULT_TARGET_DO_MG_L,
      kp: C.DEFAULT_KP,
      ki: C.DEFAULT_KI,
      kd: C.DEFAULT_KD,
      min_rpm: C.MIN_MOTOR_RPM,
      max_rpm: C.MAX_MOTOR_RPM,
      failsafe_rpm: C.FAILSAFE_MOTOR_RPM,
      manual_throttle_pct: 0.0
    };
    this.derivativeMode = derivativeMode;
    this.vbusNominal = vbusNominal;

    this._lastMeasuredDo = 0.0;
    this._lastWaterTemp = 25.0;
    this._lastPacketTimeMs = 0;
    this._hasFirstSample = false;
    this._failsafeActive = false;
    this._emergencyStopActive = false;
    this._experimentLogging = false;

    this._integral = 0.0;
    this._prevError = 0.0;
    this._lastComputeMs = 0;
  }

  configure(patch) {
    this._config = { ...this._config, ...patch };
    if (this._config.max_rpm < this._config.min_rpm) this._config.max_rpm = this._config.min_rpm + 500.0;
    this._config.manual_throttle_pct = constrain(this._config.manual_throttle_pct, 0, 100);
  }

  getConfig() { return { ...this._config }; }

  /** OJO: el original limpia el latch de e-stop aquí (no lo hace `configure`). */
  setManualThrottle(pct) {
    this._config.mode = MODE_MANUAL;
    this._config.manual_throttle_pct = constrain(pct, 0, 100);
    this._emergencyStopActive = false;
  }

  triggerEmergencyStop() {
    this._config.mode = MODE_MANUAL;
    this._config.manual_throttle_pct = 0;
    this._emergencyStopActive = true;
  }

  isEmergencyStopActive() { return this._emergencyStopActive; }

  clearEmergencyStop() { this._emergencyStopActive = false; }

  setExperimentLogging(active) { this._experimentLogging = active; }
  isExperimentLogging() { return this._experimentLogging; }

  /**
   * Llamado desde el receptor ESP-NOW. Refresca el watchdog implícitamente.
   * `nowMs` sustituye a `millis()` del firmware para que el modelo sea determinista; en el MCU el
   * valor procede del reloj del nodo.
   */
  updateProcessVariable(measuredDoMgL, waterTempC, nowMs) {
    this._lastMeasuredDo = measuredDoMgL;
    this._lastWaterTemp = waterTempC;
    this._lastPacketTimeMs = nowMs !== undefined ? nowMs : (this._nowMs ?? 0);
    this._hasFirstSample = true;
    this._failsafeActive = false;
  }

  getLastMeasuredDo() { return this._lastMeasuredDo; }
  getLastWaterTemp() { return this._lastWaterTemp; }
  isFailsafeActive() { return this._failsafeActive; }

  getLastPacketAgeMs(nowMs) {
    if (!this._hasFirstSample) return 0xFFFFFFFF;
    return nowMs >= this._lastPacketTimeMs ? nowMs - this._lastPacketTimeMs : 0;
  }

  /** `computeOutputRpm()` — 20 Hz en Core 1. */
  computeOutputRpm(nowMs) {
    this._nowMs = nowMs;
    const cfg = this._config;

    // 2. La parada de emergencia tiene prioridad absoluta
    if (this._emergencyStopActive) return 0.0;

    // 3. Anulación manual
    if (cfg.mode === MODE_MANUAL) {
      this._failsafeActive = false;
      const throttle = cfg.manual_throttle_pct / 100.0;
      return constrain(throttle * cfg.max_rpm, cfg.min_rpm, cfg.max_rpm);
    }

    // 4. Arranque seguro: 0 RPM hasta la primera muestra válida
    if (!this._hasFirstSample) {
      this._failsafeActive = true;
      return 0.0;
    }

    // 5. Watchdog: si el enlace con el sensor cae, se va a la consigna de seguridad
    if (nowMs - this._lastPacketTimeMs > C.FAILSAFE_TIMEOUT_MS) {
      this._failsafeActive = true;
      return cfg.failsafe_rpm;
    }
    this._failsafeActive = false;

    // 5b. Cálculo de tiempos
    if (this._lastComputeMs === 0) {
      this._lastComputeMs = nowMs;
      return cfg.min_rpm;
    }
    let dt = (nowMs - this._lastComputeMs) / 1000.0;
    this._lastComputeMs = nowMs;
    if (dt <= 0.0001 || dt > 5.0) dt = C.CONTROL_LOOP_INTERVAL_MS / 1000.0;

    // 6. Error (positivo = OD bajo -> hace falta más aireación)
    const error = cfg.target_do_mg_l - this._lastMeasuredDo;
    const dError = (error - this._prevError) / dt;
    this._prevError = error;

    let outputRpm = 0.0;
    if (cfg.mode === MODE_PID) {
      outputRpm = this.runPid(error, dError, dt, cfg);
    } else if (cfg.mode === MODE_FUZZY) {
      outputRpm = this.runFuzzy(error, dError, cfg);
    }

    return constrain(outputRpm, cfg.min_rpm, cfg.max_rpm);
  }

  /**
   * PID con anti-windup por saturación de la integral (I_max = max_rpm / Ki).
   * `dError` se recibe ya calculado con el `_prevError` ANTERIOR: es lo que hace que el término
   * derivativo funcione (ver la nota del encabezado).
   */
  runPid(error, dError, dt, cfg) {
    const pTerm = cfg.kp * error;

    this._integral += error * dt;
    const maxIntegral = cfg.max_rpm / (cfg.ki > 0.001 ? cfg.ki : 1.0);
    this._integral = constrain(this._integral, -maxIntegral, maxIntegral);
    const iTerm = cfg.ki * this._integral;

    const dTerm = this.derivativeMode === 'firmwareV1Dead'
      ? 0.0
      : cfg.kd * dError;

    return pTerm + iTerm + dTerm;
  }

  /** `runFuzzy()` — incluido por fidelidad; el fuzzy está EXCLUIDO del alcance de la tesis. */
  runFuzzy(error, dError, cfg) {
    const u_neg = (error <= 0.0) ? 1.0 : ((error < 0.5) ? (0.5 - error) / 0.5 : 0.0);
    const u_low = (error > 0.0 && error <= 0.8) ? (error / 0.8)
      : ((error > 0.8 && error < 1.8) ? (1.8 - error) / 1.0 : 0.0);
    const u_med = (error > 0.8 && error <= 1.8) ? (error - 0.8) / 1.0
      : ((error > 1.8 && error < 3.0) ? (3.0 - error) / 1.2 : 0.0);
    const u_high = (error >= 3.0) ? 1.0 : ((error > 1.8) ? (error - 1.8) / 1.2 : 0.0);

    const u_improving = (dError <= -0.1) ? 1.0 : ((dError < 0.0) ? (-dError) / 0.1 : 0.0);
    const u_steady = (Math.abs(dError) <= 0.1) ? 1.0 - (Math.abs(dError) / 0.1) : 0.0;
    const u_worsening = (dError >= 0.1) ? 1.0 : ((dError > 0.0) ? dError / 0.1 : 0.0);

    const span = cfg.max_rpm - cfg.min_rpm;
    const RPM_OFF = cfg.min_rpm;
    const RPM_LOW = cfg.min_rpm + span * 0.25;
    const RPM_MED = cfg.min_rpm + span * 0.55;
    const RPM_HIGH = cfg.min_rpm + span * 0.80;
    const RPM_MAX = cfg.max_rpm;

    let num = 0.0;
    let den = 0.0;
    const addRule = (weight, singleton) => {
      if (weight > 0.0001) { num += weight * singleton; den += weight; }
    };

    addRule(u_neg, RPM_OFF);
    addRule(Math.min(u_low, u_improving), RPM_OFF);
    addRule(Math.min(u_low, u_steady), RPM_LOW);
    addRule(Math.min(u_low, u_worsening), RPM_MED);
    addRule(Math.min(u_med, u_improving), RPM_LOW);
    addRule(Math.min(u_med, u_steady), RPM_MED);
    addRule(Math.min(u_med, u_worsening), RPM_HIGH);
    addRule(u_high, RPM_MAX);

    if (den < 0.0001) return cfg.min_rpm;
    return num / den;
  }
}

module.exports = { ControlEngine, MODE_PID, MODE_FUZZY, MODE_MANUAL };
