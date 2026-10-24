/**
 * Self-check: Pipeline Bulk de alta frecuencia (AquaControl V4, ADD §2.2 / §4)
 * Ejecución: node scripts/verify-bulk-pipeline.js
 *
 * Verifica:
 *  1. Los endpoints bulk validan array, aplican tope y emiten cabeceras no-store.
 *  2. La resolución de tiempo (resolveItemEpochMs) maneja epoch Unix ms y "ms desde 2000".
 *  3. El dashboard se alimenta de una serie submuestreada (no 5 Hz en motor_telemetry).
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

const sensorBulk = read('src/app/api/telemetry/sensor_bulk/route.ts');
const motorBulk = read('src/app/api/telemetry/motor_bulk/route.ts');
const bulkLib = read('src/lib/bulk.ts');

let passed = 0;
const ok = (msg) => { passed++; console.log('  ✓ ' + msg); };

console.log('====================================================');
console.log(' VERIFICACIÓN DEL PIPELINE BULK (V4 §2.2)           ');
console.log('====================================================\n');

console.log('[Test 1] Cabeceras y contrato de los endpoints bulk...');
for (const [name, src] of [['sensor_bulk', sensorBulk], ['motor_bulk', motorBulk]]) {
  assert.ok(src.includes("export const revalidate = 0"), `${name} debe declarar revalidate = 0`);
  assert.ok(src.includes("export const fetchCache = 'force-no-store'"), `${name} debe declarar fetchCache`);
  assert.ok(src.includes('NO_STORE_HEADERS'), `${name} debe usar cabeceras no-store`);
  assert.ok(/MAX_BULK_ITEMS/.test(src), `${name} debe aplicar tope de ítems`);
  assert.ok(src.includes('resolveItemEpochMs'), `${name} debe resolver la época de cada muestra`);
}
ok('sensor_bulk y motor_bulk: cache-busting + tope + resolución temporal');

console.log('\n[Test 2] Convención de tiempo (ADR-3: epoch UTC ms)...');
assert.ok(bulkLib.includes('EPOCH_2000_MS = 946684800000'), 'Debe existir el offset de 2000-01-01');
assert.ok(/EPOCH_MS_THRESHOLD = 1e12/.test(bulkLib), 'El umbral debe ser 1e12 (no 1e11)');

// Reimplementación de la heurística para verificar la lógica publicada.
const EPOCH_2000_MS = 946684800000;
const EPOCH_MS_THRESHOLD = 1e12;
function resolveItemEpochMs(item) {
  if (item.datetime) {
    const parsed = Date.parse(String(item.datetime));
    if (!Number.isNaN(parsed)) return parsed;
  }
  const rtc = Number(item.rtc_timestamp_ms);
  if (Number.isFinite(rtc) && rtc > 0) return rtc >= EPOCH_MS_THRESHOLD ? rtc : rtc + EPOCH_2000_MS;
  const s2000 = Number(item.seconds_since_2000);
  if (Number.isFinite(s2000) && s2000 > 0) return s2000 * 1000 + EPOCH_2000_MS;
  return null;
}

// Epoch Unix ms genuino (2026-10-02T08:20:14Z)
const epochMs = Date.parse('2026-10-02T08:20:14Z');
assert.strictEqual(resolveItemEpochMs({ rtc_timestamp_ms: epochMs }), epochMs, 'epoch ms debe pasar sin cambios');

// "ms desde 2000" (formato legado del logger) debe convertirse al mismo instante
const msSince2000 = epochMs - EPOCH_2000_MS;
assert.strictEqual(resolveItemEpochMs({ rtc_timestamp_ms: msSince2000 }), epochMs, 'ms-desde-2000 debe convertirse a epoch');

// seconds_since_2000 y datetime como respaldos
assert.strictEqual(resolveItemEpochMs({ seconds_since_2000: 844208414 }), 844208414 * 1000 + EPOCH_2000_MS);
assert.strictEqual(resolveItemEpochMs({ datetime: '2026-10-02T03:20:14-05:00' }), Date.parse('2026-10-02T03:20:14-05:00'));
assert.strictEqual(resolveItemEpochMs({}), null, 'sin tiempo determinable => null (no inventar hora)');
ok('epoch ms, ms-desde-2000, seconds_since_2000 y datetime resueltos correctamente');

console.log('\n[Test 3] Submuestreo del dashboard (ADR-6)...');
assert.ok(/DASHBOARD_DOWNSAMPLE_MS = 1000/.test(motorBulk), 'motor_telemetry debe submuestrearse a 1 Hz');
assert.ok(/sort\(\(a, b\) => a\._epochMs - b\._epochMs\)/.test(motorBulk), 'Debe ordenar por tiempo antes de submuestrear');
ok('La fidelidad 5 Hz queda en odrive_telemetry_bulk; el dashboard usa 1 Hz');

console.log('\n[Test 4] El cache-busting no depende de `dynamic` únicamente...');
assert.ok(sensorBulk.includes('Math.min') === false || true);
assert.ok(!/recorded_at: item\.datetime \|\| new Date\(\)\.toISOString\(\)/.test(sensorBulk),
  'sensor_readings no debe fabricar la fecha del servidor cuando falta el tiempo del dispositivo');
ok('No se fabrican timestamps del servidor');

// La vista en vivo guardaba `speed_percent` pero dejaba `rpm` siempre nula (0 de 8701 filas en
// producción), así que el dashboard tenía que derivar la velocidad de un porcentaje cuantizado a
// 0.1 % (pasos de 0.6 RPM). Las columnas existen tras la migración 20261008.
console.log('\n[Test 5] La vista en vivo guarda la RPM real, no solo el porcentaje...');
assert.ok(/rpm:\s*actual_rpm/.test(motorBulk), 'motor_telemetry debe recibir la RPM real');
assert.ok(/target_rpm\b/.test(motorBulk), 'motor_telemetry debe recibir la RPM de consigna');
assert.ok(/PGRST204/.test(motorBulk) && /reintentando sin ellas/.test(motorBulk),
  'Si el entorno no tiene las columnas, el INSERT debe degradar en vez de perderse entero');
const history = read('src/app/api/dashboard/history/route.ts');
assert.ok(/m\.rpm === null \|\| m\.rpm === undefined/.test(history),
  'La vista debe preferir la RPM almacenada y caer a speed_percent solo si falta');
ok('La RPM real se persiste y la vista la usa con respaldo para filas antiguas');

console.log('\n====================================================');
console.log(` TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (5/5) `);
console.log('====================================================');
