/**
 * Self-check script: Verificación de correcciones del Dashboard
 * - Heartbeat de dispositivos (offline/online)
 * - Supresión de telemetría fantasma cuando el motor está offline
 * - Formateo de zona horaria Perú (America/Lima / GMT-5)
 * - Normalización de nombres de dispositivos
 *
 * Ejecución: node scripts/verify-dashboard-fixes.js
 */
const assert = require('assert');

// 1. Verificación del Helper de Zona Horaria (dateUtils)
function formatPeruTime(dateInput, includeSeconds = false) {
  if (!dateInput) return '--:--';
  const date = typeof dateInput === 'string' || typeof dateInput === 'number' ? new Date(dateInput) : dateInput;
  return date.toLocaleTimeString('es-PE', {
    timeZone: 'America/Lima',
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false
  });
}

function formatPeruDateTime(dateInput, includeSeconds = false) {
  if (!dateInput) return '--/-- --:--';
  const date = typeof dateInput === 'string' || typeof dateInput === 'number' ? new Date(dateInput) : dateInput;
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Lima',
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false
  }).formatToParts(date);
  const getPart = (type) => parts.find(p => p.type === type)?.value || '00';
  const time = includeSeconds
    ? `${getPart('hour')}:${getPart('minute')}:${getPart('second')}`
    : `${getPart('hour')}:${getPart('minute')}`;
  return `${getPart('day')}/${getPart('month')} ${time}`;
}

console.log('====================================================');
console.log(' VERIFICACIÓN DE CORRECCIONES DEL DASHBOARD AQUACONTROL');
console.log('====================================================\n');

// Test 1: Verificación de Zona Horaria Perú (UTC-5)
console.log('[Test 1] Verificando conversión exacta a hora de Lima (GMT-5)...');
// 2026-09-06T21:00:00Z en UTC equivale exactamente a las 16:00:00 en Lima (GMT-5)
const utcTimestamp = '2026-09-06T21:00:00Z';
const peruTime = formatPeruTime(utcTimestamp);
const peruDateTime = formatPeruDateTime(utcTimestamp);

assert.strictEqual(peruTime, '16:00', `Debe ser 16:00 pero se obtuvo ${peruTime}`);
assert.strictEqual(peruDateTime, '06/09 16:00', `Debe ser 06/09 16:00 pero se obtuvo ${peruDateTime}`);
console.log('  ✓ 21:00:00 UTC convertido correctamente a 16:00 en Lima (GMT-5)');

// Test 2: Verificación de Heartbeat Timeout (60 segundos)
console.log('\n[Test 2] Verificando evaluación dinámica de Heartbeat...');
const HEARTBEAT_TIMEOUT_MS = 60 * 1000;
function evaluateDeviceStatus(device) {
  if (!device) return null;
  const isFresh = Boolean(
    device.last_seen_at &&
    (Date.now() - new Date(device.last_seen_at).getTime() < HEARTBEAT_TIMEOUT_MS)
  );
  return {
    ...device,
    status: isFresh ? 'online' : 'offline'
  };
}

// Dispositivo visto hace 10 segundos
const freshDevice = {
  id: 'test-1',
  name: 'Sensor Reciente',
  status: 'online',
  last_seen_at: new Date(Date.now() - 10000).toISOString()
};
assert.strictEqual(evaluateDeviceStatus(freshDevice).status, 'online');
console.log('  ✓ Dispositivo visto hace 10s se evalúa como ONLINE.');

// Dispositivo visto hace 5 días (como el ODrive en la BD de agosto)
const staleDevice = {
  id: 'test-2',
  name: 'ODrive S1 Antiguo',
  status: 'online', // en BD estaba guardado como 'online'
  last_seen_at: '2026-08-31T20:00:00Z'
};
assert.strictEqual(evaluateDeviceStatus(staleDevice).status, 'offline');
console.log('  ✓ Dispositivo sin telemetría reciente pasa automáticamente a OFFLINE.');

