/**
 * Port fiel del nodo OD-Logger: sensor óptico OPTOD (DIGISENS) sobre Modbus RTU / RS-485.
 *
 * Fuentes:
 *   - `src/sensor_engine.cpp`  -> `odReadAll()` (secuencia Modbus, decodificación, plausibilidad)
 *   - `src/measurement_storage.cpp` -> `takeMeasurement()` (empaquetado y bits de estado)
 *   - `src/n_logger_config.h`  -> constantes
 *
 * El modelo NO inventa valores de oxígeno: recibe la "verdad" de la planta por `setTrueDo()` y aplica
 * encima lo que el hardware real hace con ella — cuantización, retardos, y SOBRE TODO los modos de
 * fallo. Esos fallos son la parte que más importa, porque una lectura fallida viaja como `DO=0.000`
 * con `STATUS_OD_SENSOR_BIT` activo y el receptor DEBE descartarla (si no, el lazo persigue un 0
 * fantasma y lleva el motor a fondo).
 */
const { statusBits, logger: L } = require('./constants');
const { MasterClock } = require('./masterClock');

/** Constantes de `n_logger_config.h`. */
const OD = {
  BAUD: 9600,
  SLAVE_ID: 10,
  SAMPLE_COUNT: 1,
  REGISTER_ORDER: 0x0001,
  ORDER_VALUE: 0x001F,
  START_ADDR: 0x0053,
  READ_COUNT: 10,
  SAMPLING_DELAY_REG: 0x00A4,
  DEFAULT_SAMPLING_DELAY_MS: 250,
  WARMUP_MS: 1500,
  INA226_WARMUP_MS: 200,
  NUM_READINGS: 5,
  // De `modbus.configure(cfg)` en odReadAll()
  RESPONSE_TIMEOUT_MS: 500,
  TX_ENABLE_GUARD_US: 250,
  TX_DISABLE_GUARD_US: 250,
  INTER_FRAME_GAP_MS: 4,
  RX_TURNAROUND_US: 200,
  SAMPLE_TIMEOUT_MS: 5000
};

/** Códigos de error que el DIGISENS devuelve como valor de medición. */
const DIGISENS_ERROR_CODES = [9999, 9998, 9996];

function isDigisensMeasurementError(value) {
  return DIGISENS_ERROR_CODES.includes(Math.trunc(value));
}

/** `isPlausibleMeasurement`: rango físico admisible + no NaN/Inf. */
function isPlausibleMeasurement(temperature, od) {
  return Number.isFinite(temperature) && Number.isFinite(od) &&
    temperature >= -40.0 && temperature <= 125.0 && od >= 0.0 && od <= 500.0;
}

const setBit = (value, bitIndex, set) => set ? (value | (1 << bitIndex)) : (value & ~(1 << bitIndex));

/**
 * Fallos inyectables. `none` es el caso normal.
 *  - 'noResponse'    : el trigger Modbus no obtiene respuesta -> `nread == 0` -> ceros + bit 22
 *  - 'digisensError' : el sensor responde con código de error (9999)
 *  - 'implausible'   : la trama se lee pero la medición no es plausible
 *  - 'busy'          : responde lento y agota el timeout de muestreo de 5 s
 */
const FAULTS = ['none', 'noResponse', 'digisensError', 'implausible', 'busy'];

class OdLogger {
  constructor({ clock = new MasterClock(), fault = 'none', trueDoMgL = 7.5, trueTempC = 24.0 } = {}) {
    this.clock = clock;
    this.fault = fault;
    this.trueDoMgL = trueDoMgL;
    this.trueTempC = trueTempC;

    this.sensorPowered = false;
    this.lastMeasurement = null;
    this.measurements = 0;
    this.faultsInjected = 0;
    this.samplingDelayMs = OD.DEFAULT_SAMPLING_DELAY_MS;
    this.seqCounter = 0;
  }

  /** La planta (escenario) fija la verdad; el sensor la muestrea. */
  setTrueDo(mgL) { this.trueDoMgL = mgL; }
  setTrueTemp(c) { this.trueTempC = c; }
  setFault(fault) {
    if (!FAULTS.includes(fault)) throw new Error(`Fallo desconocido: ${fault}`);
    if (fault !== 'none' && fault !== this.fault) this.faultsInjected++;
    this.fault = fault;
  }

