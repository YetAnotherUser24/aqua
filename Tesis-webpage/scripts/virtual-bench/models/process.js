/**
 * MODELO DE PROCESO (planta sintética) para el banco virtual.
 *
 * ⚠️ ADVERTENCIA DE ALCANCE — LÉASE ANTES DE USAR ESTE MODELO
 *
 * Este modelo existe para observar que el SISTEMA COMPLETO trabaja coordinado: que al girar el
 * aireador el oxígeno sube, que al inyectar sulfito baja, y que todo se refleja en la telemetría.
 * NO es un gemelo térmico/hidrodinámico del tanque y NO está identificado sobre el banco físico.
 *
 * En particular, `KLA_MAX_PER_S` y las constantes de mezcla son VALORES INVENTADOS (etiquetados
 * UNIDENTIFIED). Por eso:
 *   - Su salida NUNCA es evidencia experimental ni puede alimentar el Capítulo VII (AGENTS.md regla 10).
 *   - NO sirve para validar ganancias, ni para estimar K, τ_p ni θ_d: eso sale del escalón en banco.
 *   - NO hay lazo cerrado aquí: el aireador recibe consignas, no las calcula contra esta planta.
 *
 * Lo que SÍ es física real y por eso se usa tal cual:
 *   - La solubilidad de saturación del oxígeno en agua dulce en función de la temperatura
 *     (ecuación estándar APHA). A 24 °C da 8.33 mg/L, coherente con los 8.39 mg/L implícitos en las
 *     mediciones del banco (7.27 mg/L = 86.6 % sat a 24.08 °C).
 *   - La estequiometría del sulfito: Na2SO3 + ½O2 → Na2SO4 consume 7.878 mg de sulfito por mg de O2.
 *     Sin catalizador de cobalto (prohibido por la regla 11).
 */

/** Saturación de OD en agua dulce a 1 atm (mg/L) — ecuación estándar APHA. */
function doSaturationMgL(tempC) {
  const T = Number(tempC);
  return 14.652 - 0.41022 * T + 0.007991 * T * T - 0.000077774 * T * T * T;
}

const SULFITE_MG_PER_O2_MG = 126.04 / 16.0; // 7.878 mg Na2SO3 por mg de O2

/* ------------------------------------------------------------------------- */
/* Constantes INVENTADAS (UNIDENTIFIED). Solo dan una dinámica plausible.     */
/* ------------------------------------------------------------------------- */
const UNIDENTIFIED = {
  /** KLa máximo a 600 RPM (1/s). Elegido para que el transitorio se vea en segundos. */
  KLA_MAX_PER_S: 0.020,
  /** Exponente de la dependencia de KLa con las RPM (la teoría suele dar 0.5-1). */
  KLA_RPM_EXPONENT: 0.7,
  /** Consumo basal de oxígeno (respiración del agua) en mg/L/s. */
  RESPIRE_MG_L_S: 0.0004,
  /**
   * Ritmo intrínseco de reacción del sulfito (1/s). Sin él la reacción sería instantánea y no
   * habría transitorio que observar. INVENTADO: solo da una dinámica plausible.
   */
  SULFITE_REACTION_K: 0.05,
  /** Factor de mejora de la transferencia por agitación del mixer. */
  MIXER_KLA_BOOST: 1.35,
  /** Multiplicador de la velocidad de reacción del sulfito con mezcla activa. */
  MIXER_REACTION_BOOST: 3.0,
  /**
   * Factor de aceleración temporal. El banco real tardaría minutos u horas; para poder testear en
   * segundos se acelera el tiempo simulado. Se declara para que nadie confunda escalas.
   */
  TIME_ACCELERATION: 1.0
};

/**
 * Planta de oxígeno disuelto de un tanque agitado, con entrada de oxígeno por aireación y consumo
 * por sulfito. Integración explícita a paso fijo; el paso lo controla quien llama.
 */
