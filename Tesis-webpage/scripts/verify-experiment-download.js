/**
 * Script de verificación para la descarga de CSV de experimentos
 * Ejecutar con: node scripts/verify-experiment-download.js
 */
const assert = require('assert');

console.log('====================================================');
console.log(' VERIFICACIÓN DE DESCARGA CSV DE EXPERIMENTOS');
console.log('====================================================\n');

// 1. Simulación de datos registrados para Prueba3
const mockRegistry = [
  {
    id: "exp_1789203933660",
    name: "Prueba3",
    status: "completed",
    started_at: "2026-09-12T09:05:33.660Z",
    ended_at: "2026-09-12T09:11:17.214Z",
    csv_filename: "PRUEBA_3.CSV"
  },
  {
    id: "exp_1789203655075",
    name: "Prueba3",
    status: "completed",
    started_at: "2026-09-12T09:00:55.075Z",
    ended_at: "2026-09-12T09:04:57.554Z",
    csv_filename: "PRUEBA3.CSV"
  },
  {
    id: "exp_1789199537448",
    name: "Prueba2",
    status: "completed",
    started_at: "2026-09-12T07:52:17.448Z",
    ended_at: "2026-09-12T07:57:38.314Z",
    csv_filename: "PRUEBA_2.CSV"
  },
  {
    id: "exp_1789198638666",
    name: "Prueba1",
    status: "completed",
    started_at: "2026-09-12T07:37:18.666Z",
    ended_at: "2026-09-12T07:51:38.973Z",
    csv_filename: "PRUEBA.CSV"
  }
];

// 2. Mock de lecturas de sensor capturadas
const mockReadings = [
  {
    recorded_at: "2026-09-12T09:01:05.000Z",
    seconds_since_2000: 842501000,
    dissolved_oxygen_mg_l: 9.500,
    oxygen_saturation_pct: 77.0,
    water_temperature_c: 27.0,
    battery_v: 3.74,
    status: 0
  },
  {
    recorded_at: "2026-09-12T09:02:10.000Z",
    seconds_since_2000: 842501065,
    dissolved_oxygen_mg_l: 13.500,
    oxygen_saturation_pct: 82.0,
    water_temperature_c: 27.2,
    battery_v: 3.74,
    status: 0
  },
  {
    recorded_at: "2026-09-12T09:06:02.000Z",
    seconds_since_2000: 842501297,
    dissolved_oxygen_mg_l: 7.500,
    oxygen_saturation_pct: 70.0,
    water_temperature_c: 28.0,
    battery_v: 3.74,
    status: 0
  }
];

function simulateDownloadLogic(expId, registry, readingsDb, activeDeviceMeta = null) {
  let experiment = registry.find(e => e.id === expId) || null;

  if (!experiment && activeDeviceMeta?.active_experiment?.id === expId) {
    experiment = activeDeviceMeta.active_experiment;
  }

  if (!experiment) {
    return { status: 404, error: `Experimento '${expId}' no encontrado en el registro.` };
  }

  const filename = experiment.csv_filename || `EXPERIMENTO_${expId}.CSV`;

  const filteredReadings = readingsDb.filter(r => {
    const t = new Date(r.recorded_at).getTime();
    const start = new Date(experiment.started_at).getTime();
    const end = experiment.ended_at ? new Date(experiment.ended_at).getTime() : Infinity;
    return t >= start && t <= end;
  });

  const csvLines = [
    'Fecha_Hora_GMT, Segundos_2000, OD_mg_L, Saturacion_pct, Temp_Agua_C, Bateria_V, Status'
  ];

  filteredReadings.forEach((r) => {
    const dt = r.recorded_at ? new Date(r.recorded_at).toISOString() : '';
    const s2000 = r.seconds_since_2000 ?? '';
    const od = r.dissolved_oxygen_mg_l !== undefined ? Number(r.dissolved_oxygen_mg_l).toFixed(3) : '';
    const sat = r.oxygen_saturation_pct !== undefined ? Number(r.oxygen_saturation_pct).toFixed(2) : '';
    const temp = r.water_temperature_c !== undefined ? Number(r.water_temperature_c).toFixed(2) : '';
    const bat = r.battery_v !== undefined ? Number(r.battery_v).toFixed(2) : '';
    const status = r.status ?? 0;

    csvLines.push(`${dt}, ${s2000}, ${od}, ${sat}, ${temp}, ${bat}, ${status}`);
  });

  return {
    status: 200,
    filename,
    csvContent: csvLines.join('\r\n'),
    linesCount: csvLines.length,
    dataRowCount: filteredReadings.length
  };
}

// Test 1: Búsqueda exacta de Prueba3 run 1
console.log('[Test 1] Verificando resolución de Experimento 3 (exp_1789203655075)...');
const res1 = simulateDownloadLogic('exp_1789203655075', mockRegistry, mockReadings);
assert.strictEqual(res1.status, 200);
assert.strictEqual(res1.filename, 'PRUEBA3.CSV');
assert.strictEqual(res1.dataRowCount, 2, 'Debe incluir las 2 lecturas dentro de la ventana de tiempo');
console.log(`  ✓ Resuelto como '${res1.filename}' con ${res1.dataRowCount} filas de datos.`);

// Test 2: Búsqueda exacta de Prueba3 run 2
console.log('[Test 2] Verificando resolución de Experimento 4 (exp_1789203933660)...');
const res2 = simulateDownloadLogic('exp_1789203933660', mockRegistry, mockReadings);
assert.strictEqual(res2.status, 200);
assert.strictEqual(res2.filename, 'PRUEBA_3.CSV');
assert.strictEqual(res2.dataRowCount, 1, 'Debe incluir la lectura de las 09:06:02');
console.log(`  ✓ Resuelto como '${res2.filename}' con ${res2.dataRowCount} filas de datos.`);

// Test 3: Fallback a active_experiment en metadata de dispositivo
console.log('[Test 3] Verificando fallback a metadata de devices para experimento en curso...');
const activeMeta = {
  active_experiment: {
    id: "exp_active_live_999",
    name: "PruebaEnVivo",
    csv_filename: "ENVIVO.CSV",
    started_at: "2026-09-12T09:00:00.000Z",
    ended_at: null
  }
};
const res3 = simulateDownloadLogic('exp_active_live_999', mockRegistry, mockReadings, activeMeta);
assert.strictEqual(res3.status, 200);
assert.strictEqual(res3.filename, 'ENVIVO.CSV');
assert.strictEqual(res3.dataRowCount, 3, 'Debe incluir todas las lecturas desde el inicio');
console.log(`  ✓ Resuelto vía metadata como '${res3.filename}' con ${res3.dataRowCount} filas de datos.`);

// Test 4: Id inexistente devuelve 404
console.log('[Test 4] Verificando respuesta 404 para ID inexistente...');
const res4 = simulateDownloadLogic('exp_inexistente_000', mockRegistry, mockReadings);
assert.strictEqual(res4.status, 404);
console.log('  ✓ Responde con status 404 y mensaje claro sin generar CSV ficticio vacío.');

console.log('\n====================================================');
console.log(' TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE (4/4)');
console.log('====================================================\n');
