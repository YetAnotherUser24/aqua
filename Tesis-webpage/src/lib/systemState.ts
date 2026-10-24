import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { OrchestratorState, SystemState, OverrideFlags, CommandStatus, DeviceRole } from '@/types';
import { resolveActuatorRecipe, buildActuatorIntent } from '@/lib/experimentRecipe';
import { isSupersededByStateChange } from '@/lib/commandPayload';import {
  DEVICE_ID_BY_ROLE,
  ALL_DEVICE_IDS as ALL_CANONICAL_DEVICE_IDS,
  ROLE_BY_DEVICE_ID
} from '@/lib/deviceRoles';

export type { DeviceRole };

/** Clave canónica en system_settings para el estado global del orquestador (AquaControl V4). */
export const ORCHESTRATOR_KEY = 'system_state';

/** Registro canónico de nodos del banco de pruebas (rol -> device_id). Ver `deviceRoles.ts`. */
export const DEVICE_IDS = DEVICE_ID_BY_ROLE;

export const ALL_DEVICE_IDS = ALL_CANONICAL_DEVICE_IDS;

const IDLE_OVERRIDE: OverrideFlags = { master: false, pump: false, mixer: false, odrive: false };

export const DEFAULT_SYSTEM_STATE: SystemState = {
  state: 'IDLE',
  since: new Date(0).toISOString(),
  experiment_id: null,
  override: IDLE_OVERRIDE,
  updated_by: 'system'
};

const VALID_STATES: OrchestratorState[] = ['IDLE', 'ACTIVE_EXPERIMENT', 'MANUAL_OVERRIDE'];

export function normalizeSystemState(raw: any): SystemState {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_SYSTEM_STATE };
  const state: OrchestratorState = VALID_STATES.includes(raw.state) ? raw.state : 'IDLE';
  return {
    state,
    since: typeof raw.since === 'string' && raw.since ? raw.since : new Date().toISOString(),
    experiment_id: raw.experiment_id ?? null,
    override: { ...IDLE_OVERRIDE, ...(raw.override || {}) },
    updated_by: typeof raw.updated_by === 'string' ? raw.updated_by : 'system'
  };
}

/** Lee el estado global. Devuelve IDLE por defecto si la BD no está configurada o la clave no existe. */
export async function getSystemState(): Promise<SystemState> {
  if (!isSupabaseConfigured()) return { ...DEFAULT_SYSTEM_STATE };
  try {
    const { data, error } = await supabaseAdmin
      .from('system_settings')
      .select('value')
      .eq('key', ORCHESTRATOR_KEY)
      .maybeSingle();
    if (error || !data) return { ...DEFAULT_SYSTEM_STATE };
    return normalizeSystemState(data.value);
  } catch {
    return { ...DEFAULT_SYSTEM_STATE };
  }
}

/** Escribe (upsert) el estado global. `since` se refresca cuando cambia el estado o el experimento. */
export async function setSystemState(patch: Partial<SystemState> & { updated_by?: string }): Promise<SystemState> {
  const current = await getSystemState();
  const next: SystemState = {
    ...current,
    ...patch,
    override: { ...current.override, ...(patch.override || {}) }
  };
  const stateChanged = next.state !== current.state || next.experiment_id !== current.experiment_id;
  if (stateChanged) next.since = new Date().toISOString();
  next.updated_by = patch.updated_by || current.updated_by;

  if (isSupabaseConfigured()) {
    await supabaseAdmin.from('system_settings').upsert(
      {
        key: ORCHESTRATOR_KEY,
        value: next,
        description: 'Estado global del orquestador AquaControl (IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE)',
        updated_at: new Date().toISOString()
      },
      { onConflict: 'key' }
    );
  }
  return next;
}

