/**
 * Flota virtual de ALTA FIDELIDAD: gemelo digital de los 4 nodos del banco.
 *
 * Cada nodo combina dos capas:
 *   1. MODELOS PORTADOS del firmware real (`models/`): física del ODrive, motor de control PID,
 *      sensor OPTOD con sus modos de fallo, reloj maestro y enlace ESP-NOW.
 *   2. TRANSPORTE HTTP idéntico al del firmware: mismos endpoints, mismos nombres de campo, mismas
 *      cadencias y mismo protocolo de ACK.
 *
 * Lo que el gemelo valida: contrato de orquestación, dinámica del lazo, alineación de relojes,
 * frontera de confianza de `status_flags` y persistencia.
 * Lo que NO valida (sólo se prueba en banco): capa física de radio, RS-485 real, FOC del motor y la
 * hidrodinámica del tanque.
 */
const { DEVICES, CADENCE } = require('./config');
const { ODriveVirtual } = require('./models/odrive');
const { ControlEngine, MODE_PID, MODE_MANUAL } = require('./models/controlEngine');
const { OdLogger } = require('./models/odLogger');
const { MasterClock, ExtrapolatedClock } = require('./models/masterClock');
const { EspNowLink } = require('./models/espnow');
const { ProcessModel } = require('./models/process');
const { MixerDriver, T200 } = require('./models/mixer');
const { odrive: ODRIVE_C } = require('./models/constants');

const nowMs = () => Date.now();

/** Acciones que cada rol EJECUTA: sirve para detectar entrega cruzada. */
const ROLE_ACTIONS = {
  sensor: new Set(['set_state', 'start_monitor', 'start', 'monitor', 'stop_monitor', 'stop',
    'start_experiment', 'stop_experiment', 'manual_sample', 'set_sampling_rate', 'sleep']),
  odrive: new Set(['set_state', 'set_mode', 'set_config', 'force_on', 'force_off', 'emergency_stop',
    'clear_estop', 'resume', 'start', 'stop', 'set_speed', 'reboot']),
  mixer: new Set(['set_state', 'start_mixer', 'stop_mixer', 'set_speed', 'stop']),
  pump: new Set(['set_state', 'dose', 'pump_dose', 'start', 'stop'])
};

/* ============================== Nodo base ============================== */

class BaseNode {
  constructor({ role, api, verbose = false }) {
    this.role = role;
    this.device = DEVICES[role];
    this.api = api;
    this.verbose = verbose;

    this.received = [];
    this.acked = [];
    this.telemetryCount = 0;
    this.errors = [];
    this.systemState = 'IDLE';
    this.experimentId = null;
    this.timers = [];
    this.pollTimers = [];
    this.pollingPaused = false;
    this.inFlightPoll = null;
    this.stopped = false;
  }

  setExperiment(id) { this.experimentId = id; }

  start() {
    this.pollTimers.push(setInterval(() => {
      // Se guarda el sondeo en vuelo para que `pauseCommandPolling()` pueda esperarlo: el servidor
      // marca la orden como `sent` al responder, así que una pausa sin esperar no es una pausa.
      this.inFlightPoll = this.pollCommands().catch(() => {});
    }, CADENCE.commandPollMs));
  }

  /**
   * Detiene el sondeo de comandos sin apagar el nodo (los lazos y la telemetría siguen).
   * El banco lo usa para reproducir de forma determinista la ventana en la que una orden de
   * receta aún está encolada y el operador aborta: sin esto, los nodos podían consumirla antes
   * del override y la prueba medía una carrera en vez del mecanismo.
   *
   * Es `async` a propósito: un sondeo ya lanzado no se puede cancelar, y el servidor marca la
   * orden como `sent` al responderle. Hay que esperar a que termine para que la pausa sea real.
   */
  async pauseCommandPolling() {
    this.pollingPaused = true;
    for (const t of this.pollTimers) clearInterval(t);
    this.pollTimers = [];
    if (this.inFlightPoll) await this.inFlightPoll.catch(() => {});
  }

