/**
 * Archivado de telemetría de alta fidelidad por ROL de actuador.
 *
 * Cada nodo escribe su serie completa en su propia tabla (`odrive_telemetry_bulk`, `mixer_telemetry`,
 * `pump_telemetry`, `sensor_telemetry_bulk`). Antes todos los actuadores compartían un único INSERT
 * a `odrive_telemetry_bulk` y una única tabla en vivo, así que las series se mezclaban y no se podía
 * identificar la dinámica del aireador por separado.
 *
 * Si la tabla aún no existe (la migración `20261008_separate_actuator_roles.sql` es aditiva y se
 * aplica aparte), el archivado **no se pierde en silencio**: se registra un aviso explícito una sola
 * vez por tabla, con el nombre de la migración que falta. Ese silencio es justo lo que ocultó que la
 * fidelidad de 5 Hz nunca se estaba guardando.
 */
import { supabaseAdmin } from '@/lib/supabase';
import { DeviceRole, ROLE_ARCHIVE_TABLE, isMissingTableError } from '@/lib/deviceRoles';

export type ArchiveOutcome = 'stored' | 'missing_table' | 'error' | 'skipped';

/** Evita repetir el aviso en cada lote (el bulk llega cada 5 s). */
const warnedTables = new Set<string>();

/**
 * Resuelve el id del experimento al que pertenece una lectura.
 *
 * El firmware no conoce el id que acuñó el backend, así que envía `"backend_resolved"`. Guardarlo
 * literal deja la fila huérfana: la descarga CSV por experimento busca por `experiment_id` y no la
 * encontraría. Vive aquí para que todas las rutas de telemetría resuelvan igual.
 */
export async function resolveActiveExperimentId(raw: unknown): Promise<string> {
  const requested = typeof raw === 'string' && raw ? raw : 'idle';
  if (requested !== 'backend_resolved') return requested;
  try {
    const { data } = await supabaseAdmin
      .from('system_settings')
      .select('value')
      .eq('key', 'experiments_registry')
      .maybeSingle();
    const list = Array.isArray(data?.value) ? data.value : [];
    const active = list.find((e: any) => e?.status === 'active');
    return active?.id || 'idle';
  } catch {
    return 'idle';
  }
}

/**
 * Guarda un lote completo (payload JSON) en la tabla de archivo del rol.
 * @returns `stored` si se persistió, `missing_table` si falta la tabla de archivo, `skipped`/`error` en el resto.
 */
export async function archiveRolePayload(
  role: DeviceRole | null,
  experimentId: string,
  payload: any
): Promise<ArchiveOutcome> {
  if (!role) return 'skipped';

  const table = ROLE_ARCHIVE_TABLE[role];
  const { error } = await supabaseAdmin
    .from(table)
    .insert({
      experiment_id: experimentId || 'idle',
      payload_json: payload,
      created_at: new Date().toISOString()
    });

  if (!error) return 'stored';

  if (isMissingTableError(error)) {
    if (!warnedTables.has(table)) {
      warnedTables.add(table);
      console.warn(
        `[telemetry-archive] La tabla de archivo '${table}' (rol '${role}') no existe: ` +
        `la telemetría de alta fidelidad NO se está archivando. ` +
        `Aplica supabase/migrations/20261008_separate_actuator_roles.sql`
      );
    }
    return 'missing_table';
  }

  console.error(`[telemetry-archive] Error insertando en '${table}':`, error);
  return 'error';
}
