import { NextRequest, NextResponse } from 'next/server';
import { authenticateDevice } from '@/lib/deviceAuth';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { MAX_BULK_ITEMS, resolveItemEpochMs, NO_STORE_HEADERS } from '@/lib/bulk';
import { archiveRolePayload, ArchiveOutcome, resolveActiveExperimentId } from '@/lib/telemetryArchive';
import { isActuatorRole } from '@/lib/deviceRoles';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

/** Cadencia de persistencia en motor_telemetry (dashboard en vivo). La fidelidad completa vive en la tabla de archivo del rol. */
const DASHBOARD_DOWNSAMPLE_MS = 1000;

export async function POST(req: NextRequest) {
  // Autorización por ROL: el ODrive, el mixer y la bomba comparten el tipo legacy `motor_thruster`,
  // así que el tipo no basta para decidir de quién es la telemetría ni a qué tabla archivarla.
  const { device, role, errorResponse } = await authenticateDevice(req, undefined, undefined);
  if (errorResponse) return errorResponse;
  if (!isActuatorRole(role)) {
    return NextResponse.json(
      { error: 'Endpoint reservado a actuadores (odrive/mixer/pump)' },
      { status: 403, headers: NO_STORE_HEADERS }
    );
  }

  try {
    const body = await req.json();
    const rawPayload = body?.payload;
    const { experiment_id } = body;

    if (!rawPayload || !Array.isArray(rawPayload)) {
      return NextResponse.json({ error: 'Payload must be an array' }, { status: 400, headers: NO_STORE_HEADERS });
    }
    const payload = rawPayload.slice(0, MAX_BULK_ITEMS);

    // Borrador resoluble a partir del experimento activo cuando el firmware envía "backend_resolved".
    let resolvedExperimentId: string = experiment_id || 'idle';
    let archiveOutcome: ArchiveOutcome = 'skipped';
    let ingestWarning: string | null = null;
    let ingestedRows = 0;

    if (isSupabaseConfigured() && device) {
      // 1. Archivar la serie completa en la tabla dedicada del ROL (odrive/mixer/pump).
      //    `backend_resolved` se traduce al experimento activo: guardarlo literal dejaría la fila
      //    huérfana y la descarga CSV por experimento no la encontraría.
      resolvedExperimentId = await resolveActiveExperimentId(experiment_id);
      const archiveOutcomeResult = await archiveRolePayload(role, resolvedExperimentId, payload);
      archiveOutcome = archiveOutcomeResult;
      if (archiveOutcomeResult === 'missing_table') {
        // La tabla de archivo del rol aún no existe (migración pendiente). Se conserva el
        // submuestreo en motor_telemetry para no dejar al dashboard sin datos.
        console.warn(`[motor_bulk] Archivado omitido para rol '${role}' (tabla inexistente).`);
      }

      // 2. Map and bulk insert into motor_telemetry for real-time dashboard visualization.
      // V4/ADR-6: la fidelidad completa (5 Hz) queda en odrive_telemetry_bulk; el dashboard se
      // alimenta con una versión submuestreada a 1 Hz para no saturar la tabla.
      const rowsToInsert = payload
        .map((item: any) => {
          const is_on = Boolean(item.is_on ?? item.is_running);
          const speed_percent = Number(item.speed_percent ?? 0);
          const pwm_us = item.pwm_us !== undefined ? Number(item.pwm_us) : 1500;
          const voltage_v = item.voltage_v !== undefined ? Number(item.voltage_v) : null;
          const current_a = item.current_a !== undefined ? Number(item.current_a) : null;
          const power_w = item.power_w !== undefined
            ? Number(item.power_w)
            : (voltage_v && current_a ? Number((voltage_v * current_a).toFixed(2)) : null);
          const status_code = Number(item.status_code ?? 0);

          const epochMs = resolveItemEpochMs(item) ?? Date.now();
          const recordedAt = new Date(epochMs).toISOString();
          const actual_rpm = item.actual_rpm !== undefined ? Number(item.actual_rpm)
            : (item.rpm !== undefined ? Number(item.rpm) : null);
          const target_rpm = item.target_rpm !== undefined ? Number(item.target_rpm) : null;

          // `rpm` / `target_rpm` existen en producción tras la migración 20261008 (comprobado con
          // `db.js audit`) pero podrían faltar en un entorno más antiguo. Enviar una columna
          // inexistente hace que PostgREST rechace el INSERT COMPLETO (PGRST204) y el lote entero se
          // pierde: por eso el insert de abajo reintenta sin ellas si hace falta.
          return {
            _epochMs: epochMs,
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
        })
        .sort((a, b) => a._epochMs - b._epochMs)
        .filter((row, index, arr) => index === 0 || row._epochMs - arr[index - 1]._epochMs >= DASHBOARD_DOWNSAMPLE_MS)
        .map(({ _epochMs, ...rest }) => rest);

      if (rowsToInsert.length > 0) {
        const insertRows = (rows: typeof rowsToInsert) =>
          supabaseAdmin.from('motor_telemetry').insert(rows);

        let { error: insertError } = await insertRows(rowsToInsert);
        if (insertError?.code === 'PGRST204') {
          console.warn('motor_telemetry sin columnas rpm/target_rpm; reintentando sin ellas');
          const sinRpm = rowsToInsert.map(({ rpm, target_rpm, ...rest }) => rest);
          ({ error: insertError } = await insertRows(sinRpm as typeof rowsToInsert));
        }

        if (insertError) {
          // Antes solo se registraba en consola y la ruta devolvía `success: true` igualmente, así
          // que el firmware reportaba HTTP 200 y la pérdida de datos pasaba inadvertida.
          console.error('Error insertando en motor_telemetry:', insertError);
          ingestWarning = `${insertError.code || 'error'}: ${insertError.message}`;
        } else {
          ingestedRows = rowsToInsert.length;
        }
      }

      await supabaseAdmin.from('devices').update({
        status: 'online',
        last_seen_at: new Date().toISOString()
      }).eq('id', device.id);
    }

    return NextResponse.json({
      success: true,
      received: payload.length,
      ingested: ingestedRows,
      experiment_id: resolvedExperimentId,
      archived: archiveOutcome,
      ...(ingestWarning ? { warning: ingestWarning } : {})
    }, { headers: NO_STORE_HEADERS });
  } catch (error: any) {
    return NextResponse.json({ error: 'Payload error' }, { status: 400, headers: NO_STORE_HEADERS });
  }
}
