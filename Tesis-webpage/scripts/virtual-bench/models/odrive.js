/**
 * Port fiel de `nodo-aerador/src/motor_sim.cpp` (+ `motor_bus_virtual.cpp`).
 *
 * Reemplazo directo del modelo de motor + driver ODrive S1: dinamica de velocidad de primer orden,
 * carga hidrodinamica cuadratica, caida del bus DC por resistencia interna, potencia, torque
 * estimado, corriente de eje q (proxy) y temperatura de FET con retardo termico, mas la maquina de
 * estados de armado (IDLE <-> CLOSED_LOOP con retardo de armado) y el desarme por fallo.
 *
 * Cada formula conserva la forma EXACTA de `motor_sim.cpp`; solo se sustituye `millis()` por un
 * `nowMs` inyectado para que el modelo sea reproducible y testeable fuera del MCU.
 *
 * Diferencias deliberadas (documentadas, no accidentales):
 *  1. `VIRTUAL_VBUS_NOMINAL` usa 12 V (el banco real) en vez de los 24 V del firmware. Parametrizable.
 *  2. `dt` se recorta igual que el original, pero el gemelo recibe el tiempo del runner.
 */
const { odrive: C, odriveModel: M } = require('./constants');

class ODriveVirtual {
  constructor({ nominalVbus = C.VIRTUAL_VBUS_NOMINAL, rampRateRpmPerSec = C.VIRTUAL_RAMP_RPM_PER_S } = {}) {
    this._spec = {
      rpmMax: C.MAX_MOTOR_RPM,
      spinUpTauS: M.SPIN_UP_TAU_S,
      nominalVbus,
      busResistance: M.BUS_INTERNAL_R_OHM,
      idleIbus: C.VIRTUAL_IBUS_IDLE,
      maxLoadIbus: C.VIRTUAL_IBUS_MAX_LOAD,
      ambientTempC: M.FET_TEMP_BASE_C,
      tempPerAmp: M.FET_TEMP_PER_AMP,
      tempTauS: 4.0,
      armDelayMs: 150
    };
    this._rampRate = rampRateRpmPerSec;
    this.reset();
  }

  reset() {
    const s = this._spec;
    this._targetRpm = 0;
    this._actualRpm = 0;
    this._turns = 0;
    this._vbus = s.nominalVbus;
    this._ibus = s.idleIbus;
    this._power = this._vbus * this._ibus;
    this._torque = 0;
    this._fetTemp = s.ambientTempC;
    this._iq = 0;
    this._state = M.STATE_IDLE;
    this._controlMode = 2;
    this._inputMode = 2;
    this._axisError = 0;
    this._isArmed = false;
    this._lastUpdateMs = 0;
    this._armAtMs = 0;
  }

  begin() { this.reset(); return true; }
  setSpec(spec) { Object.assign(this._spec, spec); }

  setVelocityRpm(rpm) {
    if (!this._isArmed || this._state !== M.STATE_CLOSED_LOOP) { this._targetRpm = 0; return true; }
    this._targetRpm = Math.min(this._spec.rpmMax, Math.max(0, rpm));
    return true;
  }

  requestState(state) {
    this._state = state;
    this._isArmed = state === M.STATE_CLOSED_LOOP;
    if (this._isArmed) this._armAtMs = this._lastUpdateMs + this._spec.armDelayMs;
    else this._targetRpm = 0;
    return true;
  }

  enterClosedLoop() { this.clearErrors(); this.requestState(M.STATE_CLOSED_LOOP); return true; }
  enterIdle() { this.requestState(M.STATE_IDLE); return true; }
  clearErrors() { this._axisError = 0; return true; }

  injectFault(mask) {
    this._axisError |= mask;
    this._state = M.STATE_IDLE;
    this._isArmed = false;
    this._targetRpm = 0;
  }

  setSimulatedError(errorCode) { this.injectFault(errorCode); }
  setNominalVbus(v) { this._spec.nominalVbus = v; }
  setRampRate(rpmPerSec) { if (rpmPerSec > 10.0) this._rampRate = rpmPerSec; }

  updatePhysics(nowMs) {
    if (this._lastUpdateMs === 0) { this._lastUpdateMs = nowMs; return; }
    let dt = (nowMs - this._lastUpdateMs) / 1000.0;
    this._lastUpdateMs = nowMs;
    if (dt <= 0) return;
    if (dt > M.DT_CLAMP_S) dt = M.DT_CLAMP_S;

    const s = this._spec;
    const closedLoop = this._isArmed && this._state === M.STATE_CLOSED_LOOP;
    const armSettled = closedLoop && nowMs >= this._armAtMs;

    const alpha = dt / (s.spinUpTauS + dt);
    if (armSettled) {
      this._actualRpm += (this._targetRpm - this._actualRpm) * alpha;
    } else {
      this._actualRpm += (0 - this._actualRpm) * alpha;
      if (this._actualRpm < 0.5) this._actualRpm = 0;
    }
    if (this._actualRpm > s.rpmMax) this._actualRpm = s.rpmMax;

    this._turns += (this._actualRpm / 60.0) * dt;

    const norm = s.rpmMax > 0 ? this._actualRpm / s.rpmMax : 0;
    this._ibus = s.idleIbus + s.maxLoadIbus * norm * norm;
    this._vbus = s.nominalVbus - this._ibus * s.busResistance;
    this._power = this._vbus * this._ibus;

    const omega = (this._actualRpm * 2.0 * Math.PI) / 60.0;
    this._torque = omega > M.TORQUE_OMEGA_MIN ? this._power / omega : 0;
    // Same threshold as torque so both fall to zero together (no iq blow-up at stop).
    this._iq = omega > M.TORQUE_OMEGA_MIN ? this._torque / M.KT_NM_PER_A : 0;

    const fetTarget = s.ambientTempC + this._ibus * s.tempPerAmp;
    const ta = dt / (s.tempTauS + dt);
    this._fetTemp += (fetTarget - this._fetTemp) * ta;
  }

  pollTelemetry(nowMs) {
    this.updatePhysics(nowMs);
    return {
      vbus_voltage: this._vbus,
      ibus_current: this._ibus,
      actual_rpm: this._actualRpm,
      power_watts: this._power,
      torque_estimate: this._torque,
      iq_measured: this._iq,
      fet_temperature: this._fetTemp,
      axis_error: this._axisError,
      disarm_reason: (this._state === M.STATE_IDLE && !this._isArmed) ? 1 : 0,
      current_state: this._state,
      control_mode: this._controlMode,
      input_mode: this._inputMode,
      is_armed: this._isArmed
    };
  }

  readFeedbackFast() { return { turns: this._turns, rpm: this._actualRpm }; }

  get state() {
    return {
      targetRpm: this._targetRpm, actualRpm: this._actualRpm, vbus: this._vbus,
      ibus: this._ibus, power: this._power, torque: this._torque, iq: this._iq,
      fetTemp: this._fetTemp, isArmed: this._isArmed, axisError: this._axisError
    };
  }
}

module.exports = { ODriveVirtual };