  resumeCommandPolling() {
    if (!this.pollingPaused) return;
    this.pollingPaused = false;
    this.pollTimers.push(setInterval(() => this.pollCommands(), CADENCE.commandPollMs));
  }

  stop() {
    this.stopped = true;
    for (const t of this.timers) clearInterval(t);
    for (const t of this.pollTimers) clearInterval(t);
    this.timers = [];
    this.pollTimers = [];
  }

  /** Consume los comandos pendientes hasta agotarlos (descarta residuos de corridas anteriores). */
  async drainCommands(maxRounds = 12) {
    let drained = 0;
    for (let i = 0; i < maxRounds; i++) {
      const res = await this.api.deviceGet('/api/commands/pending', this.device.key);
      if (!res.ok || !res.body?.has_command) break;
      drained++;
    }
    return drained;
  }

  async pollCommands() {
    if (this.stopped) return;
    const res = await this.api.deviceGet('/api/commands/pending', this.device.key);
    if (!res.ok || !res.body?.has_command || !res.body.command) return;

    const cmd = res.body.command;
    const payload = cmd.payload && typeof cmd.payload === 'object' ? cmd.payload : {};
    const action = String(payload.action || cmd.command_type || '');

    this.received.push({
      id: cmd.id,
      command_type: cmd.command_type,
      action,
      payload,
      expectsRole: payload.target_role || null,
      at: nowMs()
    });
    if (this.verbose) console.log(`   ← ${this.role}: ${action} ${JSON.stringify(payload).slice(0, 130)}`);

    let extra = {};
    try {
      extra = (await this.applyCommand(action, payload, cmd)) || {};
    } catch (err) {
      this.errors.push(`${action}: ${err.message}`);
    }
    await this.acknowledge(cmd.id, extra);
  }

  async acknowledge(cmdId, extra = {}) {
    if (!cmdId) return;
    const body = { success: true, rtc_timestamp_ms: this.rtcForAck(), message: `virtual-${this.role}`, ...extra };
    const res = await this.api.devicePost(`/api/commands/${cmdId}/acknowledge`, this.device.key, body);
    this.acked.push({ id: cmdId, status: res.status, ok: res.ok, body: res.body });
  }

  /** Instante de ejecución para el ACK. Cada rol lo resuelve con su propio reloj. */
  rtcForAck() { return nowMs(); }

  async applyCommand() { return {}; }

  recordPush(res, kind) {
    this.telemetryCount++;
    if (!res.ok) this.errors.push(`push ${kind} HTTP ${res.status}`);
    else if (res.body?.warning) this.errors.push(`push ${kind} warning: ${res.body.warning}`);
    this.api.log(`${this.role} push ${kind} -> HTTP ${res.status}${res.body?.archived ? ` archived=${res.body.archived}` : ''}${res.body?.warning ? ` warning=${res.body.warning}` : ''}`);
  }

  actionsSeen() { return [...new Set(this.received.map(r => r.action))]; }
  gotAction(a) { return this.received.some(r => r.action === a); }
  foreignActions() { return this.actionsSeen().filter(a => a && !ROLE_ACTIONS[this.role].has(a)); }
  payloadOf(action) { return this.received.find(r => r.action === action)?.payload ?? null; }

  /** Último payload de una acción: refleja el estado actual, no un residuo del paso anterior. */
  lastPayloadOf(action) {
    for (let i = this.received.length - 1; i >= 0; i--) {
      if (this.received[i].action === action) return this.received[i].payload;
    }
    return null;
  }

  summary() {
    return { role: this.role, commands: this.received.length, actions: this.actionsSeen(), telemetry: this.telemetryCount, errors: this.errors };
  }
}

/* ============================ Nodo SENSOR ============================= */

/**
 * Boya OD-Logger: reloj maestro + sensor OPTOD + emisor ESP-NOW.
 * Muestrea a 0.2 Hz en experimento y emite un heartbeat cada 20 s en IDLE (V4 §2.1).
 */
