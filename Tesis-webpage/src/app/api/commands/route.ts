import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { demoSensorDevice } from '@/lib/demoData';
import { getSystemState } from '@/lib/systemState';
import { DeviceRole } from '@/types';
import { DEVICE_ID_BY_ROLE } from '@/lib/deviceRoles';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

/** Acciones de "receta automática" que se abortan mientras el orquestador está en MANUAL_OVERRIDE. */
const RECIPE_ACTIONS = ['start_monitor', 'start_experiment', 'set_sampling_rate'];

/**
 * Crea una nueva orden de control para el aireador desde la interfaz de usuario
 * Payload: { device_id, command_type: 'start'|'stop'|'set_speed'|'emergency_stop', speed_percent }
 */
export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const { device_id, command_type, speed_percent = 0, payload = {}, requested_by = 'Operador Web' } = body;

    if (!command_type) {
      return NextResponse.json({ error: 'command_type es requerido' }, { status: 400 });
    }

    const isSensorCommand = 
      device_id === 'a0000000-0000-0000-0000-000000000001' || 
      ['start_monitor', 'stop_monitor', 'start_experiment', 'stop_experiment', 'set_sampling_rate', 'manual_sample', 'sleep', 'set_sleep_cycle'].includes(payload?.action);

    const effectivePayload = {
      action: payload?.action || command_type,
      original_command_type: command_type,
      ...payload
    };

    // V4: en MANUAL_OVERRIDE el operador humano tiene control directo; se abortan las recetas automáticas.
    const isEmergency = command_type === 'emergency_stop' || payload?.action === 'emergency_stop';
    if (!isEmergency && RECIPE_ACTIONS.includes(String(effectivePayload.action))) {
      const current = await getSystemState();
      if (current.state === 'MANUAL_OVERRIDE') {
        return NextResponse.json(
          {
            error: 'Sistema en MANUAL_OVERRIDE: las recetas automáticas están abortadas. Use el interruptor maestro para volver a IDLE.',
            state: current.state
          },
          { status: 409 }
        );
      }
    }

    let speed = 0;
    let pwm_us = 1500;

    if (!isSensorCommand) {
      // Calcular PWM estimado para Blue Robotics T200 (ESC Basic):
      // 0%  -> 1500 us (Neutro / Detenido)
      // 100% -> 1900 us (Maximo Avance)
      speed = Math.max(0, Math.min(100, Number(speed_percent)));
      if (command_type === 'stop' || command_type === 'emergency_stop') {
        speed = 0;
      }
      pwm_us = Math.round(1500 + (speed / 100) * 400);
    }

    if (!isSupabaseConfigured()) {
      if (isSensorCommand && demoSensorDevice?.metadata) {
        const act = effectivePayload.action || command_type;
        if (act === 'start_monitor') {
          demoSensorDevice.metadata.monitor_active = true;
          if (effectivePayload.interval_sec) {
            demoSensorDevice.metadata.monitor_interval_sec = Number(effectivePayload.interval_sec);
          }
        } else if (act === 'stop_monitor' || act === 'sleep') {
          demoSensorDevice.metadata.monitor_active = false;
        } else if (act === 'set_sampling_rate' && effectivePayload.interval_sec) {
          demoSensorDevice.metadata.monitor_interval_sec = Number(effectivePayload.interval_sec);
        } else if (act === 'set_sleep_cycle' && effectivePayload.measure_time_min) {
          demoSensorDevice.metadata.sleep_cycle_min = Number(effectivePayload.measure_time_min);
        }
      }

      return NextResponse.json({
        success: true,
        message: isSensorCommand 
          ? `Comando para sensor registrado con éxito (${effectivePayload.action || command_type}) [Modo Demo]`
          : 'Comando registrado con exito (Modo Demo Local)',
        command: {
          id: 'demo-cmd-' + Date.now(),
          device_id: device_id || (isSensorCommand ? 'a0000000-0000-0000-0000-000000000001' : 'b0000000-0000-0000-0000-000000000002'),
          command_type,
          speed_percent: speed,
          pwm_us,
          payload: effectivePayload,
          status: 'pending',
          created_at: new Date().toISOString()
        }
      });
    }

    // Resolver ID del dispositivo si no fue enviado.
    // Se resuelve por ROL canónico: `.eq('type','motor_thruster').limit(1)` podía devolver el
    // mixer o la bomba en lugar del aireador, porque los tres comparten esa etiqueta legacy.
    let targetDeviceId = device_id;
    if (!targetDeviceId) {
      const targetRole: DeviceRole = isSensorCommand ? 'sensor' : 'odrive';
      const roleDeviceId = DEVICE_ID_BY_ROLE[targetRole];

      const { data: foundDev } = await supabaseAdmin
        .from('devices')
        .select('id')
        .eq('id', roleDeviceId)
        .limit(1);

      if (foundDev && foundDev.length > 0) {
        targetDeviceId = foundDev[0].id;
      } else {
        return NextResponse.json(
          { error: `No se encontró el dispositivo con rol '${targetRole}' registrado` },
          { status: 404 }
        );
      }
    }

    // Normalizar command_type para satisfacer el constraint de la BD: ('start', 'stop', 'set_speed', 'emergency_stop', 'reboot')
    const allowedDbTypes = ['start', 'stop', 'set_speed', 'emergency_stop', 'reboot'];
    let dbCommandType = command_type;
    if (!allowedDbTypes.includes(dbCommandType)) {
      if (isSensorCommand) {
        const act = payload?.action || '';
        dbCommandType = (act === 'sleep' || act === 'stop_monitor' || act === 'stop_experiment' || command_type === 'stop') 
          ? 'stop' 
          : 'start';
      } else {
        dbCommandType = 'set_speed';
      }
    }

    const actionTag = effectivePayload.action || command_type;
    const reqBy = requested_by.includes('(') ? requested_by : `${requested_by} (${actionTag})`;

    // Insertar comando en la cola de control_commands
    // Nota de robustez: Si la tabla en Supabase no tiene la columna 'payload', guardar JSON en error_message (columna TEXT)
    const baseCommand = {
      device_id: targetDeviceId,
      command_type: dbCommandType,
      speed_percent: speed,
      pwm_us,
      status: 'pending',
      requested_by: reqBy
    };

    let cmd: any = null;
    let error: any = null;

    const resWithPayload = await supabaseAdmin
      .from('control_commands')
      .insert({
        ...baseCommand,
        payload: effectivePayload,
        error_message: JSON.stringify(effectivePayload)
      })
      .select()
      .single();

    if (resWithPayload.error) {
      // Reintentar sin columna payload (guardando el payload serializado en error_message)
      const resWithoutPayload = await supabaseAdmin
        .from('control_commands')
        .insert({
          ...baseCommand,
          error_message: JSON.stringify(effectivePayload)
        })
        .select()
        .single();
      cmd = resWithoutPayload.data;
      error = resWithoutPayload.error;
    } else {
      cmd = resWithPayload.data;
      error = resWithPayload.error;
    }

    if (error) {
      console.error('Error creando comando de control:', error);
      return NextResponse.json(
        {
          error: 'Error al enviar orden al dispositivo',
          details: error.message || error,
          code: error.code,
          hint: error.hint
        },
        { status: 500 }
      );
    }

    // Actualizar metadatos del sensor inmediatamente para reflejar la intención del operador sin esperar ack
    if (isSensorCommand) {
      try {
        const action = effectivePayload.action;
        let metadataUpdates: Record<string, any> | null = null;
        if (action === 'start_monitor') {
          metadataUpdates = {
            monitor_active: true,
            ...(effectivePayload.interval_sec ? { monitor_interval_sec: Number(effectivePayload.interval_sec) } : {})
          };
        } else if (action === 'stop_monitor' || action === 'sleep') {
          metadataUpdates = { monitor_active: false };
        } else if (action === 'set_sampling_rate' && effectivePayload.interval_sec) {
          metadataUpdates = { monitor_interval_sec: Number(effectivePayload.interval_sec) };
        } else if (action === 'set_sleep_cycle' && effectivePayload.measure_time_min) {
          metadataUpdates = { sleep_cycle_min: Number(effectivePayload.measure_time_min) };
        }

        if (metadataUpdates) {
          const { data: currentDev } = await supabaseAdmin
            .from('devices')
            .select('metadata')
            .eq('id', targetDeviceId)
            .maybeSingle();

          const currentMeta = currentDev?.metadata || {};
          await supabaseAdmin
            .from('devices')
            .update({
              metadata: { ...currentMeta, ...metadataUpdates },
              updated_at: new Date().toISOString()
            })
            .eq('id', targetDeviceId);
        }
      } catch (metaErr) {
        console.warn('Advertencia actualizando metadatos del sensor:', metaErr);
      }
    }

    // Registrar evento de motor de forma no-bloqueante (solo para actuadores motor)
    if (!isSensorCommand) {
      try {
        await supabaseAdmin.from('motor_events').insert({
          device_id: targetDeviceId,
          event_type: command_type === 'stop' ? 'stop' : (command_type === 'start' ? 'start' : 'speed_change'),
          speed_percent: speed,
          pwm_us,
          source: 'manual',
          notes: `Comando '${command_type}' enviado desde la dashboard web (${speed}% / ${pwm_us}us)`
        });
      } catch (eventErr) {
        console.warn('Advertencia registrando evento de motor:', eventErr);
      }
    }

    return NextResponse.json({
      success: true,
      message: isSensorCommand 
        ? `Comando para sensor registrado con éxito (${effectivePayload.action})`
        : 'Comando emitido exitosamente. En espera de confirmacion por el ESP32.',
      command: {
        ...(cmd || baseCommand),
        command_type: effectivePayload.original_command_type || cmd?.command_type || dbCommandType,
        payload: effectivePayload
      }
    });

  } catch (error: any) {
    console.error('Error en POST /api/commands:', error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}
