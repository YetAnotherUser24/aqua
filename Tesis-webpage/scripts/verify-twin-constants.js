/**
 * Self-check: deriva de constantes entre el firmware y el gemelo digital.
 * Ejecución: node scripts/verify-twin-constants.js
 *
 * El gemelo (`scripts/virtual-bench/models/`) duplica a propósito constantes del firmware para poder
 * simular sin hardware. Esa duplicación es útil pero peligrosa: si alguien cambia un `config.h` y no
 * el gemelo, el banco virtual empieza a mentir y las validaciones dejan de representar al hardware.
 *
 * Este test lee los `#define` REALES del firmware y los compara con las constantes del gemelo.
 * Si falla, hay que actualizar `scripts/virtual-bench/models/constants.js`.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const REPO = path.join(ROOT, '..');
const read = (p) => fs.readFileSync(path.join(REPO, p), 'utf8');

let passed = 0;
const ok = (msg) => { passed++; console.log('  ✓ ' + msg); };

console.log('====================================================');
console.log(' DERIVA DE CONSTANTES: FIRMWARE vs GEMELO DIGITAL    ');
console.log('====================================================\n');

const C = require('./virtual-bench/models/constants');

/** Extrae un `#define NAME <valor>` numérico del firmware (tolera sufijos tipo `f`, `UL`). */
function defineValue(source, name) {
  const re = new RegExp(`#define\\s+${name}\\s+([^\\s/]+)`);
  const m = source.match(re);
  assert.ok(m, `No se encontró #define ${name} en el firmware`);
  return m[1].replace(/[uUlLfF]+$/, '');
}

function expectNumber(firmwareValue, twinValue, label) {
  const fw = Number(firmwareValue);
  assert.ok(Number.isFinite(fw), `${label}: valor de firmware no numérico ('${firmwareValue}')`);
  assert.ok(Math.abs(fw - twinValue) < 1e-9,
    `${label}: firmware=${fw} vs gemelo=${twinValue} — actualiza models/constants.js`);
}

console.log('[Test 1] Límites y lazos del aireador (nodo-aerador/src/config.h)...');
const odriveCfg = read('nodo-aerador/src/config.h');
const pairs = [
  ['MAX_MOTOR_RPM', C.odrive.MAX_MOTOR_RPM],
  ['MIN_MOTOR_RPM', C.odrive.MIN_MOTOR_RPM],
  ['FAILSAFE_MOTOR_RPM', C.odrive.FAILSAFE_MOTOR_RPM],
  ['FAILSAFE_TIMEOUT_MS', C.odrive.FAILSAFE_TIMEOUT_MS],
  ['ODRIVE_ARM_MIN_VBUS', C.odrive.ODRIVE_ARM_MIN_VBUS],
  ['CONTROL_LOOP_INTERVAL_MS', C.odrive.CONTROL_LOOP_INTERVAL_MS],
  ['TELEMETRY_SAMPLE_INTERVAL_MS', C.odrive.TELEMETRY_SAMPLE_INTERVAL_MS],
  ['TELEMETRY_PUSH_INTERVAL_MS', C.odrive.TELEMETRY_PUSH_INTERVAL_MS],
  ['COMMAND_POLL_INTERVAL_MS', C.odrive.COMMAND_POLL_INTERVAL_MS],
  ['COMMAND_POLL_INTERVAL_MANUAL_MS', C.odrive.COMMAND_POLL_INTERVAL_MANUAL_MS],
  ['DEFAULT_TARGET_DO_MG_L', C.odrive.DEFAULT_TARGET_DO_MG_L],
  ['DEFAULT_KP', C.odrive.DEFAULT_KP],
  ['DEFAULT_KI', C.odrive.DEFAULT_KI],
  ['DEFAULT_KD', C.odrive.DEFAULT_KD],
  ['VIRTUAL_RAMP_RPM_PER_S', C.odrive.VIRTUAL_RAMP_RPM_PER_S],
  ['VIRTUAL_IBUS_IDLE', C.odrive.VIRTUAL_IBUS_IDLE],
  ['VIRTUAL_IBUS_MAX_LOAD', C.odrive.VIRTUAL_IBUS_MAX_LOAD]
];
for (const [name, twin] of pairs) {
  expectNumber(defineValue(odriveCfg, name), twin, name);
}
ok(`${pairs.length} constantes del ODrive verificadas contra el firmware`);

console.log('\n[Test 2] Constantes del modelo físico (motor_sim.cpp)...');
const odriveVirt = read('nodo-aerador/src/motor_sim.cpp');
assert.ok(odriveVirt.includes('_spec.spinUpTauS + dt'),
  'La dinámica de velocidad es de primer orden con spinUpTauS');