class SensorNode extends BaseNode {
  constructor(opts) {
    super(opts);
    this.clock = new MasterClock({ skewMs: opts.skewMs || 0, rtcAvailable: opts.rtcAvailable !== false });
    this.sensor = new OdLogger({ clock: this.clock, trueDoMgL: opts.initialDoMgL ?? 7.5, trueTempC: opts.trueTempC ?? 24.0 });
    this.link = opts.link || null;
    this.buffer = [];
    this.sampleIntervalMs = CADENCE.sensorSampleMs;
    this.espNowSeq = 0;
    this.lastMeasurement = null;
    /** Planta compartida: si existe, el sensor la muestrea; si no, se usa `setTrueDo` manual. */
    this.process = opts.process || null;
  }

  start() {
    super.start();
    this.timers.push(setInterval(() => this.sampleAndPublish(), this.sampleIntervalMs));
    this.timers.push(setInterval(() => this.flush(), CADENCE.sensorBulkFlushMs));
    this.timers.push(setInterval(() => this.idleHeartbeat(), CADENCE.idleHeartbeatMs));
  }

  rtcForAck() { return this.clock.rtcTimestampMs(nowMs(), nowMs() % 1000); }

  /** Verdad de la planta. Si hay modelo de proceso, éste manda; si no, se fija a mano. */
  setTrueDo(mgL) {
    if (this.process) { this.process.doMgL = Number(mgL); return; }
    this.sensor.setTrueDo(mgL);
  }
  setFault(f) { this.sensor.setFault(f); }

  /** Lee la planta (o el valor fijo) y lo entrega al sensor como "verdad" a muestrear. */
  syncFromProcess() {
    if (!this.process) return;
    this.sensor.setTrueDo(this.process.doMgL);
    this.sensor.setTrueTemp(this.process.tempC);
  }

  sampleAndPublish() {
    if (this.stopped || this.systemState !== 'ACTIVE_EXPERIMENT') return;
    this.syncFromProcess();
    this.measureOnce();
  }

  idleHeartbeat() {
    if (this.stopped || this.systemState === 'ACTIVE_EXPERIMENT') return;
    this.syncFromProcess();
    this.measureOnce();
    this.flush();
  }

  /** Una medición: muestrea, publica por ESP-NOW y encola para el bulk. */
  measureOnce() {
    const m = this.sensor.takeMeasurement(nowMs());
    this.lastMeasurement = m;
    // Verdad de la planta en el instante exacto del muestreo. El OD evoluciona, así que el ítem
    // publicado debe corresponder a ESTE valor y no al que la planta tenga cuando se revise.
    this.plantDoAtSample = this.sensor.trueDoMgL;

    if (this.link) {
      this.link.send({
        magic: 0x41515541,
        sequence: ++this.espNowSeq,
        rtc_timestamp_ms: m.rtcTimestampMs,
        dissolved_oxygen: m.readings[2] / 1000.0,   // milli-mg/L -> mg/L
        water_temperature: m.readings[0] / 100.0,   // centi-°C -> °C
        oxygen_saturation: m.readings[1] / 10.0,    // deci-% -> %
        battery_voltage: m.batteryMv / 1000.0,
        status_flags: m.status
      });
    }

    this.buffer.push(this.sensor.toBulkItem(m));
    if (this.buffer.length >= 12) this.flush();
  }

  async flush() {
    if (this.stopped || this.buffer.length === 0) return;
    const payload = this.buffer.splice(0, 200);
    const res = await this.api.devicePost('/api/telemetry/sensor_bulk', this.device.key, {
      experiment_id: this.systemState === 'ACTIVE_EXPERIMENT' ? (this.experimentId || 'idle') : 'idle',
      payload
    });
    this.recordPush(res, 'sensor_bulk');
  }