  /**
   * Una lectura completa del sensor, equivalente a `odReadAll()` + `sensorReadAll()`.
   * @returns {{ ok: boolean, status: number, readings: number[], errorCode: number|null }}
   */
  readModbus() {
    // 1. Encendido y warm-up (sólo la primera lectura tras alimentar el sensor)
    if (!this.sensorPowered) {
      this.sensorPowered = true;
      if (this.fault === 'noResponse') { /* el warm-up ocurre igual */ }
    }

    // 2. Fallos que impiden obtener medidas
    if (this.fault === 'noResponse' || this.fault === 'busy') {
      // `nread == 0` -> el firmware marca el bit y escribe CEROS en las 5 lecturas.
      return {
        ok: false,
        status: setBit(0, statusBits.OD_SENSOR, true),
        readings: new Array(OD.NUM_READINGS).fill(0),
        errorCode: this.fault === 'busy' ? -3 : -2
      };
    }

    // 3. Trigger OK -> lectura de los 10 registros
    const doValue = this.trueDoMgL;
    const tempValue = this.trueTempC;
    // Saturación coherente con temperatura de 24 °C (no es un modelo de solubilidad completo).
    const satValue = Math.min(150, (doValue / 8.39) * 100);

    let decodedTemp = tempValue;
    let decodedDo = doValue;

    if (this.fault === 'digisensError') {
      // El sensor responde 9999 -> el firmware aborta con -6
      decodedDo = 9999;
    } else if (this.fault === 'implausible') {
      decodedDo = 700.0; // fuera de rango (0..500) -> implausible
    }

    // 3b. Comprobaciones del firmware, en el mismo orden
    if (isDigisensMeasurementError(Math.trunc(decodedTemp)) || isDigisensMeasurementError(Math.trunc(decodedDo))) {
      return this.failedRead(-6);
    }
    if (!isPlausibleMeasurement(decodedTemp, decodedDo)) {
      return this.failedRead(-6);
    }

    // 4. Escalado a enteros, idéntico a `odReadAll()`
    const readings = [
      Math.round(decodedTemp * 100),   // [0] centi °C
      Math.round(satValue * 10),       // [1] deci %Sat
      Math.round(decodedDo * 1000),    // [2] milli-mg/L  (8456 = 8.456 mg/L)
      Math.round(decodedDo * 100),     // [3] centi ppm
      0                                // [4]
    ];

    return { ok: true, status: 0, readings, errorCode: null };
  }

  failedRead(errorCode) {
    return {
      ok: false,
      status: setBit(0, statusBits.OD_SENSOR, true),
      readings: new Array(OD.NUM_READINGS).fill(0),
      errorCode
    };
  }

  /**
   * `takeMeasurement()`: temperatura (RTC con fallback), batería y construcción de la muestra.
   * @returns datos listos para el payload bulk / el paquete ESP-NOW
   */
  takeMeasurement(wallNowMs) {
    const rtc = this.clock.rtcTimestampMs(wallNowMs, wallNowMs % 1000);
    const bus = this.readModbus();
    this.measurements++;

    let status = bus.status;
    // Temperatura del RTC: si no hay RTC se usa 25.0 °C de respaldo (bare ESP32 / virtual).
    const rtcAvailable = this.clock.rtcAvailable;
    const rtcTempCenti = rtcAvailable ? Math.round(this.trueTempC * 100) : 2500;
    if (rtcAvailable && (rtcTempCenti < -4000 || rtcTempCenti > 8500)) {
      status = setBit(status, statusBits.TEMPERATURE, true);
    }

    // Batería: el env lora32 usa ADC; valor plausible del banco.
    const batteryMv = 4100;

    this.lastMeasurement = {
      wallNowMs,
      rtcTimestampMs: rtc,
      datetime: this.clock.isoLocal(wallNowMs),
      secondsSince2000: Math.floor(rtc / 1000) - 946684800,
      readings: bus.readings,
      rtcTempCenti,
      batteryMv,
      status,
      ok: bus.ok,
      errorCode: bus.errorCode
    };
    return this.lastMeasurement;
  }

  /** Ítem del payload bulk de `/api/telemetry/sensor_bulk` (campos de `buildSampleObject`). */
  toBulkItem(m) {
    const r = m.readings;
    return {
      datetime: m.datetime,
      seconds_since_2000: m.secondsSince2000,
      rtc_timestamp_ms: m.rtcTimestampMs,
      water_temp_centi: r[0],
      do_sat_deci_pct: r[1],
      do_milli_mg_l: r[2],
      param3_centi: r[3],
      param4_centi: r[4],
      battery_mv: m.batteryMv,
      rtc_temp_centi: m.rtcTempCenti,
      status: m.status,
      sent: true
    };
  }
}

module.exports = {
  OdLogger,
  OD,
  FAULTS,
  isDigisensMeasurementError,
  isPlausibleMeasurement,
  setBit
};
