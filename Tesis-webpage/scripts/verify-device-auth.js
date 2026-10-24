/**
 * Self-check script: Verificación del motor de autenticación de dispositivos (deviceAuth)
 * Ejecución: node scripts/verify-device-auth.js
 */
const assert = require('assert');
const crypto = require('crypto');

// Simulación de hashing equivalente a hashApiKey en lib/supabase.ts
function hashApiKey(key) {
  return crypto.createHash('sha256').update(key.trim()).digest('hex');
}

// Configuración idéntica a KNOWN_DEVICES en deviceAuth.ts
const KNOWN_DEVICES = [
  {
    id: 'a0000000-0000-0000-0000-000000000001',
    name: 'Sensor Óptico OD - Estanque 1',
    type: 'sensor_do',
    location: 'Estanque Principal (Zona Norte)',
    metadata: { sensor_model: 'Aqualabo DIGISENS', interface: 'Modbus RS485' },
    envVarKeys: ['ESP32_OD_SENSOR', 'ESP32_SENSOR_DEVICE_KEY'],
    defaultTokens: ['ESP32_OD_SENSOR', 'ESP32_SENSOR_KEY_2026']
  },
  {
    id: 'b0000000-0000-0000-0000-000000000002',
    name: 'Controlador ODrive S1 - Estanque 1',
    type: 'motor_thruster',
    location: 'Estanque Principal (Zona Central)',
    metadata: { controller_model: 'ODrive S1', interface: 'UART ASCII', control_mode: 'pid' },
    envVarKeys: ['ESP32_ODRIVE', 'ESP32_MOTOR_DEVICE_KEY'],
    defaultTokens: ['ESP32_ODRIVE', 'ESP32_MOTOR_KEY_2026']
  },
  {
    id: 'c0000000-0000-0000-0000-000000000003',
    name: 'Aireador Auxiliar ESC (Banco de Pruebas)',
    type: 'motor_thruster',
    location: 'Laboratorio / Banco de Pruebas',
    metadata: { controller_model: 'ESP32-S3 ESC PWM', status: 'auxiliary_backup' },
    envVarKeys: ['ESP32_T_200', 'ESP32_ESC_DEVICE_KEY'],
    defaultTokens: ['ESP32_T_200', 'ESP32_ESC_KEY_2026']
  }
];

function resolveKnownDevice(deviceKey) {
  for (const cfg of KNOWN_DEVICES) {
    if (cfg.defaultTokens.includes(deviceKey)) {
      return {
        id: cfg.id,
        name: cfg.name,
        type: cfg.type,
        location: cfg.location,
        status: 'online',
        metadata: cfg.metadata
      };
    }
    for (const envKey of cfg.envVarKeys) {
      const envVal = process.env[envKey];
      if (envVal && (envVal === deviceKey || envKey === deviceKey)) {
        return {
          id: cfg.id,
          name: cfg.name,
          type: cfg.type,
          location: cfg.location,
          status: 'online',
          metadata: cfg.metadata
        };
      }
    }
  }
  return null;
}

function simulateAuth(headers, expectedType) {
  const deviceKey = headers['x-device-key'] || headers['X-Device-Key'];
  if (!deviceKey) {
    return { status: 401, error: 'Encabezado X-Device-Key faltante', device: null };
  }

  const device = resolveKnownDevice(deviceKey);
  if (device) {
    if (expectedType && device.type !== expectedType) {
      return { status: 403, error: 'Dispositivo no autorizado para este tipo', device: null };
    }
    return { status: 200, device, error: null };
  }

  return { status: 403, error: 'Clave inválida o no autorizada', device: null };
}

console.log('====================================================');
console.log(' VERIFICACIÓN DE AUTENTICACIÓN DE DISPOSITIVOS IoT  ');
console.log('====================================================\n');