class ProcessModel {
  constructor({
    volumeL = 87.5,          // volumen activo del banco (regla 10: 70x50x25 cm)
    initialDoMgL = 7.5,
    tempC = 24.0,
    timeAcceleration = UNIDENTIFIED.TIME_ACCELERATION
  } = {}) {
    this.volumeL = volumeL;
    this.tempC = tempC;
    this.timeAcceleration = timeAcceleration;

    this.doMgL = initialDoMgL;
    this.doMin = 0.0;         // el OD no es negativo
    this.sulfiteDemandMgL = 0; // demanda de O2 pendiente del sulfito inyectado

    // Entradas del sistema (las fijan los nodos)
    this.aeratorRpm = 0;
    this.mixerOn = false;
    this.tempC = tempC;

    this.simSeconds = 0;
  }

  get saturation() { return doSaturationMgL(this.tempC); }

  /** KLa (1/s) en función de las RPM del aireador. Forma plausible, parámetros inventados. */
  kla() {
    const norm = Math.max(0, Math.min(1, this.aeratorRpm / 600));
    const base = UNIDENTIFIED.KLA_MAX_PER_S * Math.pow(norm, UNIDENTIFIED.KLA_RPM_EXPONENT);
    return this.mixerOn ? base * UNIDENTIFIED.MIXER_KLA_BOOST : base;
  }

  /** El aireador reporta sus RPM (el firmware las manda). */
  setAeratorRpm(rpm) { this.aeratorRpm = Math.max(0, Number(rpm) || 0); }

  /** El mixer solo agita; su efecto aquí es sobre la transferencia y la reacción. */
  setMixer(on) { this.mixerOn = Boolean(on); }

  setTemperature(c) { this.tempC = Number(c); }

  /**
   * Inyección de sulfito de sodio. Convierte la masa dosificada en demanda de oxígeno mediante la
   * estequiometría real. `stockGPerL` reproduce la disolución de 100 g/L de la regla 11.
   */
  injectSulfite(volumeMl, stockGPerL = 100) {
    const massMg = (volumeMl / 1000) * stockGPerL * 1000;
    const demandMgL = massMg / (SULFITE_MG_PER_O2_MG * this.volumeL);
    this.sulfiteDemandMgL += demandMgL;
    return demandMgL;
  }

  /**
   * Avanza el modelo `dtSeconds` de tiempo simulado.
   *   dDO/dt = KLa·(DOsat − DO) − respiración − consumo de sulfito
   *
   * El consumo de sulfito se trata como una demanda total de O2 (mg/L) que se agota a un ritmo
   * proporcional a lo que queda, acelerado por la mezcla y limitado por el oxígeno disponible.
   */
  step(dtSeconds) {
    const dt = Math.max(0, Number(dtSeconds) || 0) * this.timeAcceleration;
    if (dt <= 0) return this.doMgL;

    const sat = this.saturation;
    const transfer = this.kla() * (sat - this.doMgL);   // mg/L/s que entran del aire
    const respire = UNIDENTIFIED.RESPIRE_MG_L_S;        // consumo basal

    // Reacción del sulfito: ritmo proporcional a la demanda restante, acelerado por agitación
    const reactionRate = this.mixerOn ? UNIDENTIFIED.MIXER_REACTION_BOOST : 1.0;
    const wanted = this.sulfiteDemandMgL * UNIDENTIFIED.SULFITE_REACTION_K * reactionRate; // mg/L/s
    const available = this.doMgL / dt + transfer;                 // mg/L/s disponibles
    const applied = Math.max(0, Math.min(wanted, available));

    this.doMgL = Math.max(0, this.doMgL + (transfer - respire - applied) * dt);
    this.sulfiteDemandMgL = Math.max(0, this.sulfiteDemandMgL - applied * dt);

    this.simSeconds += dt;
    return this.doMgL;
  }

  /** Estado observable (para logs y aserciones). */
  snapshot() {
    return {
      doMgL: this.doMgL,
      saturationMgL: this.saturation,
      satPct: this.saturation > 0 ? (this.doMgL / this.saturation) * 100 : 0,
      kla: this.kla(),
      aeratorRpm: this.aeratorRpm,
      mixerOn: this.mixerOn,
      sulfiteDemandMgL: this.sulfiteDemandMgL,
      simSeconds: this.simSeconds
    };
  }
}

module.exports = { ProcessModel, doSaturationMgL, SULFITE_MG_PER_O2_MG, UNIDENTIFIED };
