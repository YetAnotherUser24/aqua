import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { demoSensorDevice } from '@/lib/demoData';
import { ClearCategory, TimeScope } from '@/types';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

interface ClearRequestBody {
  categories: ClearCategory[];
  time_scope?: TimeScope;
}

/**
 * POST: Purgar datos de la base de datos de manera granular
 */
export async function POST(req: NextRequest) {
  try {
    const body: ClearRequestBody = await req.json();
    const { categories = [], time_scope = 'all' } = body;

    if (!Array.isArray(categories) || categories.length === 0) {
      return NextResponse.json(
        { error: 'Debe especificar al menos una categoría para limpiar.' },
        { status: 400 }
      );
    }

    // Calcular fecha límite de corte según time_scope
    let cutoffDate: Date;
    const now = new Date();

    switch (time_scope) {
      case 'older_than_1h':
        cutoffDate = new Date(now.getTime() - 60 * 60 * 1000);
        break;
      case 'older_than_24h':
        cutoffDate = new Date(now.getTime() - 24 * 60 * 60 * 1000);
        break;
      case 'older_than_today':
        cutoffDate = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
        break;
      case 'all':
      default:
        cutoffDate = new Date('2099-12-31T23:59:59Z');
        break;
    }

    const cutoffIso = cutoffDate.toISOString();
    const results: Record<string, string> = {};

    if (isSupabaseConfigured()) {
      // 1. Lecturas del Sensor de Oxígeno Disuelto
      if (categories.includes('sensor_readings')) {
        const { error: srErr } = await supabaseAdmin
          .from('sensor_readings')
          .delete()
          .lte('recorded_at', cutoffIso);

        if (srErr) {
          console.error('Error eliminando sensor_readings:', srErr);
          results['sensor_readings'] = `Error: ${srErr.message}`;
        } else {
          results['sensor_readings'] = 'Eliminado exitosamente';
        }
      }

      // 2. Telemetría y Eventos de Motor (ODrive / ESC)
      if (categories.includes('motor_telemetry')) {
        const { error: mtErr } = await supabaseAdmin
          .from('motor_telemetry')
          .delete()
          .lte('recorded_at', cutoffIso);

        const { error: meErr } = await supabaseAdmin
          .from('motor_events')
          .delete()
          .lte('started_at', cutoffIso);

        if (mtErr || meErr) {
          results['motor_telemetry'] = `Error: ${mtErr?.message || meErr?.message}`;
        } else {
          results['motor_telemetry'] = 'Eliminado exitosamente';
        }
      }

      // 3. Alertas y Cola de Comandos
      if (categories.includes('alerts_commands')) {
        const { error: alErr } = await supabaseAdmin
          .from('alerts')
          .delete()
          .lte('created_at', cutoffIso);

        const { error: ccErr } = await supabaseAdmin
          .from('control_commands')
          .delete()
          .lte('created_at', cutoffIso);

        if (alErr || ccErr) {
          results['alerts_commands'] = `Error: ${alErr?.message || ccErr?.message}`;
        } else {
          results['alerts_commands'] = 'Eliminado exitosamente';
        }
      }

      // 4. Registro Histórico de Experimentos
      if (categories.includes('experiments')) {
        // Consultar metadata de dispositivo para preservar atributos y detectar experimento activo
        const { data: dev } = await supabaseAdmin
          .from('devices')
          .select('metadata')
          .eq('id', 'a0000000-0000-0000-0000-000000000001')
          .maybeSingle();

        const hadActiveExp = Boolean(dev?.metadata?.active_experiment);

        if (time_scope === 'all') {
          await supabaseAdmin
            .from('system_settings')
            .upsert({
              key: 'experiments_registry',
              value: [],
              description: 'Registro histórico de experimentos de oxigenación y muestreo'
            });

          // Resetear metadata en dispositivos preservando propiedades de hardware
          await supabaseAdmin
            .from('devices')
            .update({
              metadata: {
                ...(dev?.metadata || {}),
                monitor_active: false,
                active_experiment: null
              }
            })
            .eq('id', 'a0000000-0000-0000-0000-000000000001');

          // Si había un experimento activo, enviar orden de detención al sensor
          if (hadActiveExp) {
            const stopPayload = { action: 'stop_experiment' };
            const baseStopCmd = {
              device_id: 'a0000000-0000-0000-0000-000000000001',
              command_type: 'stop',
              speed_percent: 0,
              pwm_us: 1500,
              status: 'pending',
              requested_by: 'Limpieza BD (stop_experiment)',
              error_message: JSON.stringify(stopPayload)
            };
            const resStop = await supabaseAdmin.from('control_commands').insert({ ...baseStopCmd, payload: stopPayload });
            if (resStop.error) {
              await supabaseAdmin.from('control_commands').insert(baseStopCmd);
            }
          }

          results['experiments'] = 'Registro de experimentos reseteado totalmente';
        } else {
          const { data: settingRow } = await supabaseAdmin
            .from('system_settings')
            .select('value')
            .eq('key', 'experiments_registry')
            .maybeSingle();

          if (settingRow && Array.isArray(settingRow.value)) {
            const cutoffMs = cutoffDate.getTime();
            const filtered = settingRow.value.filter((e: any) => {
              const startMs = new Date(e.started_at).getTime();
              return startMs > cutoffMs; // Conservar los posteriores al corte
            });

            await supabaseAdmin
              .from('system_settings')
              .upsert({
                key: 'experiments_registry',
                value: filtered,
                description: 'Registro histórico de experimentos de oxigenación y muestreo'
              });

            // Si el experimento activo quedó eliminado por el corte de tiempo, resetearlo
            const activeStartMs = dev?.metadata?.active_experiment?.started_at
              ? new Date(dev.metadata.active_experiment.started_at).getTime()
              : 0;

            if (hadActiveExp && activeStartMs <= cutoffMs) {
              await supabaseAdmin
                .from('devices')
                .update({
                  metadata: {
                    ...(dev?.metadata || {}),
                    monitor_active: false,
                    active_experiment: null
                  }
                })
                .eq('id', 'a0000000-0000-0000-0000-000000000001');

              const stopPayload = { action: 'stop_experiment' };
              const baseStopCmd = {
                device_id: 'a0000000-0000-0000-0000-000000000001',
                command_type: 'stop',
                speed_percent: 0,
                pwm_us: 1500,
                status: 'pending',
                requested_by: 'Limpieza BD (stop_experiment)',
                error_message: JSON.stringify(stopPayload)
              };
              const resStop = await supabaseAdmin.from('control_commands').insert({ ...baseStopCmd, payload: stopPayload });
              if (resStop.error) {
                await supabaseAdmin.from('control_commands').insert(baseStopCmd);
              }
            }
          }
          results['experiments'] = 'Experimentos anteriores al corte eliminados';
        }
      }

      // Si se limpian totalmente lecturas o comandos y no hay experimento activo, asegurar que monitor_active no quede huérfano
      if (time_scope === 'all' && (categories.includes('sensor_readings') || categories.includes('alerts_commands') || categories.includes('experiments'))) {
        const { data: settingRow } = await supabaseAdmin
          .from('system_settings')
          .select('value')
          .eq('key', 'experiments_registry')
          .maybeSingle();

        const hasActive = Array.isArray(settingRow?.value) && settingRow.value.some((e: any) => e.status === 'active');
        if (!hasActive) {
          const { data: dev } = await supabaseAdmin
            .from('devices')
            .select('metadata')
            .eq('id', 'a0000000-0000-0000-0000-000000000001')
            .maybeSingle();

          if (dev?.metadata?.monitor_active) {
            await supabaseAdmin
              .from('devices')
              .update({
                metadata: {
                  ...(dev.metadata || {}),
                  monitor_active: false
                },
                updated_at: new Date().toISOString()
              })
              .eq('id', 'a0000000-0000-0000-0000-000000000001');
          }
        }
      }
    } else {
      if (demoSensorDevice?.metadata) {
        demoSensorDevice.metadata.monitor_active = false;
      }
      results['modo_demo'] = 'Datos en memoria reiniciados (Modo Demo)';
    }

    const hasErrors = Object.values(results).some(val => typeof val === 'string' && val.startsWith('Error:'));

    return NextResponse.json({
      success: !hasErrors,
      message: hasErrors 
        ? 'Limpieza completada con advertencias o errores en algunas categorías.'
        : 'Operación de limpieza completada correctamente.',
      details: results,
      time_scope,
      cutoff: cutoffIso
    }, {
      status: hasErrors ? 207 : 200 // 207 Multi-Status para errores parciales
    });

  } catch (err: any) {
    console.error('Error en endpoint de limpieza:', err);
    return NextResponse.json(
      { error: err.message || 'Error interno al limpiar base de datos' },
      { status: 500 }
    );
  }
}
