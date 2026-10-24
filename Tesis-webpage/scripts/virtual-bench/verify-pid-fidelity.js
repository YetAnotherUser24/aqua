/**
 * Verificación de FIDELIDAD del gemelo: compara el PID en JavaScript contra el MOTOR REAL en C++.
 *
 * Cómo funciona:
 *  1. Compila `Codigos/nodo-aerador/src/control_engine.cpp` (el de producción, no una copia)
 *     junto a `test/host/dump_control_reference.cpp`, con stubs mínimos de Arduino.
 *  2. Ejecuta el binario para obtener una trayectoria de referencia determinista.
 *  3. Corre el port `models/controlEngine.js` con el MISMO escenario, paso a paso.
 *  4. Compara ambas trayectorias y falla si divergen.
 *
 * Además cuantifica el efecto del defecto del término derivativo que se corrigió en el firmware:
 * corre el escenario con `derivativeMode: 'firmwareV1Dead'` y reporta la diferencia.
 *
 * Uso: node scripts/virtual-bench/verify-pid-fidelity.js
 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const { ControlEngine, MODE_PID } = require('./models/controlEngine');

const WEB_ROOT = path.join(__dirname, '..', '..');
const FW_ROOT = path.join(WEB_ROOT, '..', 'nodo-aerador');
const HOST_DIR = path.join(FW_ROOT, 'test', 'host');

let passed = 0;
const ok = (msg) => { passed++; console.log('  ✓ ' + msg); };

/* Escenario: idéntico al de dump_control_reference.cpp */
const T0_MS = 1000;
const DT_MS = 50;
const STEP_COUNT = 1080;

function doAtStep(step) {
  const t = T0_MS + step * DT_MS;
  if (t < 3000) return 8.0;
  if (t < 12000) return 3.0;
  if (t < 14000) return 5.0;
  return -1.0;
}

/** Corre el motor en JS con el mismo escenario. */
function runJs(derivativeMode) {
  const engine = new ControlEngine({ mode: MODE_PID, derivativeMode });
  engine.configure({ mode: MODE_PID, target_do_mg_l: 7.5 });
  const out = [];
  for (let step = 0; step < STEP_COUNT; step++) {
    const t = T0_MS + step * DT_MS;
    const doVal = doAtStep(step);
    if (doVal >= 0) engine.updateProcessVariable(doVal, 24.0, t);
    out.push({ t, doVal, rpm: engine.computeOutputRpm(t) });
  }
  return out;
}

