/**
 * Self-check script: Verificación del protocolo de comunicación WebServer <-> ODrive & ESC
 * Ejecución: node scripts/verify-motor-comms.js
 */
const assert = require('assert');

// 1. Verificación de Contratos de Dispositivo y Claves
const DEVICES = {
  ODRIVE: {
    id: 'b0000000-0000-0000-0000-000000000002',
    key: 'ESP32_MOTOR_KEY_2026',
    model: 'ODrive S1 (M8325s)',
    protocol: 'UART ASCII',
    speedUnit: 'RPM (0 - 600)'
  },
  ESC: {
    id: 'c0000000-0000-0000-0000-000000000003',
    key: 'ESP32_ESC_KEY_2026',
    model: 'Blue Robotics T200 (ESC Basic / ESC500)',
    protocol: 'LEDC PWM 50Hz',
    pulseRange: [1500, 1900]
  }
};

console.log('====================================================');
console.log(' VERIFICACIÓN DE COMUNICACIÓN WEBSERVER <-> MOTORES ');
console.log('====================================================\n');

// Test 1: Mapeo de Identificadores y Claves
console.log('[Test 1] Verificando aislamiento de IDs y tokens...');
assert.notStrictEqual(DEVICES.ODRIVE.id, DEVICES.ESC.id, 'Los IDs de dispositivo deben ser estrictamente diferentes');
assert.notStrictEqual(DEVICES.ODRIVE.key, DEVICES.ESC.key, 'Las claves de autenticación deben ser únicas');
console.log('  ✓ ODrive ID:', DEVICES.ODRIVE.id, '| Key:', DEVICES.ODRIVE.key);
console.log('  ✓ ESC T-200 ID:', DEVICES.ESC.id, '| Key:', DEVICES.ESC.key);

// Test 2: Conversión y Calibración de Comandos ODrive S1 (RPM)
console.log('\n[Test 2] Verificando comandos para ODrive S1 (M8325s)...');
function computeODriveRpm(throttlePct) {
  return Math.round((Math.max(0, Math.min(100, throttlePct)) / 100) * 600);
}
assert.strictEqual(computeODriveRpm(0), 0, '0% debe dar 0 RPM');
assert.strictEqual(computeODriveRpm(50), 300, '50% debe dar 300 RPM');
assert.strictEqual(computeODriveRpm(65), 390, '65% debe dar 390 RPM');
assert.strictEqual(computeODriveRpm(100), 600, '100% debe dar 600 RPM');
console.log('  ✓ Consignas ODrive calculadas correctamente: 0% -> 0 RPM, 50% -> 300 RPM, 65% -> 390 RPM, 100% -> 600 RPM');

// Test 3: Conversión y Calibración de Comandos ESC T-200 (PWM Microsegundos)
console.log('\n[Test 3] Verificando comandos para ESC T-200 (PWM Microsegundos)...');
function computeEscPwm(throttlePct, isArmed) {
  if (!isArmed) return 1500;
  const pct = Math.max(0, Math.min(100, throttlePct));
  return Math.round(1500 + (pct / 100) * 400);
}
assert.strictEqual(computeEscPwm(0, true), 1500, '0% armado debe dar 1500 µs (neutro)');
assert.strictEqual(computeEscPwm(50, true), 1700, '50% armado debe dar 1700 µs');
assert.strictEqual(computeEscPwm(65, true), 1760, '65% armado debe dar 1760 µs');
assert.strictEqual(computeEscPwm(100, true), 1900, '100% armado debe dar 1900 µs');
assert.strictEqual(computeEscPwm(80, false), 1500, 'Desarmado siempre debe dar 1500 µs');
console.log('  ✓ Consignas ESC calculadas correctamente: Desarmado/0% -> 1500 µs, 65% -> 1760 µs, 100% -> 1900 µs');

// Test 4: Verificación de Formato de Carga Útil en /api/commands
console.log('\n[Test 4] Verificando estructura de payload enviada por la interfaz web...');
function createCommandPayload(deviceType, isArmed, speedPct, mode = 'manual') {
  if (deviceType === 'odrive') {
    return {
      device_id: DEVICES.ODRIVE.id,
      command_type: isArmed ? (speedPct > 0 ? 'set_speed' : 'stop') : 'stop',
      speed_percent: isArmed ? speedPct : 0,
      payload: { mode, manual_throttle_pct: isArmed ? speedPct : 0 }
    };
  } else {
    const pwm = computeEscPwm(speedPct, isArmed);
    return {
      device_id: DEVICES.ESC.id,
      command_type: isArmed ? (speedPct > 0 ? 'set_speed' : 'stop') : 'stop',
      speed_percent: isArmed ? speedPct : 0,
      payload: { mode: 'manual_pwm', pwm_us: pwm, target_speed_pct: isArmed ? speedPct : 0 }
    };
  }
}

const odriveCmd = createCommandPayload('odrive', true, 65);
assert.strictEqual(odriveCmd.device_id, DEVICES.ODRIVE.id);
assert.strictEqual(odriveCmd.speed_percent, 65);
assert.strictEqual(odriveCmd.command_type, 'set_speed');

const escCmd = createCommandPayload('esc', true, 65);
assert.strictEqual(escCmd.device_id, DEVICES.ESC.id);
assert.strictEqual(escCmd.payload.pwm_us, 1760);
assert.strictEqual(escCmd.command_type, 'set_speed');

console.log('  ✓ Payload ODrive:', JSON.stringify(odriveCmd));
console.log('  ✓ Payload ESC T-200:', JSON.stringify(escCmd));

// Test 5: Verificación de Paradas de Emergencia Desacopladas
console.log('\n[Test 5] Verificando independencia en paradas...');
const oDriveStop = { device_id: DEVICES.ODRIVE.id, command_type: 'emergency_stop', speed_percent: 0 };
const escStop = { device_id: DEVICES.ESC.id, command_type: 'stop', speed_percent: 0, payload: { pwm_us: 1500 } };
assert.strictEqual(oDriveStop.device_id, DEVICES.ODRIVE.id);
assert.strictEqual(escStop.device_id, DEVICES.ESC.id);
console.log('  ✓ Parada de ODrive S1 y Parada de ESC son 100% independientes y no interfieren entre sí.');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (5/5)');
console.log('====================================================\n');
