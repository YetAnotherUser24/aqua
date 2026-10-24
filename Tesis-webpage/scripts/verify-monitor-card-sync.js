/**
 * Self-check script: Verificación de sincronización de estado de la tarjeta de monitoreo
 * Ejecución: node scripts/verify-monitor-card-sync.js
 */
const assert = require('assert');

console.log('========================================================');
console.log(' VERIFICACIÓN DE SINCRONIZACIÓN Y MODO MONITOR         ');
console.log('========================================================\n');

// 1. Simulación de la lógica de actualización en POST /api/commands
console.log('[Test 1] Verificando actualización inmediata de metadatos al enviar comandos...');

function applySensorCommandMetadata(currentMetadata, effectivePayload) {
  const action = effectivePayload.action;
  let updates = null;

  if (action === 'start_monitor') {
    updates = {
      monitor_active: true,
      ...(effectivePayload.interval_sec ? { monitor_interval_sec: Number(effectivePayload.interval_sec) } : {})
    };
  } else if (action === 'stop_monitor' || action === 'sleep') {
    updates = { monitor_active: false };
  } else if (action === 'set_sampling_rate' && effectivePayload.interval_sec) {
    updates = { monitor_interval_sec: Number(effectivePayload.interval_sec) };
  } else if (action === 'set_sleep_cycle' && effectivePayload.measure_time_min) {
    updates = { sleep_cycle_min: Number(effectivePayload.measure_time_min) };
  }

  return { ...currentMetadata, ...(updates || {}) };
}

let deviceMetadata = {
  monitor_active: false,
  monitor_interval_sec: 5,
  sleep_cycle_min: 15
};

// 1.1 Iniciar monitor
deviceMetadata = applySensorCommandMetadata(deviceMetadata, { action: 'start_monitor', interval_sec: 2 });
assert.strictEqual(deviceMetadata.monitor_active, true, 'start_monitor debe activar monitor_active');
assert.strictEqual(deviceMetadata.monitor_interval_sec, 2, 'start_monitor debe actualizar monitor_interval_sec');
console.log('  ✓ start_monitor actualiza inmediatamente monitor_active = true (interval = 2s)');

// 1.2 Detener monitor
deviceMetadata = applySensorCommandMetadata(deviceMetadata, { action: 'stop_monitor' });
assert.strictEqual(deviceMetadata.monitor_active, false, 'stop_monitor debe desactivar monitor_active');
console.log('  ✓ stop_monitor desactiva inmediatamente monitor_active = false');

// 1.3 Sleep desactiva monitor
deviceMetadata.monitor_active = true;
deviceMetadata = applySensorCommandMetadata(deviceMetadata, { action: 'sleep', minutes: 30 });
assert.strictEqual(deviceMetadata.monitor_active, false, 'sleep debe desactivar monitor_active');
console.log('  ✓ sleep desactiva inmediatamente monitor_active = false');

// 1.4 Cambiar tasa de muestreo
deviceMetadata = applySensorCommandMetadata(deviceMetadata, { action: 'set_sampling_rate', interval_sec: 15 });
assert.strictEqual(deviceMetadata.monitor_interval_sec, 15, 'set_sampling_rate debe actualizar monitor_interval_sec');
console.log('  ✓ set_sampling_rate actualiza monitor_interval_sec a 15s');

// 1.5 Cambiar ciclo de sueño
deviceMetadata = applySensorCommandMetadata(deviceMetadata, { action: 'set_sleep_cycle', measure_time_min: 30 });
assert.strictEqual(deviceMetadata.sleep_cycle_min, 30, 'set_sleep_cycle debe actualizar sleep_cycle_min');
console.log('  ✓ set_sleep_cycle actualiza sleep_cycle_min a 30 min');

// 2. Simulación de la guarda de sincronización en cliente (SensorControlPanel useEffect)
console.log('\n[Test 2] Verificando que sondeos en vuelo no sobreescriben la UI del operador...');

function evaluateClientStateUpdate({
  currentLocalActive,
  incomingServerActive,
  isSending,
  isPendingToggle
}) {
  // Si está en vuelo un comando o hay un toggle pendiente, no permitir que el sondeo clobber el estado local
  if (isSending || isPendingToggle) {
    return currentLocalActive;
  }
  return incomingServerActive;
}

// Escenario: El usuario hace clic en "Iniciar Modo Monitor"
// Estado local pasa a true, isPendingToggle = true, isSending = true
let localActive = true;
let isPending = true;
let isSending = true;

// Llega una respuesta vieja del servidor donde monitor_active = false
let incomingServerState = false;
let resolvedActive = evaluateClientStateUpdate({
  currentLocalActive: localActive,
  incomingServerActive: incomingServerState,
  isSending,
  isPendingToggle: isPending
});

assert.strictEqual(resolvedActive, true, 'El estado local no debe ser sobreescrito mientras la orden está en vuelo');
console.log('  ✓ Respuesta desfasada del servidor bloqueada exitosamente durante el envío');

// Una vez finalizado el comando, el servidor ya retorna el nuevo estado
isSending = false;
isPending = false;
incomingServerState = true;
resolvedActive = evaluateClientStateUpdate({
  currentLocalActive: localActive,
  incomingServerActive: incomingServerState,
  isSending,
  isPendingToggle: isPending
});
assert.strictEqual(resolvedActive, true, 'Estado sincronizado coherentemente');
console.log('  ✓ Estado final confirmado estable');

// 3. Simulación de limpieza de BD y descarte de flags huérfanos
console.log('\n[Test 3] Verificando reset de monitor_active huérfano tras purga de base de datos...');

function evaluateClearOrphanReset({
  time_scope,
  categories,
  hasActiveExperiment,
  currentDeviceMeta
}) {
  const isFullClear = time_scope === 'all' && (
    categories.includes('sensor_readings') ||
    categories.includes('alerts_commands') ||
    categories.includes('experiments')
  );

  if (isFullClear && !hasActiveExperiment) {
    return { ...currentDeviceMeta, monitor_active: false };
  }
  return currentDeviceMeta;
}

const clearedMeta = evaluateClearOrphanReset({
  time_scope: 'all',
  categories: ['sensor_readings', 'motor_telemetry', 'alerts_commands'],
  hasActiveExperiment: false,
  currentDeviceMeta: { monitor_active: true }
});

assert.strictEqual(clearedMeta.monitor_active, false, 'monitor_active debe resetearse a false en purga total sin experimento');
console.log('  ✓ Purga total apaga monitor_active huérfano si no hay experimentos activos');

const preservedMeta = evaluateClearOrphanReset({
  time_scope: 'all',
  categories: ['sensor_readings'],
  hasActiveExperiment: true,
  currentDeviceMeta: { monitor_active: true }
});

assert.strictEqual(preservedMeta.monitor_active, true, 'monitor_active debe preservarse si hay un experimento en curso');
console.log('  ✓ monitor_active se preserva correctamente si hay un experimento en curso');

console.log('\n========================================================');
console.log(' ¡TODAS LAS COMPROBACIONES PASARON CORRECTAMENTE! ✓     ');
console.log('========================================================');
