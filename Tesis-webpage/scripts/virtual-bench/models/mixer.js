/**
 * Port fiel del nodo MIXER (nodo-mezclador): DRIVER con lazo PI cerrado.
 *
 * ⚠️ CORRECCIÓN IMPORTANTE: este nodo NO usa un ESC ni PWM de servo. Usa un **driver SNR8503M**
 * comandado por PWM de 10 kHz (lógica invertida, con linealización analítica de su integrador RC) y
 * **realimentación por tacómetro FG**: el lazo PI vive DENTRO del controlador, midiendo el período
 * de los pulsos del driver. Modelado antes como un ESC genérico, lo cual era incorrecto.
 *
 * Fuentes (`nodo-mezclador/src/mixer_sim.cpp` y `include/config.h`):
 *   - PI de velocidad en unidades SI (rad/s) a 100 Hz
 *   - Anti-windup por saturación del integrador y limitador de slew en la salida
 *   - Detección de atasco con recuperación automática
 *
 * Constantes reales del firmware (no inventadas):
 *   Kp = 0.00274565, Ki = 0.00642846, Kd = 0.0 (término derivativo eliminado)
 *   Ts = 0.010 s (100 Hz), integral acotada a [-0.5, 1.0], slew 2.0/s
 *   MAX_THRUSTER_RPM = 3800 (T-200), BLDC_POLE_PAIRS = 7
 *   MOTOR_MIN_SPIN_DUTY = 0.25 (umbral empírico de arranque), MOTOR_MAX_ALLOWED_DUTY = 0.989
 *   PWM 10 kHz, 10 bits
 *
 * Lo único INVENTADO es la respuesta mecánica duty → RPM del conjunto driver+motor+hélice
 * (`UNIDENTIFIED`): la interfaz (duty de entrada, RPM por FG de salida) es la real, pero la curva
 * no está identificada sobre el banco.
 */

const TWO_PI = 6.283185307179586;

const T200 = {
  Kp: 0.00274565,
  Ki: 0.00642846,
  Kd: 0.0,
  Ts: 0.010,                 // 100 Hz
  INTEGRAL_MIN: -0.5,
  INTEGRAL_MAX: 1.0,
  MAX_SLEW_RATE: 2.0,        // cambio máximo de duty por segundo (0 -> 100 % en 500 ms)
  MAX_THRUSTER_RPM: 3800.0,
  MAX_THRUSTER_RAD_S: (3800.0 * TWO_PI) / 60.0,   // ~397.9 rad/s
  POLE_PAIRS: 7,
  MIN_SPIN_DUTY: 0.25,
  MAX_ALLOWED_DUTY: 0.989,
  PWM_FREQ_HZ: 10000,
  PW_RES_BITS: 10,
  STALL_RPM_THRESHOLD: 50.0,
  STALL_TIMEOUT_MS: 1500,
  RECOVERY_PULSE_MS: 500,
  FG_STOP_TIMEOUT_US: 3500000
};

/** Curva mecánica duty -> RPM del conjunto. INVENTADA (ver la nota de arriba). */
const UNIDENTIFIED = {
  /** Retardo de primer orden del conjunto motor+hélice (s). */
  MOTOR_TAU_S: 0.35,
  /** Exponente de la curva duty -> RPM. */
  DUTY_EXPONENT: 0.85
};

class MixerDriver {
  constructor() {
    // PI de velocidad (rad/s)
    this.targetRadS = 0;
    this.actualRadS = 0;
    this.integral = 0;
    this.prevOutput = 0;

    // Salida
    this.commandedDuty = 0;      // duty que el PI pide (0..1)
    this.compensatedDuty = 0;    // duty tras la linealización del integrador del driver

    // Diagnóstico
    this.statusCode = 0;
    this.inStallRecovery = false;
    this.stallTimerMs = 0;
    this.recoveryStartMs = 0;
    this.isRunning = false;

    this._lastStepMs = null;
  }

  /** Consigna en rad/s (es lo que recibe el firmware desde el orquestador). */
  setTargetRadS(radS) {
    const v = Number(radS) || 0;
    this.targetRadS = Math.max(0, Math.min(T200.MAX_THRUSTER_RAD_S, v));
    this.isRunning = this.targetRadS > 0.1;
    if (this.targetRadS <= 0.1) this.reset();
  }

  /** Equivalente al comando por porcentaje del orquestador (0-100 % de 397.9 rad/s). */
  setTargetPercent(pct) {
    this.setTargetRadS((Math.max(0, Math.min(100, Number(pct) || 0)) / 100) * T200.MAX_THRUSTER_RAD_S);
  }

  /** `pidReset()`: borra el integrador y alinea el estado del limitador de slew. */
  reset() {
    this.integral = 0;
    this.prevOutput = 0;
    this.commandedDuty = 0;
    this.compensatedDuty = 0;
    this.actualRadS = 0;
  }

