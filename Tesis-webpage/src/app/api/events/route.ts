import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { demoEvents } from '@/lib/demoData';

export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  try {
    if (!isSupabaseConfigured()) {
      return NextResponse.json({ success: true, events: demoEvents, isDemo: true });
    }

    const { data: events, error } = await supabaseAdmin
      .from('motor_events')
      .select('*')
      .order('started_at', { ascending: false })
      .limit(50);

    if (error) {
      console.error('Error cargando eventos:', error);
      return NextResponse.json({ success: true, events: demoEvents, isDemo: true });
    }

    return NextResponse.json({
      success: true,
      events: events && events.length > 0 ? events : demoEvents,
      isDemo: !events || events.length === 0
    });
  } catch (error: any) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}