  async applyCommand(action, payload) {
    if (!ROLE_ACTIONS.sensor.has(action)) { this.errors.push(`acción ajena al rol: ${action}`); return {}; }

    if (action === 'set_state') {
      this.systemState = String(payload.state || 'IDLE');
      if (this.systemState !== 'ACTIVE_EXPERIMENT') await this.flush();
    }
    if (action === 'set_sampling_rate') {
      const sec = Number(payload.interval_sec || 0);
      if (sec >= 1 && sec <= 60) this.sampleIntervalMs = sec * 1000;
    }
    return {};
  }
}

/* ============================ Nodo ODRIVE ============================= */

/**
 * Controlador del aireador: motor de control REAL + ODrive virtual + receptor ESP-NOW.
 * Lazo a 20 Hz y publicación bulk a 5 Hz, como el firmware.
 */
class ODriveNode extends BaseNode {
  constructor(opts) {
    super(opts);
    this.virtual = new ODriveVirtual({
      nominalVbus: opts.vbusNominal ?? ODRIVE_C.VIRTUAL_VBUS_NOMINAL,
      rampRateRpmPerSec: ODRIVE_C.VIRTUAL_RAMP_RPM_PER_S
    });
    this.virtual.begin();
    this.engine = new ControlEngine({ mode: MODE_PID, derivativeMode: opts.derivativeMode || 'errorDelta' });
    this.clock = new ExtrapolatedClock({ skewMs: opts.skewMs || 0 });

    this.buffer = [];
    this.telemetry = null;
    this.targetRpm = 0;
    this.rejectedSamples = 0;
    this.acceptedSamples = 0;
    /** Planta compartida: el aireador le comunica las RPM que realmente gira. */
    this.process = opts.process || null;
  }

  start() {
    super.start();
    this.timers.push(setInterval(() => this.controlLoop(), ODRIVE_C.CONTROL_LOOP_INTERVAL_MS));
    this.timers.push(setInterval(() => this.sampleTelemetry(), ODRIVE_C.TELEMETRY_SAMPLE_INTERVAL_MS));
    this.timers.push(setInterval(() => this.flush(), ODRIVE_C.TELEMETRY_PUSH_INTERVAL_MS));
    this.timers.push(setInterval(() => this.idleHeartbeat(), CADENCE.idleHeartbeatMs));
  }

  /**
   * `onDataRecv`: SOLO alimenta el lazo si la muestra es válida.
   * Es la frontera de confianza: una lectura marcada como fallida llega con DO=0.000 y no debe
   * entrar al PID (si entrara, el motor iría a fondo persiguiendo un cero fantasma).
   */
  onEspNowPacket(pkt) {
    this.acceptedSamples++;
    this.clock.sync(pkt.rtc_timestamp_ms, nowMs());
    this.engine.updateProcessVariable(pkt.dissolved_oxygen, pkt.water_temperature, nowMs());
  }

  onEspNowRejected() {
    this.rejectedSamples++;
    // No se refresca la variable de proceso: el watchdog de 35 s frenará el motor.
  }

  rtcForAck() { return this.clock.currentRtcMs(nowMs()); }

  /** Lazo determinista de 20 Hz (Core 1). */
  controlLoop() {
    if (this.stopped) return;
    const now = nowMs();
    const telem = this.virtual.pollTelemetry(now);
    this.telemetry = telem;

    if (this.engine.isEmergencyStopActive()) {
      this.targetRpm = 0;
      this.virtual.setVelocityRpm(0);
      this.virtual.enterIdle();
      if (this.process) this.process.setAeratorRpm(0);
      return;
    }

    // Re-armado si está desarmado y el bus está vivo (mismo criterio que main.cpp).
    // El umbral es ODRIVE_ARM_MIN_VBUS, no la tensión nominal: con 12 V nominales el bus queda por
    // debajo de 12.0 en cuanto hay consumo y el motor nunca volvería a armarse.
    if (!telem.is_armed && telem.vbus_voltage > ODRIVE_C.ODRIVE_ARM_MIN_VBUS) {
      this.virtual.enterClosedLoop();
    }
    this.targetRpm = this.engine.computeOutputRpm(now);
    this.virtual.setVelocityRpm(this.targetRpm);

    // El aireador comunica a la planta las RPM que realmente está girando (no la consigna).
    if (this.process) this.process.setAeratorRpm(this.virtual.state.actualRpm);
  }

