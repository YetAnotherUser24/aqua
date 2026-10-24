import { NextRequest, NextResponse } from 'next/server';
import { authenticateDevice } from '@/lib/deviceAuth';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { archiveRolePayload, ArchiveOutcome, resolveActiveExperimentId } from '@/lib/telemetryArchive';
import { isActuatorRole } from '@/lib/deviceRoles';

export const dynamic = 'force-dynamic';

/**
 * Recibe telemetría desde el ESP32 del motor / aireador Thruster Blue Robotics T200
 * Formato esperado en JSON:
 * {
 *   "datetime": "2026-08-31T15:00:00Z",
 *   "is_on": true,
 *   "speed_percent": 65.0,
 *   "pwm_us": 1760,
 *   "voltage_v": 14.8,
 *   "current_a": 8.5,
 *   "power_w": 125.8,
 *   "status_code": 0
 * }
 */
export async function POST(req: NextRequest) {
  // Autorización por ROL: el ODrive, el mixer y la bomba comparten el tipo legacy `motor_thruster`,
  // así que el tipo no basta para decidir a qué tabla archivar. Antes esta ruta se autenticaba
  // contra `motor_thruster` y NUNCA archivaba: el T-200 publica aquí (objeto único, no lote) y su
  // tabla `mixer_telemetry` quedaba permanentemente vacía.
  const { device, role, errorResponse } = await authenticateDevice(req, undefined, undefined);
  if (errorResponse) return errorResponse;
  if (!device) {
    return NextResponse.json({ error: 'Dispositivo no autorizado' }, { status: 401 });
  }
  if (!isActuatorRole(role)) {
    return NextResponse.json({ error: 'Endpoint reservado a actuadores (odrive/mixer/pump)' }, { status: 403 });
  }

  try {
    const body = await req.json();

    const rawList = Array.isArray(body.samples) && body.samples.length > 0 
      ? body.samples 
      : [body];

    const rowsToInsert = rawList.map((item: any) => {      const is_on = Boolean(item.is_on ?? item.is_running);
      const speed_percent = Number(item.speed_percent ?? 0);
      const pwm_us = item.pwm_us !== undefined ? Number(item.pwm_us) : 1500;
      const voltage_v = item.voltage_v !== undefined ? Number(item.voltage_v) : null;
      const current_a = item.current_a !== undefined ? Number(item.current_a) : null;
      const power_w = item.power_w !== undefined 
        ? Number(item.power_w) 
        : (voltage_v && current_a ? Number((voltage_v * current_a).toFixed(2)) : null);
      const status_code = Number(item.status_code ?? 0);
      const target_rpm = item.target_rpm !== undefined ? Number(item.target_rpm) : null;
      const actual_rpm = item.actual_rpm !== undefined ? Number(item.actual_rpm)
        : (item.rpm !== undefined ? Number(item.rpm) : null);
      const recordedAt = item.datetime ? new Date(item.datetime).toISOString() : new Date().toISOString();

      return {
        device_id: device.id,
        recorded_at: recordedAt,
        is_on,
        speed_percent,
        pwm_us,
        voltage_v,
        current_a,
        power_w,
        status_code,
        rpm: actual_rpm,
        target_rpm
      };
    });

    const latest = rowsToInsert[rowsToInsert.length - 1];
    let archiveOutcome: ArchiveOutcome = 'skipped';
    let ingestWarning: string | null = null;

    // Si Supabase está configurado, guardar en PostgreSQL
    if (isSupabaseConfigured() && device) {
      // 1. Archivar en la tabla dedicada del rol: la fidelidad completa del T-200 vive aquí y
      //    `motor_telemetry` sólo conserva la vista en vivo.
      const experimentId = await resolveActiveExperimentId(body?.experiment_id);
      archiveOutcome = await archiveRolePayload(role, experimentId, body);
      if (archiveOutcome === 'missing_table') {
        console.warn(`[telemetry/motor] Archivado omitido para rol '${role}' (tabla inexistente).`);
      }

      // 2. Vista en vivo. `rpm`/`target_rpm` se envían y, si el entorno no las tiene, se reintenta
      //    sin ellas: PostgREST rechaza el INSERT entero ante una columna inexistente (PGRST204).
      let { error: insertError } = await supabaseAdmin.from('motor_telemetry').insert(rowsToInsert);
      if (insertError?.code === 'PGRST204') {
        console.warn('motor_telemetry sin columnas rpm/target_rpm; reintentando sin ellas');
        const sinRpm = rowsToInsert.map((row: any) => {
          const { rpm, target_rpm, ...rest } = row;
          return rest;
        });
        ({ error: insertError } = await supabaseAdmin.from('motor_telemetry').insert(sinRpm as typeof rowsToInsert));
      }

      if (insertError) {
        console.error('Error guardando telemetría de motor:', insertError);
        ingestWarning = `${insertError.code || 'error'}: ${insertError.message}`;
      }

      await supabaseAdmin
        .from('devices')
        .update({
          status: 'online',
          last_seen_at: latest.recorded_at,
          updated_at: new Date().toISOString()
        })
        .eq('id', device.id);
    }

    return NextResponse.json({
      success: true,
      message: 'Telemetría de motor recibida correctamente',
      archived: archiveOutcome,
      // Un fallo de ingesta no puede ser silencioso: el firmware reportaba HTTP 200 y la pérdida
      // de datos pasaba inadvertida.
      ingested: ingestWarning ? 0 : rowsToInsert.length,
      warning: ingestWarning,
      data: {
        recorded_at: latest.recorded_at,
        is_on: latest.is_on,
        speed_percent: latest.speed_percent,
        pwm_us: latest.pwm_us,
        samples_count: rowsToInsert.length
      }
    });

  } catch (error: any) {
    console.error('Error procesando telemetría de motor:', error);
    return NextResponse.json(
      { error: 'Formato de payload inválido', details: error.message },
      { status: 400 }
    );
  }
}
