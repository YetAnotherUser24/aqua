/**
 * Self-check: Roles de dispositivo y tablas de archivo separadas (AquaControl V5)
 * Ejecución: node scripts/verify-device-roles.js
 *
 * Regresiones que bloquea:
 *  1. Los tres actuadores compartían `devices.type = 'motor_thruster'`, así que el sistema no podía
 *     distinguirlos y un nodo se robaba las órdenes/telemetría de otro.
 *  2. Toda la telemetría de actuadores caía en la misma tabla.
 *  3. El dashboard identificaba el ODrive por coincidencia de texto ("ODrive" en el nombre).
 *  4. Los INSERT incluían la columna inexistente `motor_telemetry.rpm`: PostgREST rechaza el INSERT
 *     completo (PGRST204), así que la telemetría del aireador nunca se guardaba — de forma silenciosa.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const REPO = path.join(ROOT, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const readRepo = (p) => fs.readFileSync(path.join(REPO, p), 'utf8');

let passed = 0;
const ok = (msg) => { passed++; console.log('  ✓ ' + msg); };

console.log('====================================================');
console.log(' VERIFICACIÓN DE ROLES Y TABLAS POR ACTUADOR       ');
console.log('====================================================\n');

const roles = read('src/lib/deviceRoles.ts');
const auth = read('src/lib/deviceAuth.ts');
const types = read('src/types/index.ts');
const stateLib = read('src/lib/systemState.ts');
const pendingRoute = read('src/app/api/commands/pending/route.ts');
const commandsRoute = read('src/app/api/commands/route.ts');
const summaryRoute = read('src/app/api/dashboard/summary/route.ts');
const ackRoute = read('src/app/api/commands/[id]/acknowledge/route.ts');
const motorBulk = read('src/app/api/telemetry/motor_bulk/route.ts');
const motorSingle = read('src/app/api/telemetry/motor/route.ts');
const archive = read('src/lib/telemetryArchive.ts');
const downloadRoute = read('src/app/api/experiments/[id]/download/route.ts');
const migration = read('supabase/migrations/20261008_separate_actuator_roles.sql');
const schema = read('supabase/schema.sql');

console.log('[Test 1] El tipo legacy motor_thruster ya no agrupa a los actuadores...');
assert.ok(/type:\s*'aerator_motor'/.test(roles), 'El ODrive debe tener tipo propio');
assert.ok(/type:\s*'mixer'/.test(roles), 'El mixer debe tener tipo propio');
assert.ok(/type:\s*'dosing_pump'/.test(roles), 'La bomba debe tener tipo propio');
assert.ok(!/type:\s*'motor_thruster'/.test(roles), 'REGRESIÓN: ningún actuador debe declararse motor_thruster');
// El tipo legacy sigue admitido en la unión de tipos y en el CHECK, por compatibilidad.
assert.ok(types.includes("'motor_thruster'"), 'El tipo legacy debe seguir admitido para filas antiguas');
assert.ok(migration.includes("'motor_thruster',  -- LEGACY"), 'El CHECK debe conservar el valor legacy');
const declared = ['aerator_motor', 'mixer', 'dosing_pump', 'motor_thruster', 'gateway', 'sensor_do'];
for (const t of declared) {
  assert.ok(migration.includes(`'${t}'`), `El CHECK de devices.type debe admitir '${t}'`);
}
ok('Tres actuadores con tipo propio; legacy conservado por compatibilidad');

console.log('\n[Test 2] Rol explícito resuelto por device_id (sin heurísticas)...');
assert.ok(roles.includes('export function resolveDeviceRole'), 'Debe existir resolveDeviceRole');
assert.ok(roles.includes('ROLE_BY_DEVICE_ID'), 'Debe existir el mapa id -> rol');
// Un motor_thruster genérico NO debe resolverse a un rol concreto.
assert.ok(roles.includes('export function isActuatorRole'), 'Debe existir isActuatorRole');
assert.ok(/LEGACY_TYPE_TO_ROLE[\s\S]{0,120}sensor_do: 'sensor'/.test(roles),
  'Solo el tipo legacy inequívoco debe mapearse a un rol');
assert.ok(!/motor_thruster:\s*'/.test(roles), 'REGRESIÓN: motor_thruster no debe mapearse a un rol único');
ok('Rol inequívoco por device_id; motor_thruster no se adivina');

console.log('\n[Test 3] Cada actuador archiva en su propia tabla...');
for (const [role, table] of [['odrive', 'odrive_telemetry_bulk'], ['mixer', 'mixer_telemetry'], ['pump', 'pump_telemetry'], ['sensor', 'sensor_telemetry_bulk']]) {
  assert.ok(new RegExp(`${role}: '${table}'`).test(roles), `El rol ${role} debe archivar en ${table}`);
  assert.ok(migration.includes(`public.${table}`), `La migración debe crear ${table}`);
}
for (const t of ['mixer_events', 'pump_events', 'manual_overrides_log']) {
  assert.ok(migration.includes(`public.${t}`), `La migración debe crear ${t}`);
}
ok('4 tablas de archivo (una por rol) + tablas de eventos');

console.log('\n[Test 4] El archivado avisa cuando falta la tabla (no pierde datos en silencio)...');
assert.ok(archive.includes('isMissingTableError'), 'Debe detectar la tabla inexistente');
assert.ok(archive.includes('20261008_separate_actuator_roles.sql'), 'El aviso debe indicar la migración');
assert.ok(archive.includes('warnedTables'), 'El aviso no debe repetirse en cada lote de 5 s');
assert.ok(motorBulk.includes('archiveRolePayload'), 'motor_bulk debe archivar por rol');
assert.ok(!motorBulk.includes(".from('odrive_telemetry_bulk')"), 'REGRESIÓN: no debe archivar todo en odrive_telemetry_bulk');
// El T-200 publica un objeto suelto en /api/telemetry/motor (no un lote), y esa ruta no archivaba:
// `mixer_telemetry` quedaba vacía para siempre aunque la tabla existiera.
assert.ok(motorSingle.includes('archiveRolePayload'),
  'REGRESIÓN: /api/telemetry/motor debe archivar en la tabla del rol (el mixer llega por aquí)');
assert.ok(motorSingle.includes("from '@/lib/telemetryArchive'"),
  'La ruta de objeto suelto debe usar el mismo archivado por rol');
assert.ok(!/authenticateDevice\(req, 'motor_thruster'\)/.test(motorSingle),
  'REGRESIÓN: no debe autenticarse contra el tipo legacy; el rol se resuelve por device_id');
assert.ok(motorSingle.includes('isActuatorRole'), 'Debe restringirse a actuadores por rol');
ok('Archivado por rol con aviso accionable una sola vez (lote y objeto suelto)');

console.log('\n[Test 5] `rpm` existe ya en producción y se persiste con degradación segura...');
// La migración 20261008 añadió la columna, y `db.js audit` la confirma desplegada.
assert.ok(/ALTER TABLE public.motor_telemetry ADD COLUMN IF NOT EXISTS rpm/.test(migration),
  'La migración debe añadir la columna rpm que schema.sql declara');
// El defecto original fue enviar la columna cuando NO existía: PostgREST rechazaba el INSERT entero
// (PGRST204) y la telemetría del aireador no se guardaba nunca. Ahora que existe se envía, y si un
// despliegue contra un esquema viejo la rechaza se reintenta sin ella en vez de perder el lote.
assert.ok(/rpm:\s*actual_rpm/.test(motorBulk), 'motor_bulk debe persistir la RPM real');
assert.ok(motorBulk.includes('PGRST204') && /reintentando sin ellas/.test(motorBulk),
  'Un entorno sin la columna debe degradar, no perder el lote entero');
assert.ok(motorBulk.includes('warning: ingestWarning'), 'motor_bulk debe reportar el fallo de ingesta');
assert.ok(motorBulk.includes('ingested: ingestedRows'), 'motor_bulk debe reportar las filas almacenadas');
// Un SELECT contra una columna inexistente también falla entero (42703) y devuelve vacío.
assert.ok(!/MOTOR_CSV_COLUMNS = '[^']*rpm/.test(downloadRoute),
  'REGRESIÓN: el respaldo legacy no debe seleccionar columnas que producción no tenía');
ok('Columna rpm persistida con degradación segura + ingesta reportada + esquema convergente');

console.log('\n[Test 6] Descarga CSV con respaldo legacy (nunca vacía)...');
assert.ok(downloadRoute.includes('fetchLegacyWindow'), 'La descarga debe tener respaldo legacy');
assert.ok(downloadRoute.includes('sensor_readings') && downloadRoute.includes('motor_telemetry'),
  'El respaldo debe leer las tablas legacy');
assert.ok(downloadRoute.includes('USE_PLACEHOLDER') === false, 'Sin marcadores pendientes');
// El CSV reconstruye RPM desde speed_percent (igual que /api/dashboard/history).
assert.ok(/\(speedPercent \/ 100\) \* 600/.test(downloadRoute), 'Debe reconstruir RPM desde speed_percent');
ok('CSV con respaldo legacy y RPM reconstruidas');

console.log('\n[Test 7] Autorización, enrutado y dashboard por rol...');
assert.ok(auth.includes('expectedRole'), 'authenticateDevice debe aceptar un rol esperado');
assert.ok(auth.includes('KNOWN_DEVICES') && !/const KNOWN_DEVICES: KnownDeviceConfig\[\]/.test(auth),
  'deviceAuth debe consumir el registro canónico (sin duplicar identidades)');
assert.ok(roles.includes('export const KNOWN_DEVICES'), 'El registro canónico debe estar en deviceRoles');
// /api/commands/pending: el fallback se limita a órdenes globales y respeta target_role.
assert.ok(pendingRoute.includes("target_role"), 'Debe respetar el rol destinatario');
assert.ok(/\.is\('device_id', null\)/.test(pendingRoute), 'El fallback debe limitarse a órdenes globales');
assert.ok(stateLib.includes('target_role: role'), 'El set_state difundido debe declarar su rol destinatario');
// /api/commands: resuelve por rol, no por el tipo ambiguo.
assert.ok(commandsRoute.includes('DEVICE_ID_BY_ROLE'), 'La creación de comandos debe resolver por rol');
assert.ok(!/\.eq\('type', targetType\)/.test(commandsRoute),
  'REGRESIÓN: no debe resolver el motor por el tipo legacy ambiguo');
// Dashboard: sin heurísticas de texto.
assert.ok(summaryRoute.includes('DEVICE_ID_BY_ROLE.odrive') && summaryRoute.includes('DEVICE_ID_BY_ROLE.mixer'),
  'El resumen debe localizar los actuadores por rol');
assert.ok(!/name\?\.includes\('ODrive'\)/.test(summaryRoute),
  "REGRESIÓN: no debe identificar el ODrive por coincidencia de texto en el nombre");
// ACK: sin depender del tipo legacy.
assert.ok(ackRoute.includes('isActuatorRole(role)'), 'El ACK debe decidir por rol');
ok('Autorización, comandos, dashboard y ACK gobernados por rol');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (7/7) ');
console.log('====================================================');
assert.ok(passed === 7, `Se esperaban 7 bloques, se ejecutaron ${passed}`);
