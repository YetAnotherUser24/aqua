import { NextRequest, NextResponse } from 'next/server';
import { authenticateDevice } from '@/lib/deviceAuth';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { resolveCommandPayload } from '@/lib/commandPayload';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

/**
 * Permite que cualquier dispositivo ESP32 autenticado (motor_thruster o sensor_do)
 * consulte comandos de control pendientes (polling HTTP/HTTPS)
 */
export async function GET(req: NextRequest) {
  // Autenticar que sea un dispositivo registrado
  const { device, role: deviceRole, errorResponse } = await authenticateDevice(req);
  if (errorResponse) return errorResponse;

  try {
    if (!isSupabaseConfigured() || !device) {
      return NextResponse.json({
        has_command: false,
        command: null
      });
    }

    // 1. Buscar comando pendiente para este dispositivo especifico
    let { data: commands, error } = await supabaseAdmin
      .from('control_commands')
      .select('*')
      .eq('device_id', device.id)
      .eq('status', 'pending')
      .order('created_at', { ascending: false })
      .limit(1);

    // 2. Fallback: SOLO órdenes globales (sin device_id).
    // Antes este bloque tomaba la orden pendiente más reciente de CUALQUIER dispositivo y, como
    // `candidate.payload` no existe en la BD de producción (el payload viaja serializado en
    // error_message), `isSensorCmd` se reducía a ['start','stop']. Para las órdenes del
    // orquestador (`command_type='set_speed'`) eso es false, así que el filtro terminaba siendo
    // cierto para cualquier nodo `motor_thruster`. El mixer y el ODrive son ambos motor_thruster:
    // el que hiciera polling primero se robaba la orden `set_mode` del otro y la marcaba como
    // 'sent', dejando al ODrive sin armar nunca.
    if (!commands || commands.length === 0) {
      const fallbackQuery = await supabaseAdmin
        .from('control_commands')
        .select('*')
        .is('device_id', null)
        .eq('status', 'pending')
        .order('created_at', { ascending: true })
        .limit(1);

      if (fallbackQuery.data && fallbackQuery.data.length > 0) {
        const candidate = fallbackQuery.data[0];
        const candidatePayload = resolveCommandPayload(candidate);
        const candidateAction = String(candidatePayload?.action || '');

        // Si la orden declara su rol destinatario, solo ese rol la puede tomar.
        const targetRole = typeof candidatePayload?.target_role === 'string' ? candidatePayload.target_role : null;
        const roleMatches = targetRole === null || targetRole === deviceRole;

        const isSensorCmd =
          candidateAction.includes('monitor') ||
          candidateAction.includes('experiment') ||
          candidateAction.includes('sample') ||
          ['start', 'stop'].includes(candidate.command_type);

        // El rol manda; `devices.type` solo se usa como último recurso cuando el rol no se pudo
        // resolver (p. ej. una fila legacy sin `device_id` canónico).
        const isSensorDevice = deviceRole ? deviceRole === 'sensor' : device.type === 'sensor_do';

        if (roleMatches && ((isSensorDevice && isSensorCmd) || (!isSensorDevice && !isSensorCmd))) {
          commands = [candidate];
        }
      }
    }

    if (error) {
      console.error('Error buscando comandos pendientes:', error);
      return NextResponse.json({ error: 'Error al consultar ordenes', details: error.message }, { status: 500 });
    }

    if (!commands || commands.length === 0) {
      return NextResponse.json({
        has_command: false,
        command: null
      });
    }

    const command = commands[0];

    // Normalizar payload para que firmware reciba action e interval_sec claros
    let payload: Record<string, any> = resolveCommandPayload(command);

    if (Object.keys(payload).length === 0) {
      let action = command.command_type;
      const reqBy = command.requested_by || '';
      const match = reqBy.match(/\(([^)]+)\)/);
      if (match && match[1]) {
        action = match[1];
      } else if (command.command_type === 'start') {
        action = 'start_monitor';
      } else if (command.command_type === 'stop') {
        action = 'stop_monitor';
      }
      payload = { action, interval_sec: 5 };
    } else if (!payload.action) {
      if (command.command_type === 'start') payload.action = 'start_monitor';
      else if (command.command_type === 'stop') payload.action = 'stop_monitor';
      else payload.action = command.command_type;
      if (!payload.interval_sec) payload.interval_sec = 5;
    }

    // Marcar como 'sent' para evitar envios duplicados
    const updateRes = await supabaseAdmin
      .from('control_commands')
      .update({
        status: 'sent',
        sent_at: new Date().toISOString()
      }, { count: 'exact' })
      .eq('id', command.id)
      .select();
    
    if (updateRes.error) {
      console.error('Error al actualizar status a sent:', updateRes.error);
    }

    return NextResponse.json({
      has_command: true,
      command: {
        id: command.id,
        command_type: command.command_type,
        speed_percent: command.speed_percent !== null ? Number(command.speed_percent) : undefined,
        target_rad_s: payload?.target_rad_s !== undefined ? Number(payload.target_rad_s) : undefined,
        target_rpm: payload?.target_rpm !== undefined ? Number(payload.target_rpm) : undefined,
        pwm_us: command.pwm_us,
        payload,
        created_at: command.created_at
      }
    });

  } catch (error: any) {
    console.error('Error en /api/commands/pending:', error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}
