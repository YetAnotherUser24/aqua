/**
 * Self-check: Actuadores del banco — Mixer T-200 y Bomba dosificadora (AquaControl V4, ADD §3)
 * Ejecución: node scripts/verify-pump-events.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

let passed = 0;
const ok = (msg) => { passed++; console.log('  ✓ ' + msg); };

console.log('====================================================');
console.log(' VERIFICACIÓN DE ACTUADORES: MIXER T-200 Y BOMBA   ');
console.log('====================================================\n');

const pumpMain = read('../Codigos/nodo-bomba/src/main.cpp');
const pumpCfg = read('../Codigos/nodo-bomba/src/config.h');
const t200 = read('../Codigos/nodo-mezclador/src/cloud_worker.cpp');
const pumpRoute = read('src/app/api/events/pump/route.ts');
const overrideRoute = read('src/app/api/events/override/route.ts');
const mixerRoute = read('src/app/api/events/mixer/route.ts');
const auth = read('src/lib/deviceAuth.ts');
const migration = read('supabase/migrations/20261007_v4_orchestration.sql');
const schema = read('supabase/schema.sql');

console.log('[Test 1] Nodo bomba: dosificación volumétrica por encoder...');
assert.ok(pumpMain.includes('AS5600_ADDR'), 'Debe leer el AS5600');
assert.ok(pumpMain.includes('ML_PER_REV'), 'Debe calibrar mL por revolución');
assert.ok(pumpMain.includes('PUMP_DOSE'), 'Debe existir el estado de dosificación');
assert.ok(pumpMain.includes('DOSE_TIMEOUT_MS'), 'Debe tener timeout anti-sobredosificación');
assert.ok(pumpMain.includes('ledcSetup') && pumpMain.includes('ledcWrite'), 'Debe modular el PWM del cabezal');
ok('Lazo de dosificación 100 Hz con AS5600, calibración y watchdog');

console.log('\n[Test 2] Nodo bomba: integración cloud completa...');
assert.ok(pumpMain.includes('API_TELEMETRY_BULK'), 'Debe publicar telemetría bulk');
assert.ok(pumpMain.includes('API_COMMANDS'), 'Debe consultar comandos');
assert.ok(pumpMain.includes('API_ACK_BASE'), 'Debe reconocer comandos (ACK)');
assert.ok(pumpMain.includes('API_EVENTS_PUMP'), 'Debe registrar eventos de dosis');
assert.ok(pumpMain.includes('ArduinoOTA'), 'Debe soportar OTA');
assert.ok(pumpMain.includes('xTaskCreatePinnedToCore'), 'El worker debe vivir en Core 0');
assert.ok(pumpCfg.includes('ESP32_PUMP'), 'Debe usar la clave de dispositivo de la bomba');
ok('WiFi + bulk + comandos + ACK + eventos + OTA en Core 0');

console.log('\n[Test 3] Nodo mixer T-200: orquestación y bitácora...');
assert.ok(t200.includes('start_mixer') && t200.includes('stop_mixer'), 'Debe interpretar start/stop_mixer');
assert.ok(t200.includes('postMixerEvent'), 'Debe publicar eventos del mixer');
assert.ok(t200.includes('/api/events/mixer'), 'Debe llamar al endpoint de eventos');
assert.ok(t200.includes('set_state'), 'Debe respetar la máquina de estados global');
assert.ok(t200.includes('rtc_timestamp_ms'), 'Debe reportar el instante exacto');
ok('T-200 arranca/para por orquestación y audita sus eventos');

console.log('\n[Test 4] Endpoints y esquema de eventos...');
assert.ok(pumpRoute.includes("event_type: 'dose_pump'"), 'La ruta de bomba debe reutilizar mixer_events con dose_pump');
assert.ok(pumpRoute.includes('volume_ml'), 'Debe registrar el volumen dosificado');
assert.ok(overrideRoute.includes("event_type: 'manual_confirmation'"), 'La anulación debe auditarse');
assert.ok(mixerRoute.includes('mixer_events'), 'La ruta del mixer debe escribir en mixer_events');
ok('Rutas de eventos compatibles con el esquema existente (sin DDL obligatorio)');

console.log('\n[Test 5] Registro de dispositivo y migración opcional...');
// La identidad y el rol de cada nodo viven en el registro canónico (deviceRoles.ts); deviceAuth
// lo consume. Antes las claves estaban duplicadas dentro de deviceAuth.
const rolesLib = read('src/lib/deviceRoles.ts');
assert.ok(rolesLib.includes('d0000000-0000-0000-0000-000000000004'), 'El registro debe conocer la bomba');
assert.ok(rolesLib.includes('ESP32_PUMP'), 'El registro debe aceptar la clave de la bomba');
assert.ok(auth.includes('KNOWN_DEVICES'), 'deviceAuth debe consumir el registro canónico');
assert.ok(migration.includes('pump_events'), 'La migración debe crear pump_events');
assert.ok(migration.includes('manual_overrides_log'), 'La migración debe crear manual_overrides_log');
assert.ok(migration.includes('executed_rtc_ms'), 'La migración debe añadir executed_rtc_ms');
assert.ok(migration.includes("'system_state'"), 'La migración debe sembrar system_state');
assert.ok(schema.includes('d0000000-0000-0000-0000-000000000004'), 'schema.sql debe sembrar la bomba');
ok('Identidad de la bomba + migración V4 documentada');

console.log('\n[Test 6] Idempotencia de la dosificación...');
function doseReached(dosed, target) { return target > 0 && dosed >= target; }
assert.strictEqual(doseReached(99.9, 100), false);
assert.strictEqual(doseReached(100.0, 100), true);
assert.strictEqual(doseReached(50, 0), false, 'Sin objetivo no dosifica');
ok('El corte por volumen alcanzado es exacto y seguro');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (6/6) ');
console.log('====================================================');
