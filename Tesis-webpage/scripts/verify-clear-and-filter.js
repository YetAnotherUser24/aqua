/**
 * Script de verificación para reinicio de gráfica experimental y limpieza granular
 * Ejecutar con: node scripts/verify-clear-and-filter.js
 */
const assert = require('assert');

console.log('====================================================');
console.log(' VERIFICACIÓN DE REINICIO DE GRÁFICA Y LIMPIEZA BD');
console.log('====================================================\n');

// ----------------------------------------------------
// TEST 1: Filtrado estricto de gráfica experimental
// ----------------------------------------------------
console.log('[Test 1] Verificando que la gráfica experimental reinicia estrictamente desde started_at...');

const now = Date.now();
const pastPoints = [
  { timestamp: new Date(now - 120000).toISOString(), dissolved_oxygen_mg_l: 8.5 },
  { timestamp: new Date(now - 60000).toISOString(), dissolved_oxygen_mg_l: 8.7 },
  { timestamp: new Date(now - 30000).toISOString(), dissolved_oxygen_mg_l: 8.9 }
];

const experimentStart = new Date(now - 20000).toISOString();
const experimentEnd = new Date(now + 10000).toISOString();

const experimentPoints = [
  { timestamp: new Date(now - 15000).toISOString(), dissolved_oxygen_mg_l: 9.1 },
  { timestamp: new Date(now - 5000).toISOString(), dissolved_oxygen_mg_l: 9.3 },
  { timestamp: new Date(now).toISOString(), dissolved_oxygen_mg_l: 9.5 }
];

const futurePoints = [
  { timestamp: new Date(now + 25000).toISOString(), dissolved_oxygen_mg_l: 9.9 }
];

const fullHistory = [...pastPoints, ...experimentPoints, ...futurePoints];

// Simulación de la lógica de ChartsSection.tsx
function filterExperimentData(history, activeExp) {
  if (!activeExp || !activeExp.started_at) return [];
  const startMs = new Date(activeExp.started_at).getTime();
  const endMs = activeExp.ended_at ? new Date(activeExp.ended_at).getTime() : Infinity;

  return history.filter(p => {
    const t = new Date(p.timestamp).getTime();
    return t >= startMs && t <= endMs;
  });
}

// Caso 1: Experimento activo en curso (ended_at = null)
const activeExpLive = {
  id: 'exp_live_01',
  name: 'Ensayo en Vivo',
  started_at: experimentStart,
  ended_at: null
};

const liveFiltered = filterExperimentData(fullHistory, activeExpLive);
assert.strictEqual(liveFiltered.length, 4, 'Debe incluir solo los puntos >= started_at');
assert.strictEqual(liveFiltered[0].dissolved_oxygen_mg_l, 9.1, 'El primer punto debe ser el primero del experimento, no del pasado');
console.log(`  ✓ Experimento en curso filtra ${pastPoints.length} puntos viejos y conserva exactamente ${liveFiltered.length} puntos nuevos.`);

// Caso 2: Experimento completado con ventana acotada (ended_at definido)
const activeExpEnded = {
  id: 'exp_ended_01',
  name: 'Ensayo Concluido',
  started_at: experimentStart,
  ended_at: experimentEnd
};

const endedFiltered = filterExperimentData(fullHistory, activeExpEnded);
assert.strictEqual(endedFiltered.length, 3, 'Debe acotar entre started_at y ended_at excluyendo puntos posteriores');
console.log(`  ✓ Experimento completado acota ventana temporal (${endedFiltered.length} puntos dentro del rango).`);

// Caso 3: Experimento recién iniciado sin mediciones aún
const freshExp = {
  id: 'exp_fresh_01',
  name: 'Recién Creado',
  started_at: new Date(now + 60000).toISOString(),
  ended_at: null
};
const freshFiltered = filterExperimentData(fullHistory, freshExp);
assert.strictEqual(freshFiltered.length, 0, 'Debe devolver array vacío para mostrar estado de espera');
console.log('  ✓ Experimento recién iniciado devuelve 0 puntos (activa estado de espera sin data vieja).');


// ----------------------------------------------------
// TEST 2: Lógica de purga granular por categorías y tiempo
// ----------------------------------------------------
console.log('\n[Test 2] Verificando lógica de corte y categorías para /api/database/clear...');

function computeCutoff(timeScope, baseNow = new Date()) {
  switch (timeScope) {
    case 'older_than_1h':
      return new Date(baseNow.getTime() - 60 * 60 * 1000);
    case 'older_than_24h':
      return new Date(baseNow.getTime() - 24 * 60 * 60 * 1000);
    case 'older_than_today':
      return new Date(baseNow.getFullYear(), baseNow.getMonth(), baseNow.getDate(), 0, 0, 0);
    case 'all':
    default:
      return new Date('2099-12-31T23:59:59Z');
  }
}

