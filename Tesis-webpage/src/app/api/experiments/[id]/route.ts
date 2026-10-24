import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { Experiment } from '@/types';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

/**
 * DELETE: Eliminar un experimento individual del registro
 * Opcionalmente purga las lecturas asociadas de sensor_readings
 */
export async function DELETE(
  req: NextRequest,
  { params }: { params: { id: string } }
) {
  const { id } = params;

  try {
    let deleteReadings = false;
    try {
      const body = await req.json();
      deleteReadings = Boolean(body.delete_readings);
    } catch {
      const url = new URL(req.url);
      deleteReadings = url.searchParams.get('delete_readings') === 'true';
    }

    if (isSupabaseConfigured()) {
      // 1. Obtener registro de experimentos
      const { data: settingRow } = await supabaseAdmin
        .from('system_settings')
        .select('value')
        .eq('key', 'experiments_registry')
        .maybeSingle();

      let currentList: Experiment[] = settingRow?.value && Array.isArray(settingRow.value) ? settingRow.value : [];
      const targetExp = currentList.find(e => e.id === id);

      if (!targetExp) {
        return NextResponse.json(
          { error: `Experimento '${id}' no encontrado en el registro.` },
          { status: 404 }
        );
      }

      // 2. Si se solicitó, purgar lecturas de sensor asociadas a este experimento
      if (deleteReadings && targetExp.started_at) {
        let query = supabaseAdmin
          .from('sensor_readings')
          .delete()
          .gte('recorded_at', targetExp.started_at);

        if (targetExp.ended_at) {
          query = query.lte('recorded_at', targetExp.ended_at);
        }

        const { error: delReadingsErr } = await query;
        if (delReadingsErr) {
          console.error('Error eliminando lecturas asociadas al experimento:', delReadingsErr);
        }
      }

      // 3. Filtrar y guardar lista actualizada
      const updatedList = currentList.filter(e => e.id !== id);
      await supabaseAdmin
        .from('system_settings')
        .upsert({
          key: 'experiments_registry',
          value: updatedList,
          description: 'Registro histórico de experimentos de oxigenación y muestreo'
        });

      // 4. Si el experimento estaba activo en devices.metadata, limpiarlo y despachar stop al sensor
      const { data: dev } = await supabaseAdmin
        .from('devices')
        .select('metadata')
        .eq('id', 'a0000000-0000-0000-0000-000000000001')
        .maybeSingle();

      if (targetExp.status === 'active' || dev?.metadata?.active_experiment?.id === id) {
        await supabaseAdmin
          .from('devices')
          .update({
            metadata: {
              ...dev?.metadata,
              monitor_active: false,
              active_experiment: null
            }
          })
          .eq('id', 'a0000000-0000-0000-0000-000000000001');

        const stopPayload = {
          action: 'stop_experiment',
          experiment_id: id
        };
        const baseStopCmd = {
          device_id: 'a0000000-0000-0000-0000-000000000001',
          command_type: 'stop',
          speed_percent: 0,
          pwm_us: 1500,
          status: 'pending',
          requested_by: 'Experimento (eliminado)',
          error_message: JSON.stringify(stopPayload)
        };
        const resStop = await supabaseAdmin.from('control_commands').insert({ ...baseStopCmd, payload: stopPayload });
        if (resStop.error) {
          await supabaseAdmin.from('control_commands').insert(baseStopCmd);
        }
      }

      return NextResponse.json({
        success: true,
        message: `Experimento '${targetExp.name}' (${id}) eliminado correctamente.`,
        deleted_readings: deleteReadings
      });
    } else {
      return NextResponse.json({
        success: true,
        message: `Experimento '${id}' eliminado (Modo Demo).`,
        deleted_readings: deleteReadings
      });
    }

  } catch (err: any) {
    console.error('Error eliminando experimento individual:', err);
    return NextResponse.json({ error: err.message }, { status: 500 });
  }
}