  sampleTelemetry() {
    if (this.stopped || !this.telemetry) return;
    const t = this.telemetry;
    const speedPercent = Math.min(100, Math.max(0, (t.actual_rpm / ODRIVE_C.MAX_MOTOR_RPM) * 100));
    this.lastSample = {
      is_on: t.actual_rpm > 0.5,
      speed_percent: Math.round(speedPercent * 10) / 10,
      pwm_us: Math.round(t.actual_rpm),
      actual_rpm: Math.round(t.actual_rpm * 10) / 10,
      target_rpm: Math.round(this.targetRpm * 10) / 10,
      voltage_v: Math.round(t.vbus_voltage * 100) / 100,
      current_a: Math.round(t.ibus_current * 100) / 100,
      power_w: Math.round(t.power_watts * 10) / 10,
      torque_nm: Math.round(t.torque_estimate * 10000) / 10000,
      iq_a: Math.round(t.ibus_current * 10000) / 10000,
      status_code: t.axis_error,
      rtc_timestamp_ms: this.clock.currentRtcMs(nowMs()),
      // Sólo para replicar el promediado del lote en flush(); se eliminan antes de publicar.
      _ibus_current: t.ibus_current,
      _power_watts: t.power_watts
    };
    this.buffer.push(this.lastSample);
    if (this.buffer.length >= 25) this.flush();
  }

  idleHeartbeat() {
    if (this.stopped || this.systemState === 'ACTIVE_EXPERIMENT') return;
    this.sampleTelemetry();
    this.flush();
  }

  async flush() {
    if (this.stopped || this.buffer.length === 0) return;
    const batch = this.buffer.splice(0, 200);

    // Fiel a cloud_worker.cpp: las medidas ruidosas del bus DC (shunt conmutado por PWM) se
    // promedian sobre TODO el lote antes de subirlas. `iq_a` y `torque_nm` NO se promedian.
    const n = batch.length;
    const avgIbus = batch.reduce((a, s) => a + s._ibus_current, 0) / n;
    const avgPower = batch.reduce((a, s) => a + s._power_watts, 0) / n;
    const payload = batch.map(({ _ibus_current, _power_watts, ...item }) => ({
      ...item,
      current_a: Math.round(avgIbus * 100) / 100,
      power_w: Math.round(avgPower * 10) / 10
    }));
    const res = await this.api.devicePost('/api/telemetry/motor_bulk', this.device.key, {
      experiment_id: this.systemState === 'ACTIVE_EXPERIMENT' ? (this.experimentId || 'backend_resolved') : 'idle',
      payload
    });
    this.recordPush(res, 'motor_bulk');
  }

