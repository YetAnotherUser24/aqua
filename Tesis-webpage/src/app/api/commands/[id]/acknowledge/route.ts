import { NextRequest, NextResponse } from 'next/server';
import { authenticateDevice } from '@/lib/deviceAuth';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { isActuatorRole } from '@/lib/deviceRoles';

export const dynamic = 'force-dynamic';

/**
 * Permite que el ESP32 confirme la ejecución de una orden recibida
 * Parámetro dinámico: [id] (UUID del comando)
 * Payload opcional: { success: boolean, actual_speed_percent: number, error_message?: string }
 */
export async function POST(
  req: NextRequest,
  { params }: { params: { id: string } }
) {
  const { id } = params;

  // 1. Autenticar ESP32 (cualquier dispositivo registrado)
  const { device, role, errorResponse } = await authenticateDevice(req);
  if (errorResponse) return errorResponse;

  try {
    const body = await req.json().catch(() => ({}));
    const { success = true, actual_speed_percent, error_message, sensor_state, rtc_timestamp_ms } = body;

    if (!isSupabaseConfigured() || !device) {
      return NextResponse.json({
        success: true,
        message: 'Comando reconocido correctamente (Modo Local)'
      });
    }

    // V4: el ACK puede arrastrar el instante exacto de ejecución física (epoch UTC ms) para
    // anular la latencia de red al graficar la perturbación. Se persiste dentro de payload JSONB
    // (sin requerir migración DDL) como `executed_rtc_ms`.
    const deviceRtcMs = Number.isFinite(Number(rtc_timestamp_ms)) && Number(rtc_timestamp_ms) > 0
      ? Number(rtc_timestamp_ms)
      : null;

    // ADR-3: se toleran hasta ±5 s de desfase entre el reloj del nodo y el servidor. Fuera de esa
    // ventana el instante reportado no es fiable (p. ej. NTP aún no sincronizado en la ESP32); se
    // ancla al reloj del servidor y se conserva el valor crudo del dispositivo para trazabilidad.
    const CLOCK_SKEW_TOLERANCE_MS = 5000;
    const clockSkewMs = deviceRtcMs !== null ? Date.now() - deviceRtcMs : null;
    const deviceClockReliable = clockSkewMs !== null && Math.abs(clockSkewMs) <= CLOCK_SKEW_TOLERANCE_MS;
    const executedRtcMs = deviceRtcMs === null ? null : (deviceClockReliable ? deviceRtcMs : Date.now());
    const latencyMs = executedRtcMs !== null ? Math.max(0, Date.now() - executedRtcMs) : null;

    // Lectura defensiva: la columna `payload` puede no existir en instalaciones sin la migración V4
    // (el ecosistema ya usa `error_message` como almacén JSON del comando).
    let existing: any = null;
    let hasPayloadColumn = false;
    {
      const withPayload = await supabaseAdmin
        .from('control_commands')
        .select('payload, error_message')
        .eq('id', id)
        .eq('device_id', device.id)
        .maybeSingle();

      if (!withPayload.error) {
        existing = withPayload.data;
        hasPayloadColumn = true;
      } else {
        const withoutPayload = await supabaseAdmin
          .from('control_commands')
          .select('error_message')
          .eq('id', id)
          .eq('device_id', device.id)
          .maybeSingle();
        existing = withoutPayload.data;
      }
    }

    let mergedPayload: Record<string, any> | null = null;
    if (existing) {
      let base = existing.payload;
      if ((!base || typeof base !== 'object' || Object.keys(base).length === 0) &&
          typeof existing.error_message === 'string' && existing.error_message.trim().startsWith('{')) {
        try { base = JSON.parse(existing.error_message); } catch { /* ignore */ }
      }
      if (base && typeof base === 'object') {
        mergedPayload = {
          ...base,
          ...(executedRtcMs !== null ? { executed_rtc_ms: executedRtcMs } : {}),
          ...(deviceRtcMs !== null && !deviceClockReliable ? { device_rtc_ms: deviceRtcMs } : {}),
          ...(clockSkewMs !== null ? { clock_skew_ms: clockSkewMs } : {}),
          ...(latencyMs !== null ? { ack_latency_ms: latencyMs } : {})
        };
      }
    }

    const updateFields: Record<string, any> = {
      status: success ? 'acknowledged' : 'failed',
      executed_at: new Date().toISOString(),
      error_message: error_message || null
    };
    if (mergedPayload) {
      if (hasPayloadColumn) {
        updateFields.payload = mergedPayload;
      } else if (!error_message) {
        // Sin columna `payload`: persistir el instante exacto en el almacén JSON del comando.
        updateFields.error_message = JSON.stringify(mergedPayload);
      }
    }

    let { data: command, error } = await supabaseAdmin
      .from('control_commands')
      .update(updateFields)
      .eq('id', id)
      .eq('device_id', device.id)
      .select()
      .single();

    // Robusto ante instalaciones sin columna `payload`: reintenta sin ella.
    if (error && updateFields.payload) {
      delete updateFields.payload;
      const retry = await supabaseAdmin
        .from('control_commands')
        .update(updateFields)
        .eq('id', id)
        .eq('device_id', device.id)
        .select()
        .single();
      command = retry.data;
      error = retry.error;
    }

    if (error || !command) {
      return NextResponse.json({ error: 'Comando no encontrado o error en actualización' }, { status: 404 });
    }

    // Actualizar metadatos de sensor si se enviaron cambios de estado (e.g. monitor_active)
    if (role === 'sensor' && sensor_state) {
      const currentMeta = device.metadata || {};
      await supabaseAdmin
        .from('devices')
        .update({
          metadata: { ...currentMeta, ...sensor_state },
          updated_at: new Date().toISOString()
        })
        .eq('id', device.id);
    }

    // Telemetría de motor: se confirma la velocidad recibida en el ACK.
    // El rol identifica al actuador sin ambigüedad (antes el tipo legacy `motor_thruster`
    // agrupaba al aireador, al mixer y a la bomba).
    if (actual_speed_percent !== undefined && isActuatorRole(role)) {
      const speed = Number(actual_speed_percent);
      const pwm_us = Math.round(1500 + (speed / 100) * 400);
      await supabaseAdmin.from('motor_telemetry').insert({
        device_id: device.id,
        recorded_at: new Date().toISOString(),
        is_on: speed > 0,
        speed_percent: speed,
        pwm_us
      });
    }

    return NextResponse.json({
      success: true,
      message: 'Confirmación de comando procesada exitosamente',
      command_id: id,
      ...(latencyMs !== null ? { latency_ms: latencyMs } : {}),
      ...(clockSkewMs !== null ? { clock_skew_ms: clockSkewMs, clock_reliable: deviceClockReliable } : {})
    });

  } catch (error: any) {
    console.error('Error en /api/commands/[id]/acknowledge:', error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}
