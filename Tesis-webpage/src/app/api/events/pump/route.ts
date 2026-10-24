import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

/**
 * Registro de eventos de la bomba dosificadora (AquaControl V4).
 * Reutiliza la tabla `mixer_events` con event_type='dose_pump' (ya permitido por el CHECK constraint),
 * evitando una migración DDL y manteniendo una sola bitácora de actuadores del banco.
 * Body: { experiment_id, event_type: 'dose_started'|'dose_completed'|'fault', volume_ml?, target_ml?, status?, rtc_timestamp_ms? }
 */
export async function POST(req: NextRequest) {
  try {
    const body = await req.json().catch(() => ({}));
    const { experiment_id, event_type, volume_ml, target_ml, status, rtc_timestamp_ms } = body;

    if (!event_type) {
      return NextResponse.json({ error: 'Missing event_type' }, { status: 400 });
    }

    const statusText = [
      event_type,
      volume_ml !== undefined ? `ml=${Number(volume_ml)}` : null,
      target_ml !== undefined ? `target=${Number(target_ml)}` : null,
      status ? String(status) : null
    ]
      .filter(Boolean)
      .join(' ');

    if (isSupabaseConfigured()) {
      const { error } = await supabaseAdmin.from('mixer_events').insert({
        experiment_id: experiment_id || 'idle',
        event_type: 'dose_pump',
        status: statusText,
        rtc_timestamp_ms: rtc_timestamp_ms || Date.now(),
        created_at: new Date().toISOString()
      });
      if (error) {
        return NextResponse.json({ error: 'Database error', details: error.message }, { status: 500 });
      }
    }

    return NextResponse.json({ success: true, message: 'Pump event registered' });
  } catch (err: any) {
    return NextResponse.json({ error: 'Invalid payload', details: err?.message }, { status: 400 });
  }
}
