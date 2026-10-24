import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { demoAlerts } from '@/lib/demoData';

export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  try {
    if (!isSupabaseConfigured()) {
      return NextResponse.json({ success: true, alerts: demoAlerts });
    }

    const { data: alerts, error } = await supabaseAdmin
      .from('alerts')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(50);

    if (error) {
      console.error('Error cargando alertas:', error);
      return NextResponse.json({ success: true, alerts: demoAlerts });
    }

    return NextResponse.json({
      success: true,
      alerts: alerts && alerts.length > 0 ? alerts : demoAlerts
    });
  } catch (error: any) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const { alert_id, action = 'reconocida', user = 'Operador Web' } = body;

    if (!alert_id) {
      return NextResponse.json({ error: 'alert_id es requerido' }, { status: 400 });
    }

    if (!isSupabaseConfigured()) {
      return NextResponse.json({
        success: true,
        message: `Alerta #${alert_id} marcada como ${action} (Modo Demo)`
      });
    }

    const { data, error } = await supabaseAdmin
      .from('alerts')
      .update({
        status: action,
        resolved_at: action === 'resuelta' ? new Date().toISOString() : null,
        resolved_by: user
      })
      .eq('id', alert_id)
      .select()
      .single();

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    return NextResponse.json({ success: true, alert: data });
  } catch (error: any) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}
