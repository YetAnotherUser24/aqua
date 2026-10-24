import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

/**
 * Bitácora de seguridad de anulaciones manuales (AquaControl V4).
 * Reutiliza `mixer_events` con event_type='manual_confirmation' para no requerir DDL nuevo.
 * Body: { action, target?, device_id?, payload?, experiment_id?, requested_by?, rtc_timestamp_ms? }
 */
export async function POST(req: NextRequest) {
  try {
    const body = await req.json().catch(() => ({}));
    const { action, target, payload, experiment_id, requested_by, rtc_timestamp_ms } = body;

    if (!action) {
      return NextResponse.json({ error: 'Missing action' }, { status: 400 });
    }

    const statusText = [
      `override:${action}`,
      target ? `target=${target}` : null,
      requested_by ? `by=${requested_by}` : null,
      payload ? `payload=${JSON.stringify(payload)}` : null
    ]
      .filter(Boolean)
      .join(' ');

    if (isSupabaseConfigured()) {
      const { error } = await supabaseAdmin.from('mixer_events').insert({
        experiment_id: experiment_id || 'idle',
        event_type: 'manual_confirmation',
        status: statusText,
        rtc_timestamp_ms: rtc_timestamp_ms || Date.now(),
        created_at: new Date().toISOString()
      });
      if (error) {
        return NextResponse.json({ error: 'Database error', details: error.message }, { status: 500 });
      }
    }

    return NextResponse.json({ success: true, message: 'Override logged' });
  } catch (err: any) {
    return NextResponse.json({ error: 'Invalid payload', details: err?.message }, { status: 400 });
  }
}