  async applyCommand(action, payload, cmd) {
    if (!ROLE_ACTIONS.odrive.has(action)) { this.errors.push(`acción ajena al rol: ${action}`); return {}; }

    if (action === 'set_state') {
      const state = String(payload.state || 'IDLE');
      this.systemState = state;
      const mode = String(payload.motor_mode || 'off');
      const cfg = this.engine.getConfig();

      if (state === 'ACTIVE_EXPERIMENT' && mode === 'pid') {
        this.engine.configure({ ...cfg, mode: MODE_PID, target_do_mg_l: payload.motor_target_do ?? cfg.target_do_mg_l });
      } else if (state === 'ACTIVE_EXPERIMENT' && mode === 'manual') {
        // Fiel al firmware: se usa configure() para NO liberar un E-Stop latcheado.
        this.engine.configure({ ...cfg, mode: MODE_MANUAL, manual_throttle_pct: payload.motor_throttle_pct ?? 0 });
      } else {
        this.engine.configure({ ...cfg, mode: MODE_MANUAL, manual_throttle_pct: 0 });
      }
      return { rtc_timestamp_ms: this.rtcForAck() };
    }

    if (action === 'set_mode' || action === 'set_config') {
      const cfg = this.engine.getConfig();
      if (payload.mode) cfg.mode = payload.mode === 'pid' ? MODE_PID : MODE_MANUAL;
      if (payload.target_do !== undefined) cfg.target_do_mg_l = Number(payload.target_do);
      if (payload.kp !== undefined) cfg.kp = Number(payload.kp);
      if (payload.ki !== undefined) cfg.ki = Number(payload.ki);
      if (payload.kd !== undefined) cfg.kd = Number(payload.kd);
      if (payload.manual_throttle_pct !== undefined) cfg.manual_throttle_pct = Number(payload.manual_throttle_pct);
      this.engine.configure(cfg);
      return { actual_speed_percent: this.engine.getConfig().manual_throttle_pct };
    }

    if (action === 'force_on') { this.engine.setManualThrottle(Number(payload.manual_throttle_pct ?? 50)); return {}; }
    if (action === 'force_off' || action === 'stop') { this.engine.setManualThrottle(0); return {}; }
    if (action === 'emergency_stop') { this.engine.triggerEmergencyStop(); return {}; }
    if (action === 'clear_estop' || action === 'resume') { this.engine.clearEmergencyStop(); return {}; }
    if (action === 'set_speed' || action === 'start') {
      const pct = Number(cmd?.speed_percent ?? payload.speed_percent ?? 0);
      this.engine.setManualThrottle(pct);
      return { actual_speed_percent: pct };
    }
    return {};
  }

  /** Estado del lazo para las aserciones del banco. */
  loopState() {
    return {
      targetRpm: this.targetRpm,
      actualRpm: this.virtual.state.actualRpm,
      mode: this.engine.getConfig().mode,
      failsafe: this.engine.isFailsafeActive(),
      eStop: this.engine.isEmergencyStopActive(),
      accepted: this.acceptedSamples,
      rejected: this.rejectedSamples
    };
  }
}

/* ============================ Nodo MIXER ============================= */

/**
 * Mixer: T-200 con **driver SNR8503M** y lazo **PI cerrado** con realimentación por tacómetro FG.
 * NO es un ESC: el lazo corre a 100 Hz dentro del controlador y la salida es un duty PWM que comanda
 * al driver. Todo el modelo (gains, clamps, slew, umbrales y curva de parada) vive en
 * `models/mixer.js`, portado del firmware.
 */
class MixerNode extends BaseNode {
  constructor(opts) {
    super(opts);
    this.driver = new MixerDriver();
    this.mixerEvents = [];
    this.lastTelemetry = null;
    /** Planta compartida: la agitación mejora la transferencia de oxígeno. */
    this.process = opts.process || null;
  }

  start() {
    super.start();
    // El firmware corre el lazo a 100 Hz y publica telemetría cada 3000 ms.
    this.timers.push(setInterval(() => this.controlLoop(), T200.Ts * 1000));
    this.timers.push(setInterval(() => this.postTelemetry(), 3000));
  }

  /** Un paso del PI real. */
  controlLoop() {
    if (this.stopped) return;
    this.driver.step(nowMs());
  }

  /** Compatibilidad con las aserciones del banco. */
  get targetRpm() { return (this.driver.targetRadS / (2 * Math.PI)) * 60; }
  get actualRpm() { return this.driver.rpm; }
  get duty() { return this.driver.commandedDuty; }

  /** El mixer va al 600 RPM por defecto (dentro del tope de 3800 RPM del T-200). */
  static get DEFAULT_RPM() { return 600; }

  async setMixer(on) {
    this.driver.setTargetRadS(on ? (MixerNode.DEFAULT_RPM * 2 * Math.PI) / 60 : 0);
    if (this.process) this.process.setMixer(on);
    const res = await this.api.devicePost('/api/events/mixer', this.device.key, {
      experiment_id: this.experimentId || 'idle',
      event_type: on ? 'start_mixer' : 'stop_mixer',
      status: 'ok',
      rtc_timestamp_ms: nowMs()
    });
    this.mixerEvents.push({ on, status: res.status });
  }

