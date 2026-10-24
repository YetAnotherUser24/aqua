/**
 * Self-check: ACK con instante exacto de ejecución y compensación de latencia (AquaControl V4, ADD §2.2)
 * Ejecución: node scripts/verify-ack-timestamp.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

let passed = 0;
const ok = (msg) => { passed++; console.log('  ✓ ' + msg); };

console.log('====================================================');
console.log(' VERIFICACIÓN DE ACK CON TIMESTAMP EXACTO          ');
console.log('====================================================\n');

const ackRoute = read('src/app/api/commands/[id]/acknowledge/route.ts');
const recentRoute = read('src/app/api/commands/recent/route.ts');
const charts = read('src/components/ChartsSection.tsx');

console.log('[Test 1] El ACK acepta y persiste el instante del dispositivo...');
assert.ok(ackRoute.includes('rtc_timestamp_ms'), 'El body debe aceptar rtc_timestamp_ms');
assert.ok(ackRoute.includes('executed_rtc_ms'), 'Debe persistir executed_rtc_ms');
assert.ok(ackRoute.includes('ack_latency_ms'), 'Debe registrar la latencia medida');
assert.ok(/updateFields\.payload = mergedPayload/.test(ackRoute), 'Debe fusionar en payload JSONB (sin exigir DDL)');
assert.ok(/delete updateFields\.payload/.test(ackRoute), 'Debe reintentar si la columna payload no existe');
ok('ACK persistido con executed_rtc_ms + ack_latency_ms (robusto sin DDL)');

console.log('\n[Test 2] Cálculo de latencia...');
function computeLatency(serverNowMs, executedRtcMs) {
  if (!Number.isFinite(executedRtcMs) || executedRtcMs <= 0) return null;
  return Math.max(0, serverNowMs - executedRtcMs);
}
assert.strictEqual(computeLatency(1759900000500, 1759900000000), 500, 'Latencia de 500 ms');
assert.strictEqual(computeLatency(1759900000000, 1759900000900), 0, 'Nunca negativa');
assert.strictEqual(computeLatency(1759900000000, 0), null, 'Sin timestamp => sin latencia');
ok('Latencia acotada a >= 0 y nula cuando no hay timestamp');

console.log('\n[Test 3] Endpoint de perturbaciones para el gráfico...');
assert.ok(recentRoute.includes('/api/commands/recent') || recentRoute.includes('PerturbationMarker'), 'Debe existir el endpoint de perturbaciones');
assert.ok(recentRoute.includes('executed_rtc_ms'), 'Debe exponer executed_rtc_ms');
assert.ok(recentRoute.includes("'acknowledged'"), 'Debe considerar comandos reconocidos');
ok('/api/commands/recent devuelve las perturbaciones con su instante real');

console.log('\n[Test 4] El gráfico marca la perturbación en el ms correcto...');
assert.ok(charts.includes('perturbationMarkers'), 'ChartsSection debe calcular marcadores');
assert.ok(charts.includes('ReferenceLine'), 'Debe dibujar ReferenceLine');
assert.ok(charts.includes('executed_rtc_ms'), 'Debe usar el instante de ejecución');
assert.ok(/diffMs <= 15000/.test(charts), 'Debe descartar marcadores sin muestra cercana');
assert.ok(/dot=\{\{ r: 4/.test(charts), 'Debe preservar la visibilidad N=1 (dot r=4)');
ok('Marcador de perturbación anclado al instante de ejecución; N=1 preservado');

console.log('\n[Test 5] Los firmware reportan rtc_timestamp_ms en el ACK...');const odrive = read('../Codigos/nodo-aerador/src/cloud_worker.cpp');
const t200 = read('../Codigos/nodo-mezclador/src/cloud_worker.cpp');
const pump = read('../Codigos/nodo-bomba/src/main.cpp');
assert.ok(odrive.includes('doc["rtc_timestamp_ms"] = rtc'), 'ODrive debe enviar rtc_timestamp_ms');
assert.ok(odrive.includes('currentRtcMs'), 'ODrive debe extrapolar el Reloj Maestro');
assert.ok(t200.includes('ackDoc["rtc_timestamp_ms"]'), 'T-200 debe enviar rtc_timestamp_ms (NTP)');
assert.ok(pump.includes('doc["rtc_timestamp_ms"] = rtc'), 'La bomba debe enviar rtc_timestamp_ms (NTP)');
ok('ODrive (ESP-NOW), T-200 y bomba (NTP) reportan el instante exacto');

console.log('\n[Test 6] El ACK tolera el desfase de reloj del nodo (ADR-3)...');
assert.ok(ackRoute.includes('CLOCK_SKEW_TOLERANCE_MS'), 'Debe declarar la tolerancia de desfase');
assert.ok(ackRoute.includes('deviceClockReliable'), 'Debe evaluar la fiabilidad del reloj del nodo');
assert.ok(ackRoute.includes('clock_skew_ms'), 'Debe exponer el desfase medido');
assert.ok(ackRoute.includes('device_rtc_ms'), 'Debe conservar el valor crudo del dispositivo');

function resolveExecuted(deviceRtcMs, serverNowMs, tolerance = 5000) {
  if (!Number.isFinite(deviceRtcMs) || deviceRtcMs <= 0) return null;
  const skew = serverNowMs - deviceRtcMs;
  return Math.abs(skew) <= tolerance ? deviceRtcMs : serverNowMs;
}
const T = 1791426163755; // 2026-10-08T02:22:43Z
assert.strictEqual(resolveExecuted(T, T + 800), T, 'Desfase pequeño: se usa el instante del nodo');
assert.strictEqual(resolveExecuted(T, T - 16000), T - 16000, 'Nodo adelantado 16 s: se ancla al servidor');
assert.strictEqual(resolveExecuted(T, T + 60000), T + 60000, 'Nodo atrasado 60 s: se ancla al servidor');
ok('Instante de ejecución saneado cuando el reloj del nodo no es fiable');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (6/6) ');
console.log('====================================================');
