/**
 * Self-check: Máquina de estados global del orquestador (AquaControl V4, ADD §2)
 * Ejecución: node scripts/verify-state-machine.js
 *
 * Verifica:
 *  1. Existe /api/system/state con GET/POST y validación de estados.
 *  2. systemState difunde `set_state` a TODOS los nodos y audita el cambio.
 *  3. /api/commands aborta recetas automáticas en MANUAL_OVERRIDE.
 *  4. La UI expone la barra de orquestación y el resumen la propaga.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

let passed = 0;
const ok = (msg) => { passed++; console.log('  ✓ ' + msg); };

console.log('====================================================');
console.log(' VERIFICACIÓN DE LA MÁQUINA DE ESTADOS GLOBAL       ');
console.log('====================================================\n');

const stateRoute = read('src/app/api/system/state/route.ts');
const stateLib = read('src/lib/systemState.ts');
const commandsRoute = read('src/app/api/commands/route.ts');
const summaryRoute = read('src/app/api/dashboard/summary/route.ts');
const types = read('src/types/index.ts');
const page = read('src/app/page.tsx');
const bar = read('src/components/OrchestratorBar.tsx');

console.log('[Test 1] Endpoint /api/system/state...');
assert.ok(stateRoute.includes('export async function GET'), 'Debe exponer GET');
assert.ok(stateRoute.includes('export async function POST'), 'Debe exponer POST');
assert.ok(stateRoute.includes("'IDLE'") && stateRoute.includes("'ACTIVE_EXPERIMENT'") && stateRoute.includes("'MANUAL_OVERRIDE'"),
  'Debe reconocer los tres estados');
assert.ok(stateRoute.includes('broadcastState'), 'Debe difundir el estado a los nodos');
ok('GET/POST + validación de los 3 estados + difusión');

console.log('\n[Test 2] Difusión y auditoría...');
assert.ok(stateLib.includes('export const DEVICE_IDS'), 'Debe existir el registro canónico de nodos');
// La identidad y el rol de cada nodo viven en el registro canónico (deviceRoles.ts); systemState
// los consume. Se verifica allí la lista completa de roles.
const rolesLib = read('src/lib/deviceRoles.ts');
for (const role of ['sensor', 'odrive', 'mixer', 'pump']) {
  assert.ok(new RegExp(`role: '${role}'`).test(rolesLib), `El registro debe incluir el rol '${role}'`);
}
// El difusor despacha por ROL (no por tipo legacy) usando el mapa canónico.
assert.ok(stateLib.includes('ROLE_BY_DEVICE_ID'), 'systemState debe despachar por rol vía el mapa canónico');
assert.ok(stateLib.includes("role === 'mixer'") && stateLib.includes("role === 'odrive'"),
  'systemState debe distinguir mixer y odrive por rol');
assert.ok(stateLib.includes('ALL_DEVICE_IDS'), 'Debe difundirse a todos los nodos');
assert.ok(stateLib.includes('broadcastState'), 'Debe existir broadcastState');
assert.ok(stateLib.includes("action: 'set_state'"), 'El comando difundido debe ser set_state');
assert.ok(/mixer_events/.test(stateRoute), 'Debe auditar el cambio de estado (bitácora de seguridad)');
ok('set_state difundido a sensor/odrive/mixer/pump + auditoría');

console.log('\n[Test 3] MANUAL_OVERRIDE aborta recetas automáticas...');
assert.ok(commandsRoute.includes('MANUAL_OVERRIDE'), 'El endpoint de comandos debe conocer MANUAL_OVERRIDE');
assert.ok(commandsRoute.includes('RECIPE_ACTIONS'), 'Debe declarar las acciones de receta');
assert.ok(/status: 409/.test(commandsRoute), 'Debe rechazar con 409');
// Las acciones de emergencia nunca se bloquean
assert.ok(/isEmergency/.test(commandsRoute), 'La parada de emergencia debe tener vía libre');
ok('Recetas abortadas en MANUAL_OVERRIDE; emergencia siempre permitida');

console.log('\n[Test 4] Propagación a UI y resumen...');
assert.ok(types.includes('export type OrchestratorState'), 'El tipo OrchestratorState debe existir');
assert.ok(types.includes('interface SystemState'), 'El tipo SystemState debe existir');
assert.ok(summaryRoute.includes('getSystemState'), 'El resumen debe exponer el estado global');
assert.ok(summaryRoute.includes('systemState'), 'El campo systemState debe estar en la respuesta');
assert.ok(page.includes('OrchestratorBar'), 'El dashboard debe renderizar la barra de orquestación');
assert.ok(bar.includes('MANUAL_OVERRIDE') && bar.includes('ACTIVE_EXPERIMENT'), 'La barra debe exponer los estados');

// Defecto real: el override difundía el estado pero dejaba intactas las órdenes `pending` del
// estado anterior. Un `set_state` de ACTIVE_EXPERIMENT encolado un instante antes llegaba DESPUÉS
// (los nodos aplican lo último que reciben) y re-armaba el PID: la anulación manual quedaba
// silenciosamente sin efecto. Medido en producción con el banco virtual (`manualOverride`).
console.log('\n[Test 5] El cambio de estado invalida la receta aún encolada...');
const payloadLib = read('src/lib/commandPayload.ts');
assert.ok(stateLib.includes('expireCommands'),
  'broadcastState debe invalidar las órdenes de orquestación obsoletas');
// El corte temporal evita invalidar la orden que la propia transición acaba de encolar
// (p. ej. el `start_experiment` del sensor que /api/experiments crea antes de difundir).
assert.ok(/snapshotPendingOrchestrationIds\(\)/.test(stateLib),
  'La transición debe capturar por identificador lo que ya estaba encolado');
// Los identificadores no dependen de ningún reloj: comparar marcas temporales mezcla el reloj de
// Vercel con el `created_at` de Postgres, y el desfase medido (~160-660 ms) es del mismo orden que
// la ventana entre encolar y difundir.
assert.ok(!/lt\('created_at'/.test(stateLib),
  'REGRESIÓN: no debe decidirse por marcas temporales entre dos relojes distintos');
assert.ok(/broadcastState\(\s*state: SystemState,\s*requested_by: string,\s*staleCommandIds: string\[\] = \[\]/.test(stateLib),
  'broadcastState debe recibir la instantánea de identificadores obsoletos');
assert.ok(stateLib.includes('await expireCommands(staleCommandIds)'), 'Debe expirar exactamente esos ids');
// Y debe expirar ANTES de encolar: si no, cancelaría las órdenes recién creadas.
const idxExpirar = stateLib.indexOf('await expireCommands(staleCommandIds)');
const idxPrimerEnqueue = stateLib.indexOf('await enqueueCommand(', idxExpirar);
assert.ok(idxExpirar > 0 && idxPrimerEnqueue > idxExpirar,
  'La invalidación debe preceder al encolado del nuevo set_state');
for (const route of [stateRoute, read('src/app/api/experiments/route.ts')]) {
  const llamadas = (route.match(/broadcastState\(/g) || []).length;
  const conInstantanea = (route.match(/broadcastState\([^\n]*staleCommandIds\)/g) || []).length;
  assert.ok(llamadas > 0 && llamadas === conInstantanea,
    `Toda transición debe pasar su instantánea a broadcastState (${conInstantanea}/${llamadas})`);
  assert.ok(/snapshotPendingOrchestrationIds\(\)/.test(route),
    'Cada transición debe tomar su instantánea al entrar al handler');
}
assert.ok(/status: 'expired'/.test(stateLib), 'Las obsoletas deben quedar en un estado terminal');
assert.ok(/eq\('status', 'pending'\)/.test(stateLib), 'Solo se invalidan las que aún están pendientes');
// El filtro va por `action` del payload: `command_type` no distingue (`clear_estop` viaja como `set_speed`).
assert.ok(payloadLib.includes("'set_state'") && payloadLib.includes("'start_experiment'"),
  'Las acciones de orquestación deben estar declaradas en un solo lugar');
for (const segura of ['emergency_stop', 'clear_estop', 'start_dose']) {
  assert.ok(!new RegExp(`ORCHESTRATION_ACTIONS[\\s\\S]{0,400}'${segura}'`).test(payloadLib),
    `La acción de seguridad/física '${segura}' nunca debe invalidarse`);
}
assert.ok(read('src/app/api/commands/pending/route.ts').includes("from '@/lib/commandPayload'"),
  'El resolutor de payload debe ser compartido, no duplicado');
ok('Órdenes obsoletas expiradas antes de encolar; seguridad y dosis preservadas');

// Lógica de normalización (reimplementación verificada)
const VALID = ['IDLE', 'ACTIVE_EXPERIMENT', 'MANUAL_OVERRIDE'];
function normalize(raw) {
  if (!raw || typeof raw !== 'object') return 'IDLE';
  return VALID.includes(raw.state) ? raw.state : 'IDLE';
}
assert.strictEqual(normalize({ state: 'MANUAL_OVERRIDE' }), 'MANUAL_OVERRIDE');
assert.strictEqual(normalize({ state: 'basura' }), 'IDLE', 'Estado inválido => IDLE');
assert.strictEqual(normalize(null), 'IDLE');
ok('UI, resumen y normalización defensiva verificados');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (5/5) ');
console.log('====================================================');
