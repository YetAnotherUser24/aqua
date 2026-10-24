/**
 * Modelo del enlace ESP-NOW entre la boya (OD-Logger) y el controlador del aireador (ODrive).
 *
 * Reproduce el contrato REAL del enlace, que es lo que importa para el lazo de control:
 *  - La trama es el struct `OdTelemetryPacket` de `include/esp_now_packet.h`.
 *  - Emisor: broadcast `FF:FF:FF:FF:FF:FF` con `peerInfo.channel = 0` (auto-seguir al AP).
 *  - Receptor: sin peers registrados, recibe en el canal actual del STA.
 *  - ESP-NOW comparte radio y canal con WiFi: si emisor y receptor no están en el MISMO canal,
 *    todos los paquetes se pierden en silencio. El gemelo lo reproduce para poder detectarlo.
 *  - Latencia real ~1-2 ms por trama MAC-a-MAC directa (sin relay del AP, sin TCP/TLS).
 *  - `status_flags` es una FRONTERA DE CONFIANZA: un bit activo invalida la lectura. En particular
 *    `STATUS_OD_SENSOR_BIT` (22) significa que el sensor falló y el firmware transmite DO=0.000;
 *    el receptor debe ignorar esa muestra para que el watchdog de 35 s frene el motor en lugar de
 *    perseguir un valor fantasma.
 *
 * NO se modela la capa física (RSSI, modulación, contención de canal): eso sólo se valida en banco.
 */
const { statusBits } = require('./constants');

const MAGIC = 0x41515541; // ASCII "AQUA"

/**
 * Tamaño de la trama empaquetada (`#pragma pack(1)`):
 *   magic u32 (4) + sequence u32 (4) + rtc_timestamp_ms u64 (8) + 4 floats (16) + status_flags u32 (4)
 */
const PACKET_SIZE = 36;

/** Construye una trama con el mismo orden de campos que el struct del firmware. */
function buildPacket({ sequence, rtcTimestampMs, dissolvedOxygen, waterTemperature, oxygenSaturation, batteryVoltage, statusFlags }) {
  return {
    magic: MAGIC,
    sequence,
    rtc_timestamp_ms: rtcTimestampMs,
    dissolved_oxygen: dissolvedOxygen,
    water_temperature: waterTemperature,
    oxygen_saturation: oxygenSaturation,
    battery_voltage: batteryVoltage,
    status_flags: statusFlags
  };
}

/** ¿La trama marca la lectura de OD como inválida? (frontera de confianza) */
function isSampleValid(packet) {
  return (packet.status_flags & (1 << statusBits.OD_SENSOR)) === 0;
}

/**
 * Enlace ESP-NOW simulado. Es in-process: el gemelo no necesita radio, pero conserva las
 * condiciones que hacen que el enlace real funcione o falle.
 */
class EspNowLink {
  constructor({ channel = 11, latencyMs = 1.5, jitterMs = 0.5, lossRate = 0, verbose = false } = {}) {
    this.channel = channel;
    this.latencyMs = latencyMs;
    this.jitterMs = jitterMs;
    this.lossRate = lossRate;
    this.verbose = verbose;

    this.receiver = null;         // { channel, onPacket, onRejected }
    this.stats = { sent: 0, delivered: 0, droppedChannel: 0, droppedLoss: 0, rejectedInvalid: 0 };
  }

  /** El receptor se registra sin peers: sólo escucha en el canal actual del STA. */
  registerReceiver({ channel, onPacket, onRejected }) {
    this.receiver = { channel, onPacket, onRejected };
  }

  /**
   * Emisor con `peerInfo.channel = 0`: usa el canal actual. Si el AP cambia de canal, el gemelo
   * debe reflejarlo aquí (es el escenario real que rompe el enlace).
   */
  send(packet, { senderChannel = this.channel } = {}) {
    this.stats.sent++;

    if (packet.magic !== MAGIC) return false;

    if (!this.receiver) return false;

    // Canal distinto -> descarte silencioso, igual que en el hardware.
    if (senderChannel !== this.receiver.channel) {
      this.stats.droppedChannel++;
      return false;
    }

    if (this.lossRate > 0 && Math.random() < this.lossRate) {
      this.stats.droppedLoss++;
      return false;
    }

    const valid = isSampleValid(packet);
    if (!valid) this.stats.rejectedInvalid++;

    // Entrega diferida con la latencia del enlace (no instantánea: afecta a la extrapolación).
    const delay = this.latencyMs + (Math.random() - 0.5) * 2 * this.jitterMs;
    setTimeout(() => {
      this.stats.delivered++;
      if (valid) this.receiver.onPacket(packet);
      else if (this.receiver.onRejected) this.receiver.onRejected(packet);
    }, Math.max(0, delay));

    return true;
  }

  /** Cambia el canal del AP: emisor y receptor deben re-alinearse o el enlace muere. */
  setApChannel(channel, { realignReceiver = true } = {}) {
    this.channel = channel;
    if (realignReceiver && this.receiver) this.receiver.channel = channel;
  }

  /** Fuerza desalineación de canal (inyección de fallo) para probar la detección. */
  misalignReceiver(channel) {
    if (this.receiver) this.receiver.channel = channel;
  }
}

module.exports = { EspNowLink, buildPacket, isSampleValid, MAGIC, PACKET_SIZE, STATUS_OD_SENSOR_BIT: statusBits.OD_SENSOR };