  /** Telemetría completa del T-200: exactamente los campos que envía el firmware. */
  async postTelemetry() {
    if (this.stopped) return;
    const payload = this.driver.telemetry(nowMs(),
      this.systemState === 'ACTIVE_EXPERIMENT' && this.experimentId ? this.experimentId : 'idle');
    this.lastTelemetry = payload;
    const res = await this.api.devicePost('/api/telemetry/motor', this.device.key, payload);
    this.recordPush(res, 'motor');
  }

  async applyCommand(action, payload, cmd) {
    if (!ROLE_ACTIONS.mixer.has(action)) { this.errors.push(`acción ajena al rol: ${action}`); return {}; }

    if (action === 'start_mixer') { await this.setMixer(true); return {}; }
    if (action === 'stop_mixer') { await this.setMixer(false); return {}; }

    if (action === 'set_state') {
      const state = String(payload.state || 'IDLE');
      this.systemState = state;
      const mix = String(payload.mixer || '');
      if (state === 'IDLE' || state === 'MANUAL_OVERRIDE') await this.setMixer(false);
      else if (mix === 'on') await this.setMixer(true);
      else if (mix === 'off') await this.setMixer(false);
      // ACTIVE_EXPERIMENT sin campo `mixer`: no se toca el actuador.
      return {};
    }

    if (action === 'set_speed') {
      const pct = Number(payload.speed_percent ?? cmd?.speed_percent ?? 0);
      this.driver.setTargetPercent(pct);
      return {};
    }
    return {};
  }
}

/* ============================= Nodo PUMP ============================== */

/** Bomba peristáltica: dosificación volumétrica con encoder AS5600 y timeout anti-sobredosificación. */
const PUMP = {
  ML_PER_REV: 1.0,
  DOSE_TIMEOUT_MS: 60000,
  MIN_DUTY: 30,
  /** Concentración de la disolución madre de Na2SO3 (regla 11: 100 g/L). */
  STOCK_G_PER_L: 100
};

class PumpNode extends BaseNode {
  constructor(opts) {
    super(opts);
    this.dosedMl = 0;
    this.targetMl = 0;
    this.duty = 0;
    this.dosing = false;
    this.doseStartedAt = 0;
    this.pumpEvents = [];
    this.buffer = [];
    /** Planta compartida: la dosis inyectada consume oxígeno. */
    this.process = opts.process || null;
  }

  start() {
    super.start();
    this.timers.push(setInterval(() => this.dosingLoop(), 50));
    this.timers.push(setInterval(() => this.flush(), 5000));
  }

  /**
   * Lazo de dosificación: integra revoluciones del encoder y corta al alcanzar el objetivo.
   * La masa inyectada se entrega a la planta por estequiometría real (Na2SO3 + ½O2).
   */
  dosingLoop() {
    if (this.stopped) return;
    if (!this.dosing) { this.duty = 0; return; }

    if (Date.now() - this.doseStartedAt > PUMP.DOSE_TIMEOUT_MS) {
      this.finishDose('timeout');
      return;
    }

    // A 100% de duty la bomba gira ~2 rev/s
    const revs = (this.duty / 100) * 2 * 0.05;
    const deltaMl = revs * PUMP.ML_PER_REV;
    this.dosedMl += deltaMl;
    if (this.process && deltaMl > 0) this.process.injectSulfite(deltaMl, PUMP.STOCK_G_PER_L);

    if (this.targetMl > 0 && this.dosedMl >= this.targetMl) {
      this.finishDose('completed');
      return;
    }
    this.duty = PUMP.MIN_DUTY;
  }

  async dose(volumeMl) {
    this.targetMl = Number(volumeMl) || 0;
    this.dosedMl = 0;
    this.dosing = true;
    this.doseStartedAt = Date.now();
  }

