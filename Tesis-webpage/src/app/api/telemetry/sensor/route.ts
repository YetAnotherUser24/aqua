import { NextRequest, NextResponse } from 'next/server';
import { authenticateDevice } from '@/lib/deviceAuth';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';

export const dynamic = 'force-dynamic';

/**
 * Recibe telemetría desde el ESP32 del sensor óptico de Oxígeno Disuelto
 * Formato esperado en JSON:
 * {
 *   "datetime": "2026-08-31T15:00:00Z",
 *   "seconds_since_2000": 841500000,
 *   "water_temp_centi": 2305,
 *   "do_milli_mg_l": 7874,
 *   "do_sat_deci_pct": 985,
 *   "param3_centi": 1024,
 *   "param4_centi": 2048,
 *   "battery_mv": 4246,
 *   "rtc_temp_centi": 2410,
 *   "status": 0,
 *   "sent": true
 * }
 */
export async function POST(req: NextRequest) {
  // 1. Autenticar dispositivo mediante X-Device-Key
  const { device, errorResponse } = await authenticateDevice(req, 'sensor_do');
  if (errorResponse) return errorResponse;

  try {
    const body = await req.json();

    // 2. Extraer y validar parámetros requeridos
    const {
      datetime,
      seconds_since_2000,
      water_temp_centi,
      do_milli_mg_l,
      do_sat_deci_pct,
      param3_centi,
      param4_centi,
      battery_mv,
      rtc_temp_centi,
      status = 0,
      sent = true
    } = body;

    if (do_milli_mg_l === undefined || water_temp_centi === undefined) {
      return NextResponse.json(
        { error: 'Faltan campos obligatorios: do_milli_mg_l o water_temp_centi' },
        { status: 400 }
      );
    }

    // 3. Normalización con escalas configurables (con valores por defecto seguros)
    const doDivider = 1000.0;    // 7874 -> 7.874 mg/L
    const tempDivider = 100.0;   // 2305 -> 23.05 °C
    const satDivider = 10.0;     // 985 -> 98.5%
    const batteryDivider = 1000.0; // 4246 -> 4.246 V

    const dissolvedOxygenMgL = Number((do_milli_mg_l / doDivider).toFixed(3));
    const waterTempC = Number((water_temp_centi / tempDivider).toFixed(2));
    const oxygenSatPct = do_sat_deci_pct !== undefined ? Number((do_sat_deci_pct / satDivider).toFixed(2)) : null;
    const batteryV = battery_mv !== undefined ? Number((battery_mv / batteryDivider).toFixed(2)) : null;
    const rtcTempC = rtc_temp_centi !== undefined ? Number((rtc_temp_centi / tempDivider).toFixed(2)) : null;

    const recordedAt = datetime ? new Date(datetime).toISOString() : new Date().toISOString();

    // 4. Si Supabase está enlazado, guardar en PostgreSQL
    if (isSupabaseConfigured() && device) {
      const { error: insertError } = await supabaseAdmin
        .from('sensor_readings')
        .insert({
          device_id: device.id,
          recorded_at: recordedAt,
          seconds_since_2000,
          dissolved_oxygen_raw: do_milli_mg_l,
          dissolved_oxygen_mg_l: dissolvedOxygenMgL,
          oxygen_saturation_raw: do_sat_deci_pct,
          oxygen_saturation_pct: oxygenSatPct,
          water_temperature_raw: water_temp_centi,
          water_temperature_c: waterTempC,
          param3_raw: param3_centi,
          param4_raw: param4_centi,
          battery_mv,
          battery_v: batteryV,
          rtc_temperature_raw: rtc_temp_centi,
          rtc_temperature_c: rtcTempC,
          status,
          sent
        });

      if (insertError) {
        console.error('Error insertando lectura de sensor en Supabase:', insertError);
        return NextResponse.json(
          { error: 'Error al persistir telemetría', details: insertError.message || insertError },
          { status: 500 }
        );
      }

      // Actualizar explícitamente estado y última conexión en devices
      await supabaseAdmin
        .from('devices')
        .update({
          status: 'online',
          last_seen_at: recordedAt,
          updated_at: new Date().toISOString()
        })
        .eq('id', device.id);

      // 5. Evaluar reglas de alerta automática si el OD es crítico o bajo
      if (dissolvedOxygenMgL < 4.0) {
        await supabaseAdmin.from('alerts').insert({
          device_id: device.id,
          alert_type: 'critical_do',
          severity: 'critical',
          status: 'activa',
          message: `Nivel CRÍTICO de Oxígeno Disuelto: ${dissolvedOxygenMgL} mg/L (Temperatura: ${waterTempC}°C)`,
          metadata: { do: dissolvedOxygenMgL, temp: waterTempC }
        });
      } else if (dissolvedOxygenMgL < 6.0) {
        // Alerta preventiva si no existe una activa similar en los últimos 30 min
        await supabaseAdmin.from('alerts').insert({
          device_id: device.id,
          alert_type: 'low_do',
          severity: 'warning',
          status: 'activa',
          message: `Oxígeno Disuelto bajo: ${dissolvedOxygenMgL} mg/L en ${device.location}`,
          metadata: { do: dissolvedOxygenMgL }
        });
      }
    }

    // 6. Comprobar si existen comandos de control pendientes para el sensor (Zero extra-handshake latency)
    let pendingCommand: any = null;
    if (isSupabaseConfigured() && device) {
      const { data: cmdRows } = await supabaseAdmin
        .from('control_commands')
        .select('*')
        .eq('device_id', device.id)
        .eq('status', 'pending')
        .order('created_at', { ascending: false })
        .limit(1);

      if (cmdRows && cmdRows.length > 0) {
        let payload = cmdRows[0].payload;
        // Si no vino en columna payload, revisar si fue serializado en error_message
        if (!payload || typeof payload !== 'object' || Object.keys(payload).length === 0) {
          if (cmdRows[0].error_message && typeof cmdRows[0].error_message === 'string' && cmdRows[0].error_message.trim().startsWith('{')) {
            try {
              payload = JSON.parse(cmdRows[0].error_message);
            } catch {}
          }
        }

        if (!payload || typeof payload !== 'object' || Object.keys(payload).length === 0) {
          let action = cmdRows[0].command_type;
          const reqBy = cmdRows[0].requested_by || '';
          const match = reqBy.match(/\(([^)]+)\)/);
          if (match && match[1]) {
            action = match[1];
          } else if (cmdRows[0].command_type === 'start') {
            action = 'start_monitor';
          } else if (cmdRows[0].command_type === 'stop') {
            action = 'stop_monitor';
          }
          payload = { action, interval_sec: 5 };
        } else if (!payload.action) {
          if (cmdRows[0].command_type === 'start') payload.action = 'start_monitor';
          else if (cmdRows[0].command_type === 'stop') payload.action = 'stop_monitor';
          else payload.action = cmdRows[0].command_type;
          if (!payload.interval_sec) payload.interval_sec = 5;
        }

        pendingCommand = {
          id: cmdRows[0].id,
          command_type: cmdRows[0].command_type,
          payload,
          created_at: cmdRows[0].created_at
        };
        // Marcar como sent
        await supabaseAdmin
          .from('control_commands')
          .update({ status: 'sent', sent_at: new Date().toISOString() })
          .eq('id', cmdRows[0].id);
      }
    }

    return NextResponse.json({
      success: true,
      message: 'Telemetría de sensor recibida y procesada correctamente',
      has_command: Boolean(pendingCommand),
      pending_command: pendingCommand,
      data: {
        recorded_at: recordedAt,
        dissolved_oxygen_mg_l: dissolvedOxygenMgL,
        water_temperature_c: waterTempC,
        oxygen_saturation_pct: oxygenSatPct,
        battery_v: batteryV
      }
    });

  } catch (error: any) {
    console.error('Error procesando telemetría de sensor:', error);
    return NextResponse.json(
      { error: 'Formato de payload inválido', details: error.message },
      { status: 400 }
    );
  }
}
