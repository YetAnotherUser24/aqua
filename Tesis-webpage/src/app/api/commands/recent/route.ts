import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

export interface PerturbationMarker {
  id: string;
  action: string;
  device_id: string;
  /** Epoch UTC ms del instante exacto de ejecución reportado por el dispositivo. */
  executed_rtc_ms: number | null;
  created_at: string;
}

/**
 * GET /api/commands/recent
 * Devuelve las perturbaciones físicas recientes (comandos reconocidos) con su instante exacto de
 * ejecución. El dashboard usa `executed_rtc_ms` para graficar la perturbación sin la latencia de red.
 * Query: ?hours=24&limit=20
 */
export async function GET(req: NextRequest) {
  const hours = Math.max(1, Math.min(168, Number(req.nextUrl.searchParams.get('hours') || 24)));
  const limit = Math.max(1, Math.min(100, Number(req.nextUrl.searchParams.get('limit') || 20)));

  if (!isSupabaseConfigured()) {
    return NextResponse.json({ success: true, perturbations: [], isDemo: true });
  }

  try {
    const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();

    // Lectura defensiva: `payload` puede no existir (instalaciones sin la migración V4);
    // en ese caso el JSON del comando vive en `error_message` (convención del ecosistema).
    const base = () =>
      supabaseAdmin
        .from('control_commands')
        .select('id, device_id, error_message, created_at, status')
        .in('status', ['acknowledged', 'sent'])
        .gte('created_at', since)
        .order('created_at', { ascending: false })
        .limit(limit);

    let data: any[] | null = null;
    let error: any = null;

    const withPayload = await supabaseAdmin
      .from('control_commands')
      .select('id, device_id, payload, error_message, created_at, status')
      .in('status', ['acknowledged', 'sent'])
      .gte('created_at', since)
      .order('created_at', { ascending: false })
      .limit(limit);

    if (withPayload.error) {
      const retry = await base();
      data = retry.data;
      error = retry.error;
    } else {
      data = withPayload.data;
    }

    if (error) {
      console.error('Error consultando perturbaciones:', error);
      return NextResponse.json({ success: true, perturbations: [], error: error.message });
    }

    const perturbations: PerturbationMarker[] = (data || [])
      .map((row: any) => {
        let payload = row.payload;
        if ((!payload || typeof payload !== 'object' || Object.keys(payload).length === 0) &&
            typeof row.error_message === 'string' && row.error_message.trim().startsWith('{')) {
          try { payload = JSON.parse(row.error_message); } catch { /* ignore */ }
        }
        const action = payload?.action || 'command';
        const executedRtc = Number(payload?.executed_rtc_ms);
        return {
          id: row.id,
          action,
          device_id: row.device_id,
          executed_rtc_ms: Number.isFinite(executedRtc) && executedRtc > 0 ? executedRtc : null,
          created_at: row.created_at
        };
      })
      .filter((p: PerturbationMarker) => p.executed_rtc_ms !== null);

    return NextResponse.json(
      { success: true, perturbations },
      { headers: { 'Cache-Control': 'no-store' } }
    );
  } catch (err: any) {
    return NextResponse.json({ success: true, perturbations: [], error: err?.message });
  }
}