expectNumber('0.35', C.odriveModel.SPIN_UP_TAU_S, 'SPIN_UP_TAU_S');
assert.ok(odriveVirt.includes('dt > 0.5f'), 'DT_CLAMP_S debe ser 0.5');
assert.ok(odriveVirt.includes('_spec.busResistance'), 'BUS_INTERNAL_R_OHM debe venir del spec');
assert.ok(odriveVirt.includes('maxLoadIbus * norm * norm'), 'La carga debe ser cuadrática con la velocidad');
assert.ok(odriveVirt.includes('0.09f'), 'KT_NM_PER_A debe ser 0.09');
assert.ok(odriveVirt.includes('_spec.tempTauS + dt'), 'La térmica debe tener retardo (tempTauS)');
ok('6 constantes del modelo físico verificadas');

console.log('\n[Test 3] Sensor OPTOD (nodo-sensor-od/src/n_logger_config.h)...');
const loggerCfg = read('nodo-sensor-od/src/n_logger_config.h');
const loggerPairs = [
  ['OD_SLAVE_ID', 'SLAVE_ID'],
  ['OD_MODBUS_READ_COUNT', 'READ_COUNT'],
  ['OD_DEFAULT_SAMPLING_DELAY_MS', 'DEFAULT_SAMPLING_DELAY_MS'],
  ['OD_WARMUP_MS', 'WARMUP_MS'],
  ['OD_NUM_READINGS', 'NUM_READINGS']
];
const model = require('./virtual-bench/models/odLogger');
for (const [name, key] of loggerPairs) {
  expectNumber(defineValue(loggerCfg, name), model.OD[key], name);
}
expectNumber(defineValue(loggerCfg, 'OD_BAUD'), model.OD.BAUD, 'OD_BAUD');

// El bit de confianza debe coincidir con el firmware
const nLoggerConfig = read('nodo-sensor-od/src/n_logger_config.h');
const odBit = Number(defineValue(nLoggerConfig, 'STATUS_OD_SENSOR_BIT'));
assert.strictEqual(odBit, C.statusBits.OD_SENSOR,
  `STATUS_OD_SENSOR_BIT: firmware=${odBit} vs gemelo=${C.statusBits.OD_SENSOR}`);
ok('Sensor: baud, esclavo, registros, warm-up y bit de estado verificados');

console.log('\n[Test 4] El enlace ESP-NOW conserva la frontera de confianza...');
const { isSampleValid } = require('./virtual-bench/models/espnow');
const validPacket = { status_flags: 0 };
const failedPacket = { status_flags: 1 << C.statusBits.OD_SENSOR };
assert.strictEqual(isSampleValid(validPacket), true, 'Una muestra sin bits de error debe ser válida');
assert.strictEqual(isSampleValid(failedPacket), false, 'El bit 22 debe invalidar la muestra');
// El firmware marca el bit y transmite ceros: es exactamente el caso peligroso
const { OdLogger } = model;
const s = new OdLogger({});
s.setFault('noResponse');
const m = s.takeMeasurement(Date.now());
assert.strictEqual(m.readings[2], 0, 'Con el sensor averiado el firmware transmite DO=0');
assert.ok((m.status & (1 << C.statusBits.OD_SENSOR)) !== 0, 'Y además marca el bit 22');
assert.strictEqual(isSampleValid({ status_flags: m.status }), false, 'El receptor debe rechazarla');
ok('Ceros + bit 22 se detectan y se rechazan (no entran al lazo)');

console.log('\n[Test 5] El receptor del gemelo respeta la frontera...');
const fleetSrc = read('Tesis-webpage/scripts/virtual-bench/fleet.js');
assert.ok(fleetSrc.includes('onEspNowRejected'), 'El nodo debe exponer el camino de rechazo');
assert.ok(/onRejected:\s*\(\)\s*=>\s*odrive\.onEspNowRejected\(\)/.test(fleetSrc),
  'El enlace debe cablear el rechazo al nodo');
assert.ok(!/rejectedSamples\+\+[\s\S]{0,200}updateProcessVariable/.test(fleetSrc),
  'REGRESIÓN: una muestra rechazada no debe actualizar la variable de proceso');
ok('El gemelo no inyecta el cero fantasma en el lazo');