export interface EnqueueCommandInput {
  device_id: string;
  /**
   * Solo los tipos permitidos por el CHECK de `control_commands` en producción
   * (`start`, `stop`, `set_speed`, `emergency_stop`, `reboot`). La intención semántica
   * viaja en `payload.action`, que el firmware interpreta con prioridad.
   */
  command_type: 'start' | 'stop' | 'set_speed' | 'emergency_stop' | 'reboot';
  payload?: Record<string, any>;
  speed_percent?: number;
  requested_by?: string;
}

/**
 * Inserta una orden en control_commands replicando la estrategia de robustez del endpoint /api/commands:
 * si la columna `payload` no está disponible, serializa el JSON en `error_message`.
 */
export async function enqueueCommand(input: EnqueueCommandInput): Promise<{ id: string; status: CommandStatus } | null> {
  if (!isSupabaseConfigured()) return null;

  const payload = { ...(input.payload || {}) };
  if (!payload.action) payload.action = input.command_type;
  const actionTag = String(payload.action);
  const requested_by = input.requested_by && input.requested_by.includes('(')
    ? input.requested_by
    : `${input.requested_by || 'Orquestador'} (${actionTag})`;

  const speed = Math.max(0, Math.min(100, Number(input.speed_percent ?? 0)));
  const base: Record<string, any> = {
    device_id: input.device_id,
    command_type: input.command_type,
    speed_percent: speed,
    pwm_us: Math.round(1500 + (speed / 100) * 400),
    status: 'pending',
    requested_by
  };

  let res = await supabaseAdmin
    .from('control_commands')
    .insert({ ...base, payload })
    .select('id, status')
    .single();

  if (res.error) {
    // Fallback histórico: la columna payload puede no existir en algunas instalaciones.
    res = await supabaseAdmin
      .from('control_commands')
      .insert({ ...base, error_message: JSON.stringify(payload) })
      .select('id, status')
      .single();
  }

  if (res.error || !res.data) {
    console.error('enqueueCommand error:', res.error);
    return null;
  }
  return { id: res.data.id, status: res.data.status };
}

/** Lee el experimento del registro canónico (`system_settings.experiments_registry`). */
export async function getRegisteredExperiment(experimentId: string | null): Promise<any | null> {
  if (!experimentId || !isSupabaseConfigured()) return null;
  try {
    const { data } = await supabaseAdmin
      .from('system_settings')
      .select('value')
      .eq('key', 'experiments_registry')
      .maybeSingle();
    const list = Array.isArray(data?.value) ? data.value : [];
    return list.find((e: any) => e?.id === experimentId) ?? null;
  } catch {
    return null;
  }
}

/**
 * Identificadores de las órdenes de orquestación que están pendientes AHORA.
 *
 * Se toma al **entrar** al handler de la transición. Comparar marcas temporales no sirve: el corte
 * lo genera el reloj de Vercel y `created_at` lo pone Postgres, y el desfase medido entre ambos
 * (~160–660 ms, con Vercel por detrás) es del mismo orden que la ventana entre encolar y difundir.
 * Con identificadores explícitos la invalidación no depende de ningún reloj.
 */
export async function snapshotPendingOrchestrationIds(): Promise<string[]> {
  if (!isSupabaseConfigured()) return [];
  try {
    const { data, error } = await supabaseAdmin
      .from('control_commands')
      .select('id, error_message, status')
      .eq('status', 'pending')
      .limit(500);
    if (error || !Array.isArray(data)) return [];
    return data.filter(isSupersededByStateChange).map((r: any) => r.id);
  } catch (err: any) {
    console.error('[systemState] Error al leer la cola de órdenes:', err?.message);
    return [];
  }
}

/**
 * Marca esas órdenes exactas como `expired` para que `/api/commands/pending` no las sirva.
 *
 * **Por qué existe:** un `set_state` encolado por una receta y aún sin entregar se servía al nodo
 * DESPUÉS del `set_state` de MANUAL_OVERRIDE. Como los nodos aplican lo último que reciben, una
 * anulación manual quedaba silenciosamente anulada y el PID volvía a armarse solo. Se detectó en
 * producción con el banco virtual (`manualOverride`): el aireador no bajaba a 0 RPM.
 *
 * El filtro es por `action` del payload y no por `command_type`, porque `command_type` no
 * distingue: `clear_estop` viaja como `set_speed`, igual que un `set_state`. Las acciones de
 * seguridad y las dosis ya ordenadas nunca llegan a esta lista (ver `commandPayload.ts`).
 */