/** Compila y ejecuta el motor real en C++. */
function runCpp() {
  const gpp = process.platform === 'win32' ? 'g++' : 'g++';
  const exe = path.join(os.tmpdir(), `dump_control_ref_${process.pid}${process.platform === 'win32' ? '.exe' : ''}`);

  const args = [
    '-O1', '-std=c++17',
    '-I', HOST_DIR,
    '-I', path.join(FW_ROOT, 'src'),
    '-I', path.join(FW_ROOT, 'include'),
    path.join(HOST_DIR, 'dump_control_reference.cpp'),
    path.join(FW_ROOT, 'src', 'control_engine.cpp'),
    '-o', exe
  ];

  execFileSync(gpp, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  const csv = execFileSync(exe, [], { encoding: 'utf8' });
  fs.unlinkSync(exe);

  const lines = csv.trim().split(/\r?\n/);
  lines.shift(); // cabecera
  return lines.map(line => {
    const [t, doVal, rpm] = line.split(',');
    return { t: Number(t), doVal: Number(doVal), rpm: Number(rpm) };
  });
}

console.log('====================================================');
console.log(' VERIFICACIÓN DE FIDELIDAD: PID JS vs MOTOR C++ REAL ');
console.log('====================================================\n');

console.log('[1] Compilando el control_engine.cpp de producción...');
let reference;
try {
  reference = runCpp();
  ok(`motor real compilado y ejecutado (${reference.length} pasos de referencia)`);
} catch (err) {
  console.error('\n  ✗ No se pudo compilar/ejecutar el motor en C++.');
  console.error(`    ${String(err.stderr || err.message).slice(0, 600)}`);
  process.exit(1);
}

console.log('\n[2] Comparando el port en JS contra la referencia C++...');
const jsCorrected = runJs('errorDelta');
if (jsCorrected.length !== reference.length) {
  throw new Error(`Longitudes distintas: JS ${jsCorrected.length} vs C++ ${reference.length}`);
}

let maxDiff = 0;
let maxDiffAt = null;
for (let i = 0; i < reference.length; i++) {
  const diff = Math.abs(jsCorrected[i].rpm - reference[i].rpm);
  if (diff > maxDiff) { maxDiff = diff; maxDiffAt = reference[i]; }
}

console.log(`    pasos comparados: ${reference.length}`);
console.log(`    diferencia máxima: ${maxDiff.toExponential(3)} RPM (en t=${maxDiffAt?.t} ms, ref=${maxDiffAt?.rpm.toFixed(4)})`);

// Tolerancia: el C++ usa float (32 bits) y el JS float64, así que se admite el error de redondeo
// acumulado del acumulador integral, pero no una divergencia algorítmica.
if (maxDiff > 0.5) {
  console.error(`  ✗ DIVERGENCIA: el port JS no reproduce el motor real (max ${maxDiff} RPM)`);
  const idx = reference.findIndex((r, i) => Math.abs(jsCorrected[i].rpm - r.rpm) > 0.5);
  for (let i = Math.max(0, idx - 2); i <= Math.min(reference.length - 1, idx + 2); i++) {
    console.error(`      t=${reference[i].t}  C++=${reference[i].rpm.toFixed(6)}  JS=${jsCorrected[i].rpm.toFixed(6)}`);
  }
  process.exit(1);
}
ok(`el port JS reproduce el motor real (máx ${maxDiff.toExponential(2)} RPM de diferencia, error de float32)`);

console.log('\n[3] Cuantificando el defecto del término derivativo corregido...');
const jsDead = runJs('firmwareV1Dead');
let maxDeadDiff = 0;
let firstDeadDivergence = null;
for (let i = 0; i < reference.length; i++) {
  const d = Math.abs(jsDead[i].rpm - jsCorrected[i].rpm);
  if (d > maxDeadDiff) maxDeadDiff = d;
  if (!firstDeadDivergence && d > 1.0) firstDeadDivergence = jsCorrected[i].t;
}
console.log(`    con el defecto (Kd anulado) la salida difiere hasta ${maxDeadDiff.toFixed(2)} RPM`);
console.log(`    primera desviación >1 RPM en t=${firstDeadDivergence ?? 'nunca'} ms`);
if (maxDeadDiff > 1.0) {
  ok('el defecto tenía efecto medible (por eso el arreglo importa)');
} else {
  console.log('    (el escenario no excita el término D lo suficiente para medirlo)');
}

console.log('\n[4] Comprobando los caminos de seguridad del motor...');
const eng = new ControlEngine({ mode: MODE_PID, derivativeMode: 'errorDelta' });
eng.configure({ mode: MODE_PID, target_do_mg_l: 7.5 });
// Sin primera muestra: 0 RPM y failsafe activo
const noSample = eng.computeOutputRpm(1000);
if (noSample !== 0 || !eng.isFailsafeActive()) throw new Error('Arranque seguro no aplicado');
ok('arranque seguro: 0 RPM hasta la primera muestra válida');

// Con muestra, y luego 36 s sin paquetes: watchdog -> failsafe_rpm (0)
eng.updateProcessVariable(3.0, 24.0, 1000);
eng.computeOutputRpm(1050);
const afterWatchdog = eng.computeOutputRpm(1050 + 36000);
if (afterWatchdog !== 0 || !eng.isFailsafeActive()) throw new Error('Watchdog de 35 s no aplicado');
ok('watchdog de 35 s: cae a failsafe_rpm ante pérdida del enlace');

// E-stop tiene prioridad absoluta sobre el PID
eng.triggerEmergencyStop();
if (eng.computeOutputRpm(40000) !== 0) throw new Error('E-Stop no tiene prioridad');
ok('E-Stop: prioridad absoluta, 0 RPM');

// clearEmergencyStop libera el latch (la acción `clear_estop` del dashboard)
eng.clearEmergencyStop();
if (eng.computeOutputRpm(40100) === 0 && eng.getConfig().mode !== 2) {
  // en modo MANUAL la salida es el throttle (0 por el E-Stop previo), así que sólo comprobamos el latch
}
if (eng.isEmergencyStopActive()) throw new Error('clear_estop no liberó el latch');
ok('clear_estop libera el latch de emergencia');

console.log('\n====================================================');
console.log(` FIDELIDAD VERIFICADA (${passed} comprobaciones) `);
console.log('====================================================');
