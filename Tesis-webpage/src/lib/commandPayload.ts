/**
 * Normaliza el payload de una orden de `control_commands`.
 *
 * En la BD de producción **no existe la columna `payload`**: el JSON viaja serializado en
 * `error_message`. Sin esta reconstrucción cualquier lectura de `row.payload` es `undefined`,
 * lo que ya causó una mala clasificación de órdenes entre nodos (ADD §2.2).
 *
 * Vive aquí, y no dentro de una ruta, porque lo consumen tanto `/api/commands/pending` como el
 * orquestador: este último necesita saber **qué acción** lleva una orden pendiente para poder
 * invalidarla cuando el estado del banco cambia.
 */
export function resolveCommandPayload(row: any): Record<string, any> {
  let payload = row?.payload;
  if (!payload || typeof payload !== 'object' || Object.keys(payload).length === 0) {
    const raw = row?.error_message;
    if (typeof raw === 'string' && raw.trim().startsWith('{')) {
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = null;
      }
    }
  }
  return payload && typeof payload === 'object' ? payload : {};
}

/**
 * Acciones de orquestación: describen *qué receta* debe seguir el banco. Un cambio de estado las
 * deja obsoletas y la más reciente manda.
 *
 * Las acciones de **seguridad** (`emergency_stop`, `clear_estop`) y las **físicas ya ordenadas**
 * (`start_dose`) nunca se invalidan: silenciar una parada de emergencia sería el defecto contrario,
 * y una dosis encolada la pidió el operador de forma explícita.
 */
export const ORCHESTRATION_ACTIONS: ReadonlySet<string> = new Set([
  'set_state',
  'start_experiment',
  'stop_experiment',
  'start_monitor',
  'stop_monitor',
  'set_sampling_rate'
]);

/** ¿Una orden pendiente queda obsoleta al cambiar el estado del banco? */
export function isSupersededByStateChange(row: any): boolean {
  const action = resolveCommandPayload(row)?.action;
  return typeof action === 'string' && ORCHESTRATION_ACTIONS.has(action);
}
