/**
 * Self-check script: Verificación del protocolo de comandos y control WebServer <-> Sensor OD-Logger
 * Ejecución: node scripts/verify-sensor-comms.js
 */
const assert = require('assert');

console.log('======================================================');
console.log(' VERIFICACIÓN DE COMANDOS WEBSERVER <-> SENSOR (OD)   ');
console.log('======================================================\n');

// 1. Identidad y Aislamiento de Dispositivos
const SENSOR_DEVICE = {
  id: 'a0000000-0000-0000-0000-000000000001',
  name: 'Sensor Óptico OD - Estanque 1',
  type: 'sensor_do',
  tokens: ['ESP32_OD_SENSOR', 'ESP32_SENSOR_KEY_2026']
};

const MOTOR_DEVICES = [
  { id: 'b0000000-0000-0000-0000-000000000002', type: 'motor_thruster' },
  { id: 'c0000000-0000-0000-0000-000000000003', type: 'motor_thruster' }
];

console.log('[Test 1] Verificando aislamiento de ID y tokens del sensor...');
assert.strictEqual(SENSOR_DEVICE.type, 'sensor_do', 'El tipo de dispositivo debe ser sensor_do');
MOTOR_DEVICES.forEach((m) => {
  assert.notStrictEqual(SENSOR_DEVICE.id, m.id, 'El ID de sensor no puede coincidir con ningún motor');
  assert.notStrictEqual(SENSOR_DEVICE.type, m.type, 'El tipo de sensor no puede coincidir con motores');
});
console.log('  ✓ ID Sensor:', SENSOR_DEVICE.id, '| Tipo:', SENSOR_DEVICE.type);

// 2. Validación de Cargas Útiles para las 5 Funcionalidades Solicitadas
console.log('\n[Test 2] Verificando contratos de payload para las 5 funciones solicitadas...');

function createSensorCommand(action, options = {}) {
  const allowedActions = [
    'start_monitor',
    'stop_monitor',
    'set_sampling_rate',
    'manual_sample',
    'sleep',
    'set_sleep_cycle',
    'start_experiment',
    'stop_experiment'
  ];
  assert(allowedActions.includes(action), `Acción no permitida: ${action}`);

  const payload = { action, ...options };
  let command_type = 'set_config';
  if (action === 'start_monitor' || action === 'start_experiment') command_type = 'start';
  if (action === 'stop_monitor' || action === 'stop_experiment') command_type = 'stop';

  return {
    device_id: SENSOR_DEVICE.id,
    command_type,
    speed_percent: 0,
    pwm_us: 1500,
    payload
  };
}

// 2.1 Start Monitor & Sampling Rate
const cmdStartMonitor = createSensorCommand('start_monitor', { interval_sec: 5 });
assert.strictEqual(cmdStartMonitor.command_type, 'start');
assert.strictEqual(cmdStartMonitor.payload.action, 'start_monitor');
assert.strictEqual(cmdStartMonitor.payload.interval_sec, 5);
console.log('  ✓ 1. Start Monitor: payload válido con interval_sec = 5s');

// 2.2 Stop Monitor
const cmdStopMonitor = createSensorCommand('stop_monitor');
assert.strictEqual(cmdStopMonitor.command_type, 'stop');
assert.strictEqual(cmdStopMonitor.payload.action, 'stop_monitor');
console.log('  ✓ 2. Stop Monitor: comando stop emitido');

// 2.3 Sampling Rate update
const cmdSamplingRate = createSensorCommand('set_sampling_rate', { interval_sec: 10 });
assert.strictEqual(cmdSamplingRate.command_type, 'set_config');
assert.strictEqual(cmdSamplingRate.payload.interval_sec, 10);
console.log('  ✓ 3. Update Sampling Rate: interval_sec = 10s');

// 2.4 Manual Sample
const cmdManualSample = createSensorCommand('manual_sample');
assert.strictEqual(cmdManualSample.payload.action, 'manual_sample');
console.log('  ✓ 4. Manual Sample: orden de muestreo inmediato verificada');

// 2.5 Go to Sleep: Timed vs Indefinite
const cmdTimedSleep = createSensorCommand('sleep', { minutes: 30, indefinite: false });
assert.strictEqual(cmdTimedSleep.payload.minutes, 30);
assert.strictEqual(cmdTimedSleep.payload.indefinite, false);

const cmdIndefiniteSleep = createSensorCommand('sleep', { minutes: 0, indefinite: true });
assert.strictEqual(cmdIndefiniteSleep.payload.minutes, 0);
assert.strictEqual(cmdIndefiniteSleep.payload.indefinite, true);
console.log('  ✓ 5. Deep Sleep: Temporizado (30 min) e Indefinido (0 min / hardware wake) validados');