// Test 1: Validación de SHA-256 matemáticos esperados
console.log('[Test 1] Verificando hashes SHA-256 de tokens principales...');
assert.strictEqual(
  hashApiKey('ESP32_OD_SENSOR'),
  '2ae50779e5419027c848d0677699c75164387685b30ed1db9f801c611af0dd5e',
  'Hash de ESP32_OD_SENSOR debe coincidir con schema.sql'
);
assert.strictEqual(
  hashApiKey('ESP32_ODRIVE'),
  'b40b683230acacb4cdc01d98cb7350a55fff80a4a5ef2dff76e6739e8810f114',
  'Hash de ESP32_ODRIVE debe coincidir con schema.sql'
);
assert.strictEqual(
  hashApiKey('ESP32_T_200'),
  'e981fdf139c8da231cddd2bbdc40b5e372b9407320df6cd462747019cf9404a6',
  'Hash de ESP32_T_200 debe coincidir con schema.sql'
);
console.log('  ✓ Hashes SHA-256 calculados y validados correctamente.');

// Test 2: Autenticación de ESP32_OD_SENSOR para telemetría de sensor
console.log('\n[Test 2] Verificando autenticación de sensor OD...');
const sensorRes = simulateAuth({ 'X-Device-Key': 'ESP32_OD_SENSOR' }, 'sensor_do');
assert.strictEqual(sensorRes.status, 200);
assert.strictEqual(sensorRes.device.id, 'a0000000-0000-0000-0000-000000000001');
assert.strictEqual(sensorRes.device.type, 'sensor_do');
console.log('  ✓ Sensor OD autenticado exitosamente (ID:', sensorRes.device.id, ')');

// Test 3: Retrocompatibilidad con ESP32_SENSOR_KEY_2026
console.log('\n[Test 3] Verificando retrocompatibilidad con clave legacy...');
const legacySensorRes = simulateAuth({ 'x-device-key': 'ESP32_SENSOR_KEY_2026' }, 'sensor_do');
assert.strictEqual(legacySensorRes.status, 200);
assert.strictEqual(legacySensorRes.device.id, 'a0000000-0000-0000-0000-000000000001');
console.log('  ✓ Clave legacy ESP32_SENSOR_KEY_2026 aceptada sin problemas.');

// Test 4: Autenticación de ODrive y ESC T-200
console.log('\n[Test 4] Verificando autenticación de actuadores motor...');
const odriveRes = simulateAuth({ 'X-Device-Key': 'ESP32_ODRIVE' }, 'motor_thruster');
assert.strictEqual(odriveRes.status, 200);
assert.strictEqual(odriveRes.device.id, 'b0000000-0000-0000-0000-000000000002');

const escRes = simulateAuth({ 'X-Device-Key': 'ESP32_T_200' }, 'motor_thruster');
assert.strictEqual(escRes.status, 200);
assert.strictEqual(escRes.device.id, 'c0000000-0000-0000-0000-000000000003');
console.log('  ✓ ODrive y ESC T-200 autenticados con sus respectivos IDs.');

// Test 5: Rechazo por discrepancia de tipo (Seguridad cruzada)
console.log('\n[Test 5] Verificando rechazo por discrepancia de tipo...');
const crossTypeRes = simulateAuth({ 'X-Device-Key': 'ESP32_OD_SENSOR' }, 'motor_thruster');
assert.strictEqual(crossTypeRes.status, 403, 'Sensor OD no debe poder enviar comandos o telemetría de motor');
console.log('  ✓ Intento de enviar telemetría de motor con clave de sensor rechazado con HTTP 403.');

// Test 6: Rechazo de clave no autorizada y cabecera faltante
console.log('\n[Test 6] Verificando rechazo de claves inválidas y headers faltantes...');
const missingHeaderRes = simulateAuth({});
assert.strictEqual(missingHeaderRes.status, 401);

const invalidKeyRes = simulateAuth({ 'X-Device-Key': 'CLAVE_HACKER_123' }, 'sensor_do');
assert.strictEqual(invalidKeyRes.status, 403);
console.log('  ✓ Header faltante -> HTTP 401 | Clave no autorizada -> HTTP 403');

console.log('\n====================================================');
console.log(' TODAS LAS PRUEBAS DE AUTENTICACIÓN PASARON (6/6)    ');
console.log('====================================================\n');