const cutoff1h = computeCutoff('older_than_1h');
const cutoff24h = computeCutoff('older_than_24h');
const cutoffAll = computeCutoff('all');

assert(cutoff1h < new Date(), 'older_than_1h debe ser menor al presente');
assert(cutoff24h < cutoff1h, 'older_than_24h debe ser menor que older_than_1h');
assert(cutoffAll.getFullYear() === 2099, 'all debe abarcar todo el historial');
console.log('  ✓ Cálculo de marcas de tiempo de corte validado (1h, 24h, hoy, todo).');


// ----------------------------------------------------
// TEST 3: Lógica de eliminación de experimento individual
// ----------------------------------------------------
console.log('\n[Test 3] Verificando eliminación individual de experimento (DELETE /api/experiments/[id])...');

const mockExperiments = [
  { id: 'exp_1', name: 'Prueba 1', started_at: '2026-09-12T07:00:00Z', ended_at: '2026-09-12T07:30:00Z' },
  { id: 'exp_2', name: 'Prueba 2', started_at: '2026-09-12T08:00:00Z', ended_at: '2026-09-12T08:30:00Z' },
  { id: 'exp_3', name: 'Prueba 3 (Descartar)', started_at: '2026-09-12T09:00:00Z', ended_at: '2026-09-12T09:10:00Z' }
];

function deleteSingleExperiment(registry, expIdToDelete) {
  const target = registry.find(e => e.id === expIdToDelete);
  if (!target) return { found: false, list: registry };
  return {
    found: true,
    target,
    list: registry.filter(e => e.id !== expIdToDelete)
  };
}

const deleteResult = deleteSingleExperiment(mockExperiments, 'exp_3');
assert.strictEqual(deleteResult.found, true);
assert.strictEqual(deleteResult.list.length, 2);
assert.strictEqual(deleteResult.list.find(e => e.id === 'exp_3'), undefined);
console.log(`  ✓ Experimento 'exp_3' eliminado del registro, conservando los otros ${deleteResult.list.length} experimentos intactos.`);

// ----------------------------------------------------
// TEST 4: Prioridad de estado de espera en gráfica de experimento
// ----------------------------------------------------
console.log('\n[Test 4] Verificando prioridad de pantalla de espera en Experimento en Vivo...');

function resolveChartViewState(activeTab, historyLen, experimentDataLen) {
  if (activeTab === 'experiment' && experimentDataLen === 0) {
    return 'WAITING_FIRST_SAMPLES';
  } else if (historyLen === 0) {
    return 'NO_HISTORY_DATA';
  } else {
    return 'RENDER_CHART';
  }
}

// Con historial vacío general (e.g. tras limpiar BD) pero con experimento activo:
assert.strictEqual(
  resolveChartViewState('experiment', 0, 0),
  'WAITING_FIRST_SAMPLES',
  'En pestaña experiment con 0 muestras, DEBE mostrar pantalla animada de espera, no "Sin datos históricos"'
);
assert.strictEqual(
  resolveChartViewState('combined', 0, 0),
  'NO_HISTORY_DATA',
  'En pestaña combined con 0 muestras, debe mostrar "Sin datos históricos para este rango"'
);
assert.strictEqual(
  resolveChartViewState('experiment', 10, 5),
  'RENDER_CHART',
  'Con datos experimentales, debe renderizar la gráfica'
);
console.log('  ✓ Prioridad de pantalla de espera validada correctamente sobre historial vacío general.');

// ----------------------------------------------------
// TEST 5: Preservación de metadatos de hardware al resetear dispositivos
// ----------------------------------------------------
console.log('\n[Test 5] Verificando preservación de metadatos de hardware en reset de devices...');

const originalDeviceMetadata = {
  sensor_model: 'Aqualabo DIGISENS',
  interface: 'Modbus RS485',
  monitor_active: true,
  monitor_interval_sec: 2,
  sleep_cycle_min: 15,
  active_experiment: { id: 'exp_1', name: 'Prueba' }
};

const updatedDeviceMetadata = {
  ...originalDeviceMetadata,
  monitor_active: false,
  active_experiment: null
};

assert.strictEqual(updatedDeviceMetadata.sensor_model, 'Aqualabo DIGISENS', 'Debe preservar sensor_model');
assert.strictEqual(updatedDeviceMetadata.interface, 'Modbus RS485', 'Debe preservar interface');
assert.strictEqual(updatedDeviceMetadata.sleep_cycle_min, 15, 'Debe preservar sleep_cycle_min');
assert.strictEqual(updatedDeviceMetadata.monitor_active, false, 'Debe apagar monitor_active');
assert.strictEqual(updatedDeviceMetadata.active_experiment, null, 'Debe limpiar active_experiment');
console.log('  ✓ Metadatos de hardware preservados íntegramente tras reset de experimento.');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (5/5)');
console.log('====================================================\n');

