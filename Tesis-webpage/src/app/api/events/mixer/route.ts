import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';

export const dynamic = 'force-dynamic';

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const { experiment_id, event_type, status, rtc_timestamp_ms } = body;

    if (!experiment_id || !event_type) {
      return NextResponse.json({ error: 'Missing experiment_id or event_type' }, { status: 400 });
    }

    if (isSupabaseConfigured()) {
      const { error } = await supabaseAdmin.from('mixer_events').insert({
        experiment_id,
        event_type,
        status: status || null,
        rtc_timestamp_ms: rtc_timestamp_ms || Date.now(),
        created_at: new Date().toISOString()
      });

      if (error) {
        return NextResponse.json({ error: 'Database error', details: error }, { status: 500 });
      }
    }

    return NextResponse.json({ success: true, message: 'Mixer event registered' });
  } catch (err: any) {
    return NextResponse.json({ error: 'Invalid payload', details: err.message }, { status: 400 });
  }
}