  async finishDose(status) {
    this.dosing = false;
    this.duty = 0;
    const res = await this.api.devicePost('/api/events/pump', this.device.key, {
      experiment_id: this.experimentId || 'idle',
      event_type: 'dose_pump',
      volume_ml: Math.round(this.dosedMl * 100) / 100,
      target_ml: this.targetMl,
      status,
      rtc_timestamp_ms: nowMs()
    });
    this.pumpEvents.push({ volumeMl: this.dosedMl, status });
  }

  sampleTelemetry() {
    const speedPercent = this.dosing ? this.duty : 0;
    this.buffer.push({
      is_on: this.dosing,
      speed_percent: Math.round(speedPercent * 10) / 10,
      actual_rpm: Math.round(this.duty * 10) / 10,
      target_rpm: this.targetMl,
      pwm_us: Math.round((speedPercent / 100) * 400) + 1500,
      status_code: 0,
      dosed_ml: Math.round(this.dosedMl * 100) / 100,
      rtc_timestamp_ms: nowMs()
    });
  }

  async flush() {
    if (this.stopped) return;
    this.sampleTelemetry();
    if (this.buffer.length === 0) return;
    const payload = this.buffer.splice(0, 200);
    const res = await this.api.devicePost('/api/telemetry/motor_bulk', this.device.key, {
      experiment_id: 'backend_resolved',
      payload
    });
    this.recordPush(res, 'motor_bulk');
  }

  async applyCommand(action, payload) {
    if (!ROLE_ACTIONS.pump.has(action)) { this.errors.push(`acción ajena al rol: ${action}`); return {}; }

    if (action === 'set_state') {
      this.systemState = String(payload.state || 'IDLE');
      if (this.systemState !== 'ACTIVE_EXPERIMENT') { this.dosing = false; this.duty = 0; }
      return {};
    }
    if (action === 'dose' || action === 'pump_dose') {
      await this.dose(payload.target_ml ?? payload.volume_ml ?? payload.target_volume_ml ?? 0);
      return {};
    }
    return {};
  }
}

/* =============================== Flota =============================== */

/**
 * Construye la flota, la planta y el enlace ESP-NOW que une boya y controlador.
 *
 * La PLANTA (`models/process.js`) es opcional: cierra el ciclo observable del sistema (aireador
 * sube el OD, sulfito lo baja) para poder comprobar que todo trabaja coordinado. Sus parámetros de
 * transferencia son INVENTADOS y su salida no es evidencia experimental (ver la advertencia del
 * propio módulo). Si no se pasa `process`, los escenarios fijan el OD a mano con `setTrueDo`.
 *
 * El canal de ESP-NOW se comparte con el AP: si se desalinea, todos los paquetes se pierden
 * (igual que en banco).
 */
function buildFleet(api, verbose, options = {}) {
  const link = new EspNowLink({
    channel: options.apChannel ?? 11,
    latencyMs: options.espNowLatencyMs ?? 1.5,
    lossRate: options.espNowLossRate ?? 0,
    verbose
  });

  // Planta compartida por los 4 nodos (opcional).
  const process = options.process || null;
  const shared = { ...options, link, process };

  const sensor = new SensorNode({ role: 'sensor', api, verbose, ...shared });
  const odrive = new ODriveNode({ role: 'odrive', api, verbose, ...shared });
  const mixer = new MixerNode({ role: 'mixer', api, verbose, ...shared });
  const pump = new PumpNode({ role: 'pump', api, verbose, ...shared });

  link.registerReceiver({
    channel: link.channel,
    onPacket: (pkt) => odrive.onEspNowPacket(pkt),
    onRejected: () => odrive.onEspNowRejected()
  });

  return { fleet: { sensor, odrive, mixer, pump }, link, process };
}

module.exports = {
  buildFleet, BaseNode, SensorNode, ODriveNode, MixerNode, PumpNode,
  ROLE_ACTIONS, PUMP, T200
};