  /**
   * Un paso del lazo (100 Hz). Reproduce el orden EXACTO del firmware:
   * error -> P -> integración con anti-windup -> suma -> saturación -> slew -> duty al driver.
   */
  step(nowMs) {
    if (this._lastStepMs === null) { this._lastStepMs = nowMs; return; }
    this._lastStepMs = nowMs;

    // --- Recuperación de atasco: 0 % de duty durante 500 ms ---
    if (this.inStallRecovery) {
      this.applyDuty(0);
      this.reset();
      if (nowMs - this.recoveryStartMs > T200.RECOVERY_PULSE_MS) {
        this.inStallRecovery = false;
        this.stallTimerMs = 0;
      }
      return;
    }

    const rpm = this.radsToRpm(this.actualRadS);

    // --- Detección de atasco: consigna activa y el FG marca < 50 RPM durante 1.5 s ---
    if (this.targetRadS > 0.1 && rpm < T200.STALL_RPM_THRESHOLD) {
      if (this.stallTimerMs === 0) this.stallTimerMs = nowMs;
      else if (nowMs - this.stallTimerMs > T200.STALL_TIMEOUT_MS) {
        this.inStallRecovery = true;
        this.recoveryStartMs = nowMs;
        this.stallTimerMs = 0;
        this.statusCode = 1;
        return;
      }
    } else {
      this.stallTimerMs = 0;
    }

    // --- PI de velocidad ---
    const error = this.targetRadS - this.actualRadS;
    const pTerm = T200.Kp * error;

    // Integración con anti-windup condicional (mismo clamp que el firmware)
    this.integral += T200.Ki * error * T200.Ts;
    this.integral = Math.max(T200.INTEGRAL_MIN, Math.min(T200.INTEGRAL_MAX, this.integral));

    // Derivativo eliminado en el firmware (evita envenenar con NaN si el tacómetro glitchea)
    const ffTerm = 0.0;

    const uRaw = pTerm + this.integral + ffTerm;
    const uClamped = Math.max(0, Math.min(1, uRaw));

    // Limitador de slew
    const maxDelta = T200.MAX_SLEW_RATE * T200.Ts;
    const delta = Math.max(-maxDelta, Math.min(maxDelta, uClamped - this.prevOutput));
    const uSlewed = this.prevOutput + delta;
    this.prevOutput = uSlewed;

    this.applyDuty(uSlewed);
    this.advanceMotor();
  }

  /** `applyHardwareDuty()`: fija el duty y su compensación; por debajo de 0.005 hay parada estricta. */
  applyDuty(duty) {
    const d = Math.max(0, Math.min(1, duty));
    this.commandedDuty = d < 0.005 ? 0 : d;
    // Linealización analítica del integrador RC del driver (habilitada en el firmware).
    this.compensatedDuty = this.commandedDuty === 0 ? 0 : this.linearize(this.commandedDuty);
  }

  /**
   * Inversa analítica del integrador RC del driver (ENABLE_PWM_LINEARIZATION = 1).
   * El firmware compensa la no linealidad del divisor RC interno para que el duty efectivo sea
   * proporcional al throttle pedido.
   */
  linearize(dTarget) {
    const Rp = 200.0, R30 = 10000.0, G = 2.0 / 3.0;
    const alpha = Rp / (Rp + R30);
    const beta = 1 - alpha;
    const eff = G * dTarget;
    if (eff >= 1) return 1;
    if (eff <= 0) return 0;
    // Resolución de la ecuación del integrador: D_pwm = (eff + alpha*D_pwm) * ... -> forma cerrada
    return Math.max(0, Math.min(1, (eff + alpha * dTarget) / (beta + G)));
  }

  /** Respuesta mecánica: duty -> RPM (INVENTADA) y de ahí a rad/s, con retardo de primer orden. */
  advanceMotor() {
    const d = this.commandedDuty;
    let steadyRpm = 0;
    if (d >= T200.MIN_SPIN_DUTY) {
      const span = (d - T200.MIN_SPIN_DUTY) / (T200.MAX_ALLOWED_DUTY - T200.MIN_SPIN_DUTY);
      steadyRpm = T200.MAX_THRUSTER_RPM * Math.pow(Math.min(1, Math.max(0, span)), UNIDENTIFIED.DUTY_EXPONENT);
    }
    const targetRadS = (steadyRpm * TWO_PI) / 60.0;
    const a = T200.Ts / (UNIDENTIFIED.MOTOR_TAU_S + T200.Ts);
    this.actualRadS += a * (targetRadS - this.actualRadS);

    // El FG nunca da exactamente cero si gira: por debajo del umbral de arranque, 0.
    if (this.commandedDuty < T200.MIN_SPIN_DUTY) this.actualRadS = 0;
  }

  /**
   * Tacómetro FG: el firmware obtiene el RPM midiendo el período de los pulsos.
   *   freq_hz = 1e6 / period_us ; rps = freq_hz / POLE_PAIRS ; rpm = rps * 60
   * Se expone para poder inyectar un fallo de tacómetro (pérdida de pulsos).
   */
  fgPulsePeriodUs() {
    const rps = this.actualRadS / TWO_PI;
    const freq = rps * T200.POLE_PAIRS;
    if (freq <= 0) return T200.FG_STOP_TIMEOUT_US;
    return Math.round(1e6 / freq);
  }

  radsToRpm(radS) { return (radS / TWO_PI) * 60.0; }
  get rpm() { return this.radsToRpm(this.actualRadS); }

  /**
   * Telemetría EXACTA que publica el firmware del T-200 en `/api/telemetry/motor`.
   * Incluye `experiment_id`: el firmware lo recibe en el `set_state` y lo adjunta a cada muestra,
   * de modo que el archivo por rol queda atribuido al ensayo correcto.
   */
  telemetry(rtcMs, experimentId = 'idle') {
    return {
      experiment_id: experimentId,
      target_rpm: Math.round(this.radsToRpm(this.targetRadS) * 100) / 100,
      actual_rpm: Math.round(this.rpm * 100) / 100,
      target_rad_s: Math.round(this.targetRadS * 10000) / 10000,
      actual_rad_s: Math.round(this.actualRadS * 10000) / 10000,
      commanded_duty: Math.round(this.commandedDuty * 10000) / 10000,
      kp: T200.Kp,
      ki: T200.Ki,
      kd: T200.Kd,
      status_code: this.statusCode,
      is_running: this.isRunning,
      rtc_timestamp_ms: rtcMs
    };
  }
}

module.exports = { MixerDriver, T200, UNIDENTIFIED };
