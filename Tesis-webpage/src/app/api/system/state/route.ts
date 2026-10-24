import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { getSystemState, setSystemState, broadcastState, normalizeSystemState, snapshotPendingOrchestrationIds } from '@/lib/systemState';
import { OrchestratorState } from '@/types';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

const VALID_STATES: OrchestratorState[] = ['IDLE', 'ACTIVE_EXPERIMENT', 'MANUAL_OVERRIDE'];

/** GET: estado global actual del orquestador. */
export async function GET() {
  const state = await getSystemState();
  return NextResponse.json(
    { success: true, state },
    { headers: { 'Cache-Control': 'no-store' } }
  );
}

/**
 * POST: transiciona la máquina de estados global y difunde `set_state` a todos los nodos.
 * Body: { state, experiment_id?, override?, requested_by? }
 */
export async function POST(req: NextRequest) {
  try {
    // Órdenes que ya estaban encoladas al empezar la transición: el estado nuevo las deja
    // obsoletas. Se capturan por IDENTIFICADOR y no por marca temporal, porque el corte lo
    // generaría el reloj de Vercel y `created_at` lo pone Postgres (desfase medido ~160-660 ms).
    // Sin esto, una orden de receta encolada un instante antes se servía DESPUÉS del override y
    // volvía a armar el actuador: el aireador no bajaba a 0 RPM al anular manualmente.
    const staleCommandIds = await snapshotPendingOrchestrationIds();
    const body = await req.json().catch(() => ({}));
    const requestedState = body?.state as OrchestratorState;
    if (!VALID_STATES.includes(requestedState)) {
      return NextResponse.json(
        { error: `state inválido. Valores permitidos: ${VALID_STATES.join(', ')}` },
        { status: 400 }
      );
    }

    const requested_by = typeof body?.requested_by === 'string' && body.requested_by
      ? body.requested_by
      : 'Operador Web';

    const overridePatch = body?.override && typeof body.override === 'object' ? body.override : {};
    if (requestedState === 'MANUAL_OVERRIDE') {
      overridePatch.master = true;
    } else if (overridePatch.master === undefined) {
      overridePatch.master = false;
    }

    const next = await setSystemState({
      state: requestedState,
      experiment_id: body?.experiment_id ?? undefined,
      override: overridePatch,
      updated_by: requested_by
    });

    // Difundir el nuevo estado a los 4 nodos (sensor, ODrive, mixer, bomba).
    const queued = await broadcastState(next, requested_by, staleCommandIds);

    // Bitácora de seguridad: registrar el cambio de estado como evento del banco.
    if (isSupabaseConfigured()) {
      await supabaseAdmin.from('mixer_events').insert({
        experiment_id: next.experiment_id || 'idle',
        event_type: 'manual_confirmation',
        status: `state:${next.state}`,
        rtc_timestamp_ms: Date.now(),
        created_at: new Date().toISOString()
      });
    }

    return NextResponse.json(
      { success: true, state: normalizeSystemState(next), commands_queued: queued },
      { headers: { 'Cache-Control': 'no-store' } }
    );
  } catch (err: any) {
    return NextResponse.json({ error: 'Invalid payload', details: err?.message }, { status: 400 });
  }
}