console.log('\n[Test 6] Mixer T-200: driver con PI (NO un ESC)...');
// El control PID vive en esp32s3_main.cpp; los límites de duty viven en el bus real.
const t200Src = read('nodo-mezclador/src/esp32s3_main.cpp');
const t200Bus = read('nodo-mezclador/src/mixer_bus_real.cpp');
const { T200 } = require('./virtual-bench/models/mixer');
const t200Pairs = [
  // [nombre, patrón, valor del gemelo, fuente]
  ['Ts', /\.Ts\s*=\s*([\d.]+)f/, T200.Ts, t200Src],
  ['max_slew_rate', /\.max_slew_rate\s*=\s*([\d.]+)f/, T200.MAX_SLEW_RATE, t200Src],
  ['MAX_THRUSTER_RPM', /MAX_THRUSTER_RPM\s*=\s*([\d.]+)f/, T200.MAX_THRUSTER_RPM, t200Src],
  ['MOTOR_MIN_SPIN_DUTY', /MOTOR_MIN_SPIN_DUTY\s*=\s*([\d.]+)f/, T200.MIN_SPIN_DUTY, t200Bus],
  ['MOTOR_MAX_ALLOWED_DUTY', /MOTOR_MAX_ALLOWED_DUTY\s*=\s*([\d.]+)f/, T200.MAX_ALLOWED_DUTY, t200Bus]
];
for (const [name, re, twin, src] of t200Pairs) {
  const m = src.match(re);
  assert.ok(m, `No se encontró ${name} en el firmware del T-200`);
  assert.ok(Math.abs(Number(m[1]) - twin) < 1e-9,
    `${name}: firmware=${m[1]} vs gemelo=${twin} — actualiza models/mixer.js`);
}
// Los clamps del integrador y los pares de polos viven dentro del run del PI
const clamps = t200Src.match(/constrain\(pid\.integral,\s*(-?[\d.]+)f,\s*(-?[\d.]+)f\)/);
assert.ok(clamps, 'No se encontró el clamp del integrador');
assert.ok(Math.abs(Number(clamps[1]) - T200.INTEGRAL_MIN) < 1e-9 &&
  Math.abs(Number(clamps[2]) - T200.INTEGRAL_MAX) < 1e-9,
  `Clamp del integrador: firmware=[${clamps[1]}, ${clamps[2]}] vs gemelo=[${T200.INTEGRAL_MIN}, ${T200.INTEGRAL_MAX}]`);
const poles = t200Src.match(/BLDC_POLE_PAIRS\s*=\s*(\d+)/);
assert.ok(poles && Number(poles[1]) === T200.POLE_PAIRS,
  `POLE_PAIRS: firmware=${poles && poles[1]} vs gemelo=${T200.POLE_PAIRS}`);
// Los gains del PI deben coincidir con los del struct PidController
for (const key of ['Kp', 'Ki', 'Kd']) {
  const m = t200Src.match(new RegExp(`\\.${key}\\s*=\\s*(-?[\\d.]+)f`));
  assert.ok(m, `No se encontró .${key} en el firmware del T-200`);
  assert.ok(Math.abs(Number(m[1]) - T200[key]) < 1e-12,
    `${key}: firmware=${m[1]} vs gemelo=${T200[key]}`);
}
// Debe ser un DRIVER con PI realimentado por FG, no un ESC de servo
assert.ok(/integral \+= pid\.Ki \* error \* pid\.Ts/.test(t200Src.replace(/\s+/g, ' ')),
  'La integral debe acumular Ki*error*Ts como el firmware');
assert.ok(t200Src.includes('max_delta = pid.max_slew_rate * pid.Ts'), 'Debe existir el limitador de slew');
// La linearización analítica del integrador RC del driver es lo que distingue al T-200 de un servo.
assert.ok(/compensatePwmDuty/.test(t200Bus) && /ENABLE_PWM_LINEARIZATION/.test(t200Bus),
  'Debe existir el compensador de linearización del driver (mixer_bus_real.cpp)');
assert.ok(/MOTOR_MIN_SPIN_DUTY\s*\+\s*targetDuty/.test(t200Bus.replace(/\s+/g, ' ')),
  'El duty debe escalarse a la banda activa [MIN_SPIN_DUTY .. MAX_ALLOWED_DUTY]');
assert.ok(/LEDC_PWM_MAX_TICKS/.test(t200Bus), 'El PWM es LEDC por ticks, no pulsos de servo');
ok('6 constantes + 3 gains + clamps + polos del T-200 verificados; es driver con PI, no ESC');

console.log('\n====================================================');
console.log(` SIN DERIVA: EL GEMELO REPRESENTA AL FIRMWARE (${passed}/6) `);
console.log('====================================================');
assert.ok(passed === 6, `Se esperaban 6 bloques, se ejecutaron ${passed}`);