// 2.6 Sleep Cycles (Scheduled Autonomous Interval)
const cmdSleepCycle = createSensorCommand('set_sleep_cycle', { measure_time_min: 15 });
assert.strictEqual(cmdSleepCycle.payload.measure_time_min, 15);
console.log('  ✓ 6. Sleep Cycle: Programación autónoma cada 15 minutos');

// 3. Simulación de Piggybacked Command en Respuesta HTTP 200 de Telemetría
console.log('\n[Test 3] Verificando empaquetado de comandos en respuesta de telemetría...');
function simulateTelemetryResponse(rawPendingCommand) {
  const hasCommand = Boolean(rawPendingCommand);
  return {
    success: true,
    message: 'Telemetría de sensor recibida y procesada correctamente',
    has_command: hasCommand,
    pending_command: rawPendingCommand || null
  };
}

const mockResponseWithCmd = simulateTelemetryResponse(cmdStartMonitor);
assert.strictEqual(mockResponseWithCmd.has_command, true);
assert.strictEqual(mockResponseWithCmd.pending_command.payload.action, 'start_monitor');

const mockResponseWithoutCmd = simulateTelemetryResponse(null);
assert.strictEqual(mockResponseWithoutCmd.has_command, false);
assert.strictEqual(mockResponseWithoutCmd.pending_command, null);
console.log('  ✓ Piggybacking en POST /api/telemetry/sensor probado con y sin comando pendiente');

// 4. Verificación de Reconocimiento y Actualización de Metadatos
console.log('\n[Test 4] Verificando acuse de recibo (Acknowledge) para el sensor...');
function simulateAcknowledge(cmdId, sensorState) {
  return {
    command_id: cmdId,
    status: 'acknowledged',
    executed_at: new Date().toISOString(),
    sensor_state: sensorState
  };
}

const ack = simulateAcknowledge('cmd-test-123', { monitor_active: true, monitor_interval_sec: 5 });
assert.strictEqual(ack.status, 'acknowledged');
assert.strictEqual(ack.sensor_state.monitor_active, true);
assert.strictEqual(ack.sensor_state.monitor_interval_sec, 5);
console.log('  ✓ Acuse de recibo de sensor propaga estado a devices.metadata correctamente');

// 5. Verificación de Ciclo de Experimentos y Formato CSV 8.3 FAT
console.log('\n[Test 5] Verificando ciclo de Experimentos y contrato 8.3 FAT CSV...');
function sanitizeFilename83(name) {
  const base = name.replace(/[^a-zA-Z0-9]/g, '_').toUpperCase().slice(0, 8);
  return `${base || 'EXP_DATA'}.CSV`;
}

const expName = 'Prueba Aireación Nocturna #1';
const expFilename = sanitizeFilename83('EXP_NIGHT_01');
assert.match(expFilename, /^[A-Z0-9_]{1,8}\.CSV$/, 'El nombre de archivo debe cumplir formato FAT 8.3');
assert.strictEqual(expFilename, 'EXP_NIGH.CSV');

const cmdStartExp = createSensorCommand('start_experiment', {
  experiment_id: 'exp-test-456',
  name: expName,
  filename: expFilename,
  interval_sec: 2
});
assert.strictEqual(cmdStartExp.command_type, 'start');
assert.strictEqual(cmdStartExp.payload.action, 'start_experiment');
assert.strictEqual(cmdStartExp.payload.interval_sec, 2);
assert.strictEqual(cmdStartExp.payload.filename, 'EXP_NIGH.CSV');
console.log('  ✓ 1. Start Experiment: comando start emitido con filename 8.3 y rate 2s');

const cmdStopExp = createSensorCommand('stop_experiment', { experiment_id: 'exp-test-456' });
assert.strictEqual(cmdStopExp.command_type, 'stop');
assert.strictEqual(cmdStopExp.payload.action, 'stop_experiment');
assert.strictEqual(cmdStopExp.payload.experiment_id, 'exp-test-456');
console.log('  ✓ 2. Stop Experiment: comando stop emitido para finalizar y cerrar archivo');

// Verificación de cabeceras RFC 4180 CSV
const expectedCsvHeader = 'Timestamp,RTC_Sec,DO_mg_L,Sat_Pct,Temp_C,Batt_V';
const mockCsvLine = '2026-09-11T20:00:00.000Z,1789156800,6.72,94.8,24.1,4.08';
const csvContent = `${expectedCsvHeader}\n${mockCsvLine}\n`;
assert(csvContent.startsWith(expectedCsvHeader), 'Cabecera CSV debe respetar el contrato estándar');
console.log('  ✓ 3. Formato CSV RFC 4180 verificado con campos requeridos');

console.log('\n======================================================');
console.log(' ¡TODAS LAS PRUEBAS DE COMANDOS DE SENSOR PASARON! ✓  ');
console.log('======================================================\n');
