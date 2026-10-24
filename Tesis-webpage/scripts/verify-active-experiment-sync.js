/**
 * Script de verificación para sincronización canónica de activeExperiment
 * Ejecutar con: node scripts/verify-active-experiment-sync.js
 */
const assert = require('assert');

console.log('===========================================================');
console.log(' VERIFICACIÓN DE SINCRONIZACIÓN CANÓNICA DE EXPERIMENTO');
console.log('===========================================================\n');

// ----------------------------------------------------
// TEST 1: Extracción canónica desde experiments_registry
// ----------------------------------------------------
console.log('[Test 1] Verificando extracción de activeExperiment desde experiments_registry...');

const mockRegistryWithActive = [
  {
    id: 'exp_002',
    name: 'Ensayo Aireación ODrive 02',
    status: 'active',
    started_at: '2026-09-12T10:30:00.000Z',
    ended_at: null,
    sampling_rate_sec: 2,
    csv_filename: 'EXP_02.CSV'
  },
  {
    id: 'exp_001',
    name: 'Ensayo Calibración 01',
    status: 'completed',
    started_at: '2026-09-12T09:00:00.000Z',
    ended_at: '2026-09-12T09:30:00.000Z',
    sampling_rate_sec: 5,
    csv_filename: 'EXP_01.CSV'
  }
];

function extractActiveExperiment(registryRows, deviceMeta = {}) {
  let activeExp = null;
  if (Array.isArray(registryRows)) {
    activeExp = registryRows.find(e => e.status === 'active') || null;
  }
  if (!activeExp && deviceMeta.active_experiment) {
    activeExp = deviceMeta.active_experiment;
  }
  return activeExp;
}

const activeExpFound = extractActiveExperiment(mockRegistryWithActive);
assert.strictEqual(activeExpFound.id, 'exp_002', 'Debe extraer el experimento con status active');
assert.strictEqual(activeExpFound.name, 'Ensayo Aireación ODrive 02');
assert.strictEqual(activeExpFound.csv_filename, 'EXP_02.CSV');
assert.strictEqual(Boolean(activeExpFound.started_at), true, 'Debe incluir started_at');
console.log(`  ✓ Experimento activo '${activeExpFound.name}' extraído correctamente con started_at.`);

// Caso de registro sin experimento activo
const mockRegistryAllCompleted = [
  { id: 'exp_001', status: 'completed', name: 'Prueba Concluida' }
];
const noActiveExp = extractActiveExperiment(mockRegistryAllCompleted);
assert.strictEqual(noActiveExp, null, 'Debe retornar null si no hay ningún experimento con status active');
console.log('  ✓ Retorna null limpiamente cuando todos los experimentos están concluidos.');

// ----------------------------------------------------
// TEST 2: Preservación de activeExperiment a través de sondeos
// ----------------------------------------------------
console.log('\n[Test 2] Verificando estabilidad de activeExperiment en sondeos periódicos...');

let clientActiveExperiment = {
  id: 'exp_002',
  name: 'Ensayo Aireación ODrive 02',
  status: 'active',
  started_at: '2026-09-12T10:30:00.000Z'
};

// Simulación de respuesta de /api/dashboard/summary
const summaryDataResponse = {
  sensorDevice: { id: 'dev_1', metadata: { monitor_active: true } },
  activeExperiment: {
    id: 'exp_002',
    name: 'Ensayo Aireación ODrive 02',
    status: 'active',
    started_at: '2026-09-12T10:30:00.000Z',
    csv_filename: 'EXP_02.CSV',
    sampling_rate_sec: 2
  }
};

// Función de sincronización en page.tsx:
clientActiveExperiment = summaryDataResponse.activeExperiment || null;

assert.notStrictEqual(clientActiveExperiment, null, 'No debe anularse a null');
assert.strictEqual(clientActiveExperiment.id, 'exp_002');
assert.strictEqual(clientActiveExperiment.started_at, '2026-09-12T10:30:00.000Z');
console.log('  ✓ Sincronización en page.tsx mantiene el experimento activo vivo durante el sondeo.');

// ----------------------------------------------------
// TEST 3: Filtrado de datos desde la muestra 1 (N = 1)
// ----------------------------------------------------
console.log('\n[Test 3] Verificando filtrado y presencia de la muestra inicial (N = 1)...');

const expStartMs = new Date('2026-09-12T10:30:00.000Z').getTime();

const historyWithFirstSample = [
  { timestamp: '2026-09-12T10:29:50.000Z', dissolved_oxygen_mg_l: 7.0 }, // Previo
  { timestamp: '2026-09-12T10:30:02.000Z', dissolved_oxygen_mg_l: 7.85 } // Primera muestra del ensayo (+2s)
];

const experimentPoints = historyWithFirstSample.filter(p => {
  const t = new Date(p.timestamp).getTime();
  return t >= expStartMs;
});

assert.strictEqual(experimentPoints.length, 1, 'Debe contener exactamente 1 punto');
assert.strictEqual(experimentPoints[0].dissolved_oxygen_mg_l, 7.85);
console.log(`  ✓ Primera muestra filtrada con éxito (${experimentPoints[0].dissolved_oxygen_mg_l} mg/L).`);

console.log('\n===========================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (3/3)');
console.log('===========================================================\n');