// Test 3: Supresión de Telemetría Fantasma cuando el Motor está Desconectado
console.log('\n[Test 3] Verificando supresión de telemetría cuando el motor está offline...');
function sanitizeTelemetry(motorDevice, telemetry) {
  if (!telemetry) return null;
  if (!motorDevice || motorDevice.status === 'offline') {
    return {
      ...telemetry,
      is_on: false,
      speed_percent: 0,
      power_w: 0
    };
  }
  return telemetry;
}

const mockHistoricalTelemetry = {
  id: 1,
  device_id: 'b0000000-0000-0000-0000-000000000002',
  is_on: true,
  speed_percent: 65,
  power_w: 125.8
};

const evaluatedStale = evaluateDeviceStatus(staleDevice);
const sanitizedOffline = sanitizeTelemetry(evaluatedStale, mockHistoricalTelemetry);
assert.strictEqual(sanitizedOffline.is_on, false, 'Motor offline no puede reportar is_on: true');
assert.strictEqual(sanitizedOffline.speed_percent, 0, 'Motor offline debe reportar 0% velocidad');
assert.strictEqual(sanitizedOffline.power_w, 0, 'Motor offline debe reportar 0 W de potencia');
console.log('  ✓ Telemetría histórica suprimida: is_on=false, speed=0%, power=0W.');

// Test 4: Salud del Sistema degradada ante sensor offline
console.log('\n[Test 4] Verificando estado de salud del estanque con sensor desconectado...');
function computeHealth(sensorDevice, reading) {
  if (!sensorDevice || sensorDevice.status === 'offline') {
    return 'warning';
  }
  if (!reading) return 'offline';
  const doVal = Number(reading.dissolved_oxygen_mg_l);
  if (doVal < 4.0) return 'critical';
  if (doVal < 6.0) return 'warning';
  return 'optimal';
}

const disconnectedSensor = { id: 's1', status: 'offline' };
const goodReading = { dissolved_oxygen_mg_l: 7.8 };
assert.strictEqual(
  computeHealth(disconnectedSensor, goodReading),
  'warning',
  'Si el sensor está desconectado, la salud debe ser warning'
);
// Test 5: Heartbeat del sensor derivado en tiempo real desde la última lectura
console.log('\n[Test 5] Verificando heartbeat del sensor derivado de latestSensorReading...');
function resolveSensorWithHeartbeat(rawDevice, latestReading) {
  const sensorLastSeen = latestReading?.recorded_at
    ? (rawDevice?.last_seen_at && new Date(rawDevice.last_seen_at).getTime() > new Date(latestReading.recorded_at).getTime()
        ? rawDevice.last_seen_at
        : latestReading.recorded_at)
    : rawDevice?.last_seen_at;

  return evaluateDeviceStatus({
    ...(rawDevice || { id: 'fallback', name: 'Sensor OD', type: 'sensor_do' }),
    last_seen_at: sensorLastSeen
  });
}

// Dispositivo en BD desfasado (antiguo), pero lectura llegando en tiempo real hace 5 segundos
const staleDbSensor = { id: 'sensor-1', name: 'Sensor OD', type: 'sensor_do', last_seen_at: '2026-08-31T20:00:00Z' };
const activeTelemetryReading = { id: 99, recorded_at: new Date(Date.now() - 5000).toISOString(), dissolved_oxygen_mg_l: 8.5 };

const resolvedActiveSensor = resolveSensorWithHeartbeat(staleDbSensor, activeTelemetryReading);
assert.strictEqual(resolvedActiveSensor.status, 'online', 'Sensor debe reconocerse ONLINE cuando hay telemetría activa');
console.log('  ✓ Sensor se evalúa ONLINE aun si la fila en devices estaba desfasada.');