export async function expireCommands(ids: string[]): Promise<number> {
  if (!isSupabaseConfigured() || ids.length === 0) return 0;
  // En lotes: el filtro `in` viaja en la URL y cientos de UUID la desbordarían.
  let invalidated = 0;
  for (let i = 0; i < ids.length; i += 100) {
    const lote = ids.slice(i, i + 100);
    const { error } = await supabaseAdmin
      .from('control_commands')
      // `expired` es un estado terminal del CHECK de producción: la orden ya no se sirve.
      .update({ status: 'expired' })
      .in('id', lote);
    if (error) {
      console.error('[systemState] No se pudieron invalidar órdenes obsoletas:', error.message);
      return invalidated;
    }
    invalidated += lote.length;
  }
  return invalidated;
}

/**
 * Difunde un `set_state` a todos los nodos del banco. Devuelve cuántas órdenes se encolaron.
 *
 * El payload incluye la intención EXPLÍCITA de cada actuador, resuelta desde el registro de
 * experimentos. Sin ella cada nodo adivinaba: el T-200 encendía el mixer en todo
 * `ACTIVE_EXPERIMENT` y el ODrive no se armaba nunca. En IDLE / MANUAL_OVERRIDE la receta se
 * fuerza a OFF porque una anulación manual debe abortar cualquier automatismo (ADD §2.3).
 *
 * @param staleCommandIds Órdenes que ya estaban pendientes al empezar la transición: el estado
 *   nuevo las deja obsoletas. Por construcción no incluyen las que esta llamada va a encolar, así
 *   que la transición nunca cancela sus propias órdenes.
 */
export async function broadcastState(
  state: SystemState,
  requested_by: string,
  staleCommandIds: string[] = []
): Promise<number> {
  // El estado nuevo es autoritativo. Se invalida ANTES de encolar para que un nodo que sondee
  // justo ahora no reciba una orden del estado anterior.
  await expireCommands(staleCommandIds);

  const experiment = await getRegisteredExperiment(state.experiment_id);
  const intent = buildActuatorIntent(state.state, resolveActuatorRecipe(experiment));

  let queued = 0;
  for (const device_id of ALL_DEVICE_IDS) {
    const role = ROLE_BY_DEVICE_ID[device_id];
    const isSensor = role === 'sensor';

    // La intención se envía solo al nodo que la ejecuta, para no dejar ambigüedad entre nodos.
    const roleIntent: Record<string, string | number> = {};
    if (role === 'mixer') {
      roleIntent.mixer = intent.mixer as string;
    }
    if (role === 'odrive') {
      roleIntent.motor_mode = intent.motor_mode as string;
      if (intent.motor_target_do !== undefined) roleIntent.motor_target_do = intent.motor_target_do;
      if (intent.motor_throttle_pct !== undefined) roleIntent.motor_throttle_pct = intent.motor_throttle_pct;
    }

    const cmd = await enqueueCommand({
      device_id,
      // `set_speed` es el tipo permitido por el CHECK de producción para actuadores;
      // el significado real viaja en payload.action = 'set_state'.
      command_type: isSensor ? (state.state === 'IDLE' ? 'stop' : 'start') : 'set_speed',
      payload: {
        action: 'set_state',
        state: state.state,
        experiment_id: state.experiment_id,
        // Rol destinatario: permite al firmware (y al fallback de /api/commands/pending) verificar
        // que la orden es suya, sin depender de `devices.type`.
        target_role: role,
        override: state.override,
        interval_sec: 5,
        ...roleIntent
      },
      requested_by
    });
    if (cmd) queued++;
  }
  return queued;
}
