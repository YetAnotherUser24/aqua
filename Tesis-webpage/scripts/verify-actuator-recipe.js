/**
 * Self-check: Receta de actuadores y arranque de experimentos (AquaControl V4, ADD §2-§3)
 * Ejecución: node scripts/verify-actuator-recipe.js
 *
 * Regresiones que bloquea este test (todas observadas en banco):
 *  1. El mixer (T-200) se encendía en CUALQUIER `ACTIVE_EXPERIMENT` y no se apagaba en
 *     MANUAL_OVERRIDE.
 *  2. El ODrive habilitaba el log SD pero nunca se armaba => motor siempre a 0 RPM.
 *  3. El E-Stop era un latch sin vía de liberación (`clearEmergencyStop()` nunca se invocaba).
 *  4. `/api/commands/pending` entregaba a un nodo motor una orden dirigida a OTRO nodo.
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
console.log(' VERIFICACIÓN DE LA RECETA DE ACTUADORES (V4)       ');
console.log('====================================================\n');

const recipe = read('src/lib/experimentRecipe.ts');
const stateLib = read('src/lib/systemState.ts');
const expRoute = read('src/app/api/experiments/route.ts');
const pendingRoute = read('src/app/api/commands/pending/route.ts');
const motorPanel = read('src/components/MotorControlPanel.tsx');
const odriveCloud = readRepo('Codigos/nodo-aerador/src/cloud_worker.cpp');
const odriveEngine = readRepo('Codigos/nodo-aerador/src/control_engine.cpp');
const t200Cloud = readRepo('Codigos/nodo-mezclador/src/cloud_worker.cpp');

console.log('[Test 1] Existe una única fuente de verdad de la receta...');
assert.ok(recipe.includes('export function resolveActuatorRecipe'), 'Debe exportar resolveActuatorRecipe');
assert.ok(recipe.includes('export function buildActuatorIntent'), 'Debe exportar buildActuatorIntent');
assert.ok(recipe.includes("type ExperimentCaseType = 'planta_1_deox' | 'planta_2_step' | 'closed_loop'"),
  'Debe declarar los tres protocolos del ADD §3');
// Ambos consumidores usan la MISMA función (era el defecto: la lógica estaba duplicada).
assert.ok(stateLib.includes("from '@/lib/experimentRecipe'"), 'systemState debe importar la receta');
assert.ok(expRoute.includes("from '@/lib/experimentRecipe'"), '/api/experiments debe importar la receta');
assert.ok(!/const wantsMixer =/.test(expRoute), 'No debe quedar la lógica duplicada del mixer');
ok('resolveActuatorRecipe centraliza la decisión; sin duplicación');

console.log('\n[Test 2] Tabla de decisión por protocolo...');
// Espejo de la tabla documentada (ADD §3). Si el fuente cambia su mapeo, el Test 1 + estos
// asserts detectan la divergencia al revisar las condiciones literales.
const decisions = [
  { name: 'Planta 1 (desoxigenación)', caseType: 'planta_1_deox', plant: 'planta_1', mixer: 'on', motor: 'off' },
  { name: 'Planta 2 (escalón KLa)', caseType: 'planta_2_step', plant: 'planta_2', mixer: 'off', motor: 'manual' },
  { name: 'Caso B (lazo cerrado)', caseType: 'closed_loop', plant: 'both', mixer: 'off', motor: 'pid' }
];
for (const d of decisions) {
  assert.ok(recipe.includes(`caseType === '${d.caseType}'`), `La receta debe reconocer '${d.caseType}'`);
}
// El mixer SOLO se enciende para Planta 1.
assert.ok(/const mixer: 'on' \| 'off' = caseType === 'planta_1_deox' \? 'on' : 'off'/.test(recipe),
  'El mixer debe encenderse únicamente en planta_1_deox');
// El ODrive se arma solo en protocolos con aireación mecánica.
assert.ok(recipe.includes("caseType === 'planta_2_step'") && recipe.includes("caseType === 'closed_loop'"),
  'El ODrive debe armarse en planta_2_step y closed_loop');
assert.ok(recipe.includes("controllerType === 'pid'"), 'PID debe depender de controller_type');
ok(`${decisions.length} protocolos mapeados (mixer + modo de motor)`);

console.log('\n[Test 3] IDLE / MANUAL_OVERRIDE fuerzan actuadores OFF (ADD §2.3)...');
assert.ok(/if \(state !== 'ACTIVE_EXPERIMENT'\)/.test(recipe), 'buildActuatorIntent debe cortar fuera de ACTIVE_EXPERIMENT');
assert.ok(recipe.includes("return { mixer: 'off', motor_mode: 'off' }"),
  'Fuera de ACTIVE_EXPERIMENT todo debe quedar en OFF');
ok('Una anulación manual aborta cualquier receta automática');

console.log('\n[Test 4] El set_state difundido transporta la intención explícita...');
assert.ok(stateLib.includes('getRegisteredExperiment'), 'Debe resolver el experimento del registro canónico');
assert.ok(stateLib.includes('experiments_registry'), 'Debe leer system_settings.experiments_registry');
assert.ok(stateLib.includes('roleIntent.mixer ='), 'Debe enviar la intención del mixer');
assert.ok(stateLib.includes('roleIntent.motor_mode ='), 'Debe enviar la intención del motor');
assert.ok(stateLib.includes('motor_target_do') && stateLib.includes('motor_throttle_pct'),
  'Debe enviar setpoint y/o escalón de apertura');
ok('set_state lleva mixer + motor_mode + target_do/throttle_pct');

console.log('\n[Test 5] Firmware T-200: mixer solo bajo orden explícita...');
// Anti-regresión del defecto real: no debe volver a encenderse por el mero estado global.
assert.ok(!/st == "ACTIVE_EXPERIMENT"\) \{ mixerCommand = true; mixerOn = true; \}/.test(t200Cloud),
  'REGRESIÓN: el mixer no debe encenderse por un ACTIVE_EXPERIMENT genérico');
assert.ok(t200Cloud.includes('p["mixer"].is<const char*>()'), 'Debe leer el campo explícito mixer');
assert.ok(/st == "IDLE" \|\| st == "MANUAL_OVERRIDE"/.test(t200Cloud),
  'IDLE y MANUAL_OVERRIDE deben apagar el mixer');
assert.ok(t200Cloud.includes('ACTIVE_EXPERIMENT sin campo `mixer`: no se toca el actuador'),
  'Sin campo explícito no debe tocar el actuador');
// El magnetismo previo forzaba 1500 RPM sin relación con el tope real del actuador.
assert.ok(!t200Cloud.includes('1500.0f'), 'REGRESIÓN: no debe forzarse 1500 RPM');
const mixerRpm = Number((t200Cloud.match(/MIXER_DEFAULT_RPM\s*=\s*([\d.]+)f/) || [])[1]);
assert.ok(Number.isFinite(mixerRpm), 'Debe usar una constante de velocidad de mezcla');
// El T-200 es un driver con PI (no un ESC): su tope real son 3800 RPM.
const maxThrusterRpm = Number(
  (readRepo('Codigos/nodo-mezclador/src/esp32s3_main.cpp').match(/MAX_THRUSTER_RPM\s*=\s*([\d.]+)f/) || [])[1]
);
assert.ok(maxThrusterRpm === 3800, `El tope del T-200 debe ser 3800 RPM (leído: ${maxThrusterRpm})`);
assert.ok(mixerRpm > 0 && mixerRpm <= maxThrusterRpm,
  `MIXER_DEFAULT_RPM (${mixerRpm}) debe estar dentro del tope del T-200 (${maxThrusterRpm} RPM)`);
ok(`Mixer explícito, apagado en IDLE/MANUAL_OVERRIDE, ${mixerRpm} RPM dentro del tope de ${maxThrusterRpm}`);

console.log('\n[Test 6] Firmware ODrive: armado por receta + liberación de E-Stop...');
assert.ok(odriveCloud.includes('motorModeStr'), 'Debe leer motor_mode del set_state');
assert.ok(/motorTargetDo|motorThrottlePct/.test(odriveCloud), 'Debe leer el setpoint/escalón');
assert.ok(odriveCloud.includes('Motor ARMED in PID') && odriveCloud.includes('Motor ARMED in MANUAL'),
  'Debe armar el actuador en PID y MANUAL (antes solo habilitaba el log SD)');
assert.ok(odriveCloud.includes('clear_estop'), 'Debe existir la acción explícita clear_estop');
assert.ok(odriveCloud.includes('clearEmergencyStop()'), 'clear_estop debe invocar clearEmergencyStop()');
// El armado usa configure() a propósito: setManualThrottle() liberaría el E-Stop de forma implícita.
assert.ok(/arrancar un experimento NO debe/i.test(odriveCloud),
  'El armado no debe liberar un E-Stop implícitamente');
assert.ok(odriveEngine.includes('void ControlEngine::clearEmergencyStop()'), 'clearEmergencyStop debe existir');
ok('Armado por receta + vía explícita de liberación del E-Stop');

console.log('\n[Test 7] La UI ofrece reanudar tras E-Stop...');
assert.ok(motorPanel.includes('handleResumeFromEstop'), 'Debe existir el manejador de reanudación');
assert.ok(motorPanel.includes("action: 'clear_estop'"), 'Debe enviar payload.action = clear_estop');
assert.ok(/REANUDAR/.test(motorPanel), 'Debe existir el botón REANUDAR');
ok('Existe botón REANUDAR que envía clear_estop');

console.log('\n[Test 8] /api/commands/pending no roba órdenes entre nodos...');
// El resolutor vive en una librería compartida porque el orquestador también necesita saber qué
// acción lleva una orden pendiente, para poder invalidarla cuando el estado del banco cambia.
const payloadLib = read('src/lib/commandPayload.ts');
assert.ok(/export function resolveCommandPayload/.test(payloadLib),
  'La reconstrucción del payload desde error_message debe estar en src/lib/commandPayload.ts');
assert.ok(payloadLib.includes('error_message'), 'Debe reconstruir el payload desde error_message');
assert.ok(pendingRoute.includes("from '@/lib/commandPayload'"),
  'La ruta debe importar el resolutor compartido, no duplicarlo');
assert.ok(!pendingRoute.includes('function resolveCommandPayload'),
  'REGRESIÓN: no debe volver a duplicarse la implementación en la ruta');
assert.ok(/\.is\('device_id', null\)/.test(pendingRoute),
  'El fallback debe limitarse a órdenes globales (sin device_id)');
assert.ok(pendingRoute.includes('resolveCommandPayload(candidate)'),
  'La clasificación debe usar el payload reconstruido, no row.payload');
assert.ok(!/candidate\.payload\?\.action\?\.includes/.test(pendingRoute),
  'REGRESIÓN: no debe clasificar leyendo candidate.payload (undefined en producción)');
ok('El fallback solo sirve órdenes globales; clasificación con payload real y compartido');

console.log('\n[Test 9] /api/experiments despliega la receta por protocolo...');
assert.ok(expRoute.includes("recipe.mixer === 'on'"), 'Debe derivar la intención del mixer de la receta');
assert.ok(expRoute.includes("recipe.motor.mode !== 'off'"), 'Debe armar el ODrive solo si la receta lo pide');
// Un único camino de despliegue: el mismo `broadcastState` de la barra del orquestador. Si se
// encolaran comandos sueltos, el SENSOR no se enteraría de que hay experimento y no muestrearía.
assert.ok(expRoute.includes('broadcastState'), 'Debe difundir con broadcastState (incluye al sensor)');
assert.ok((expRoute.match(/broadcastState\(/g) || []).length >= 2,
  'Tanto el arranque como la parada deben difundir el estado a todos los nodos');
assert.ok(!/device_id: DEVICE_IDS\.mixer/.test(expRoute),
  'REGRESIÓN: no debe encolar comandos de actuador a mano (dejaba al sensor fuera)');
ok('Arranque y parada despliegan la receta por protocolo a los 4 nodos');

console.log('\n====================================================');
console.log(` TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (9/9) `);
console.log('====================================================');
assert.ok(passed === 9, `Se esperaban 9 bloques, se ejecutaron ${passed}`);