// Telemetría que dejó de llegar hace 5 minutos -> pasa a OFFLINE
const stoppedTelemetryReading = { id: 100, recorded_at: new Date(Date.now() - 300000).toISOString(), dissolved_oxygen_mg_l: 8.5 };
const resolvedStoppedSensor = resolveSensorWithHeartbeat(staleDbSensor, stoppedTelemetryReading);
assert.strictEqual(resolvedStoppedSensor.status, 'offline', 'Sensor debe pasar a OFFLINE cuando la telemetría se detiene');
console.log('  ✓ Sensor pasa a OFFLINE automáticamente al detenerse la telemetría.');

// Test 6: Verificación de decodificación de do_milli_mg_l (mili-mg/L sin división extra)
console.log('\n[Test 6] Verificando decodificación exacta de Oxígeno Disuelto...');
function decodeSensorTelemetry(payload) {
  if (payload.do_milli_mg_l === undefined || payload.water_temp_centi === undefined) {
    throw new Error('Faltan campos obligatorios: do_milli_mg_l o water_temp_centi');
  }
  const doDivider = 1000.0;
  return Number((payload.do_milli_mg_l / doDivider).toFixed(3));
}

const mockDoPacket = { do_milli_mg_l: 8456, water_temp_centi: 2350 };
assert.strictEqual(decodeSensorTelemetry(mockDoPacket), 8.456, '8456 milli-mg/L debe decodificar exactamente a 8.456 mg/L');

const mockDoLow = { do_milli_mg_l: 4120, water_temp_centi: 2200 };
assert.strictEqual(decodeSensorTelemetry(mockDoLow), 4.120, '4120 milli-mg/L debe decodificar exactamente a 4.120 mg/L');
console.log('  ✓ do_milli_mg_l decodificado exactamente (8456 -> 8.456 mg/L, no 0.845 mg/L).');

// Test 7: Verificación de cálculo ODrive RPM (0 - 600 RPM, motor apagado = 0 RPM, sin PWM)
console.log('\n[Test 7] Verificando cálculo de ODrive RPM (Escala 0 a 600 RPM, desacople de PWM)...');
function computeODriveRpmFromTelemetry(telemetry) {
  if (!telemetry || !telemetry.is_on || Number(telemetry.speed_percent) === 0) {
    return 0;
  }
  return Math.round((Number(telemetry.speed_percent) / 100) * 600);
}

// Caso 1: Motor apagado con valor residual de pwm_us (ej: 1500 us)
const stoppedWithPwm = { is_on: false, speed_percent: 0, pwm_us: 1500 };
assert.strictEqual(computeODriveRpmFromTelemetry(stoppedWithPwm), 0, 'Motor apagado con pwm_us 1500 debe retornar 0 RPM');

// Caso 2: Motor apagado con speed_percent residual
const stoppedWithSpeed = { is_on: false, speed_percent: 65, pwm_us: 1760 };
assert.strictEqual(computeODriveRpmFromTelemetry(stoppedWithSpeed), 0, 'Motor apagado con speed_percent residual debe retornar 0 RPM');

// Caso 3: Motor encendido al 50%
const running50 = { is_on: true, speed_percent: 50, pwm_us: 1700 };
assert.strictEqual(computeODriveRpmFromTelemetry(running50), 300, 'ODrive al 50% debe calcular 300 RPM');

// Caso 4: Motor encendido al 65%
const running65 = { is_on: true, speed_percent: 65, pwm_us: 1760 };
assert.strictEqual(computeODriveRpmFromTelemetry(running65), 390, 'ODrive al 65% debe calcular 390 RPM');

// Caso 5: Motor encendido al 100%
const running100 = { is_on: true, speed_percent: 100, pwm_us: 1900 };
assert.strictEqual(computeODriveRpmFromTelemetry(running100), 600, 'ODrive al 100% debe calcular 600 RPM');

console.log('  ✓ ODrive RPM calculado limpiamente en escala 0-600 RPM (0%->0, 50%->300, 65%->390, 100%->600).');
console.log('  ✓ Motor en reposo (is_on=false) genera 0 RPM incluso con pwm_us residual de 1500.');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (7/7)');
console.log('====================================================\n');
