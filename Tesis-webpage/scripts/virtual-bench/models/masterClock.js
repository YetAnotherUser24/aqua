/**
 * Modelo del RELOJ MAESTRO (ADR-3) y de los desfases de reloj entre nodos.
 *
 * Convención del proyecto: `rtc_timestamp_ms` es SIEMPRE epoch Unix UTC en milisegundos.
 *
 *  - El OD-Logger es el reloj maestro local (DS3231). Su RTC está en hora local del Perú, así que
 *    al publicar SUMA `LOCAL_TZ_OFFSET_SEC` para entregar UTC.
 *    (`measurement_storage.cpp`: `((secondsSince2000 + UNIX_OFFSET_2000 + LOCAL_TZ_OFFSET_SEC)*1000)
 *     + millis()%1000`)
 *  - Si la fecha del RTC es inválida (< 2020) cae a la hora de sistema sincronizada por NTP.
 *    (`wifi_http.cpp::buildSampleObject`)
 *  - Los nodos NTP-only (T-200, bomba) ya están en UTC y no aplican offset.
 *  - El ODrive NO tiene RTC: extrapola desde el último paquete ESP-NOW.
 *    (`cloud_worker.cpp::currentRtcMs`: `sync.rtc_timestamp_ms + (millis() - sync.received_at_ms)`)
 *
 * Este modelo permite inyectar desfase por nodo, porque en el banco real se midió +7..+16 s de skew
 * en el mixer y el backend debe ancorar al servidor cuando |skew| > 5 s.
 */
const { logger: L, UNIX_OFFSET_2000 } = require('./constants');

/** Reloj de la boya: local (Perú) -> UTC epoch ms, exactamente como el firmware. */
class MasterClock {
  constructor({ skewMs = 0, rtcAvailable = true, ntpSynced = true, useLocalOffset = true } = {}) {
    this.skewMs = skewMs;
    this.rtcAvailable = rtcAvailable;
    this.ntpSynced = ntpSynced;
    this.useLocalOffset = useLocalOffset;
  }

  /**
   * `rtc_timestamp_ms` del reloj maestro para un instante de pared.
   * @param {number} wallNowMs epoch real (lo que el backend considera correcto)
   * @param {number} millisRemainder parte sub-segundo del `millis()` del nodo
   */
  rtcTimestampMs(wallNowMs, millisRemainder = 0) {
    // RTC local del banco: hora local = UTC - offset. El firmware suma el offset para volver a UTC.
    const localMs = wallNowMs - (this.useLocalOffset ? L.LOCAL_TZ_OFFSET_SEC * 1000 : 0);

    // RTC ausente o fecha inválida (< 2020): cae al reloj de sistema (NTP), que ya es UTC.
    const rtcValid = this.rtcAvailable && localMs >= Date.UTC(2020, 0, 1);
    const baseMs = (rtcValid || this.ntpSynced)
      ? (rtcValid ? localMs + (this.useLocalOffset ? L.LOCAL_TZ_OFFSET_SEC * 1000 : 0) : wallNowMs)
      : wallNowMs;

    // `secondsSince2000` es la magnitud que realmente viaja en el firmware.
    const secondsSince2000 = Math.floor(baseMs / 1000) - UNIX_OFFSET_2000;
    const rebuilt = (secondsSince2000 + UNIX_OFFSET_2000) * 1000;

    return rebuilt + (millisRemainder % 1000) + this.skewMs;
  }

  /** `datetime` ISO con el sufijo local que emite `buildSampleObject` (para el respaldo legacy). */
  isoLocal(wallNowMs) {
    const d = new Date(wallNowMs - L.LOCAL_TZ_OFFSET_SEC * 1000);
    return d.toISOString().replace('Z', '-05:00');
  }
}

/**
 * Reloj del nodo sin RTC (ODrive, mixer, bomba).
 * Extrapola desde el último paquete del reloj maestro; devuelve 0 si nunca ha recibido uno
 * (el backend entonces usa la hora del servidor).
 */
class ExtrapolatedClock {
  constructor({ skewMs = 0 } = {}) {
    this.skewMs = skewMs;
    this.lastRtcMs = 0;
    this.lastReceivedAtNodeMs = 0;
    this.hasSync = false;
  }

  /** Se llama al recibir un paquete ESP-NOW (o un ACK de sincronización). */
  sync(rtcMs, nodeNowMs) {
    this.lastRtcMs = rtcMs;
    this.lastReceivedAtNodeMs = nodeNowMs;
    this.hasSync = true;
  }

  /** `currentRtcMs()`: extrapolación monótona desde la última sincronización. */
  currentRtcMs(nodeNowMs) {
    if (!this.hasSync) return 0;
    return this.lastRtcMs + (nodeNowMs - this.lastReceivedAtNodeMs) + this.skewMs;
  }
}

module.exports = { MasterClock, ExtrapolatedClock };
