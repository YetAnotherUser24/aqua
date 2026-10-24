import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { demoExperiments } from '@/lib/demoData';
import { Experiment } from '@/types';
import { enqueueCommand, setSystemState, broadcastState, DEVICE_IDS, snapshotPendingOrchestrationIds } from '@/lib/systemState';
import { resolveActuatorRecipe } from '@/lib/experimentRecipe';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

// En memoria para modo local/demo
let inMemoryExperiments: Experiment[] = [...demoExperiments as Experiment[]];

/**
 * GET: Obtener lista de experimentos registrados
 */
export async function GET(req: NextRequest) {
  try {
    if (!isSupabaseConfigured()) {
      return NextResponse.json({
        success: true,
        experiments: inMemoryExperiments
      });
    }

    // Intentar leer desde system_settings (clave: 'experiments_registry') o tabla dedicada
    const { data: settingRow } = await supabaseAdmin
      .from('system_settings')
      .select('value')
      .eq('key', 'experiments_registry')
      .maybeSingle();

    let experiments: Experiment[] = [];
    if (settingRow && settingRow.value) {
      experiments = Array.isArray(settingRow.value) ? settingRow.value : [];
    } else {
      experiments = inMemoryExperiments;
    }

    return NextResponse.json({
      success: true,
      experiments
    });
  } catch (err: any) {
    console.error('Error obteniendo lista de experimentos:', err);
    return NextResponse.json(
      { error: 'Error al consultar experimentos', details: err.message },
      { status: 500 }
    );
  }
}

/**
 * POST: Iniciar un nuevo experimento
 * Body: { name, sampling_rate_sec, csv_filename, description }
 */
export async function POST(req: NextRequest) {
  try {
    // Órdenes ya encoladas al empezar la transición. Se capturan por identificador (no por marca
    // temporal: el desfase de relojes Vercel/Postgres es del mismo orden que la ventana entre
    // encolar y difundir). Así la difusión invalida lo del estado anterior pero nunca la orden que
    // este mismo handler acaba de encolar, como el `start_experiment` del sensor.
    const staleCommandIds = await snapshotPendingOrchestrationIds();
    const body = await req.json();
    const {
      name,
      sampling_rate_sec = 5,
      csv_filename,
      description = '',
      case_type = 'planta_2_step',
      setpoint_do = null,
      controller_type = 'none',
      plant_target = 'both',
      sampling_rate_sensor_sec,
      sampling_rate_motor_sec,
      parameters = {}
    } = body;

    if (!name || name.trim() === '') {
      return NextResponse.json({ error: 'El nombre del experimento es obligatorio' }, { status: 400 });
    }

    // Sanitizar nombre de archivo CSV para 8.3 FAT en firmware
    const cleanFilename = (csv_filename || `EXP_${Date.now().toString().slice(-4)}.CSV`)
      .toUpperCase()
      .replace(/[^A-Z0-9_.]/g, '')
      .slice(0, 12);
    const finalFilename = cleanFilename.endsWith('.CSV') ? cleanFilename : `${cleanFilename}.CSV`;

    const newExperiment: Experiment = {
      id: `exp_${Date.now()}`,
      name: name.trim(),
      description,
      sampling_rate_sec: Math.max(1, Math.min(60, Number(sampling_rate_sec))),
      csv_filename: finalFilename,
      status: 'active',
      case_type,
      setpoint_do: setpoint_do !== null && setpoint_do !== undefined ? Number(setpoint_do) : null,
      controller_type,
      plant_target,
      // V4 §2.2: frecuencias desacopladas por dinámica de planta.
      sampling_rate_sensor_sec: Number(sampling_rate_sensor_sec ?? sampling_rate_sec ?? 5),
      sampling_rate_motor_sec: Number(sampling_rate_motor_sec ?? 0.2),
      parameters: parameters && typeof parameters === 'object' ? parameters : {},
      started_at: new Date().toISOString(),
      ended_at: null,
      total_samples: 0
    };

    // 1. Despachar comando start_experiment al sensor mediante control_commands
    const targetDeviceId = 'a0000000-0000-0000-0000-000000000001';
    const commandPayload = {
      action: 'start_experiment',
      experiment_id: newExperiment.id,
      name: newExperiment.name,
      interval_sec: newExperiment.sampling_rate_sec,
      csv_filename: newExperiment.csv_filename
    };

    if (isSupabaseConfigured()) {
      // Guardar comando en cola con fallback si no existe columna payload
      const baseCmd = {
        device_id: targetDeviceId,
        command_type: 'start',
        speed_percent: 0,
        pwm_us: 1500,
        status: 'pending',
        requested_by: `Experimento (${commandPayload.action})`,
        error_message: JSON.stringify(commandPayload)
      };
      const resWithPayload = await supabaseAdmin.from('control_commands').insert({ ...baseCmd, payload: commandPayload });
      if (resWithPayload.error) {
        await supabaseAdmin.from('control_commands').insert(baseCmd);
      }

      // Actualizar metadatos de sensor para indicar experimento activo
      const { data: dev } = await supabaseAdmin
        .from('devices')
        .select('metadata')
        .eq('id', targetDeviceId)
        .single();

      const currentMeta = dev?.metadata || {};
      await supabaseAdmin
        .from('devices')
        .update({
          metadata: {
            ...currentMeta,
            monitor_active: true,
            active_experiment: newExperiment,
            monitor_interval_sec: newExperiment.sampling_rate_sec
          }
        })
        .eq('id', targetDeviceId);

      // Persistir lista de experimentos en system_settings
      const { data: settingRow } = await supabaseAdmin
        .from('system_settings')
        .select('value')
        .eq('key', 'experiments_registry')
        .maybeSingle();

      let currentList: Experiment[] = settingRow?.value && Array.isArray(settingRow.value) ? settingRow.value : [];
      // Marcar previos como completados si quedaron colgados
      currentList = currentList.map(e => e.status === 'active' ? { ...e, status: 'completed', ended_at: new Date().toISOString() } : e);
      currentList.unshift(newExperiment);

      await supabaseAdmin
        .from('system_settings')
        .upsert({
          key: 'experiments_registry',
          value: currentList,
          description: 'Registro histórico de experimentos de oxigenación y muestreo'
        });
    } else {
      inMemoryExperiments = inMemoryExperiments.map(e => e.status === 'active' ? { ...e, status: 'completed', ended_at: new Date().toISOString() } : e);
      inMemoryExperiments.unshift(newExperiment);
    }

    // V4: orquestación — fijar estado global y difundir la intención a TODOS los nodos.
    // Se usa `broadcastState` (la misma vía que la barra del orquestador) en lugar de encolar
    // comandos a mano: así el SENSOR también recibe `set_state ACTIVE_EXPERIMENT` y empieza a
    // muestrear. Antes sólo se avisaba a mixer y ODrive, de modo que un experimento iniciado desde
    // el asistente dejaba a la boya sin muestrear y la curva salía vacía.
    const armed: string[] = [];
    try {
      const next = await setSystemState({
        state: 'ACTIVE_EXPERIMENT',
        experiment_id: newExperiment.id,
        updated_by: `Experimento (${case_type})`
      });

      const recipe = resolveActuatorRecipe(newExperiment);
      if (recipe.mixer === 'on') armed.push('mixer');
      else armed.push('mixer_off');
      if (recipe.motor.mode !== 'off') armed.push('odrive');

      await broadcastState(next, `Orquestador (${case_type})`, staleCommandIds);
    } catch (orchErr) {
      console.warn('Advertencia en orquestación de experimento:', orchErr);
    }

    return NextResponse.json({
      success: true,
      message: `Experimento '${newExperiment.name}' iniciado exitosamente`,
      experiment: newExperiment,
      armed
    });

  } catch (err: any) {
    console.error('Error iniciando experimento:', err);
    return NextResponse.json({ error: err.message }, { status: 500 });
  }
}

/**
 * PATCH: Detener un experimento activo
 * Body: { experiment_id }
 */
export async function PATCH(req: NextRequest) {
  try {
    const staleCommandIds = await snapshotPendingOrchestrationIds();
    const body = await req.json();
    const { experiment_id } = body;

    const targetDeviceId = DEVICE_IDS.sensor;
    const stopPayload = {
      action: 'stop_experiment',
      experiment_id
    };

    // V4: desarmar TODOS los actuadores y volver a IDLE al detener el experimento.
    // Se difunde con `broadcastState` (no comandos sueltos) para que el SENSOR también reciba
    // `set_state IDLE`: si no, la boya sigue muestreando y grabando en SD después de parar.
    try {
      const next = await setSystemState({
        state: 'IDLE',
        experiment_id: null,
        updated_by: 'Experimento (stop_experiment)'
      });
      await broadcastState(next, 'Orquestador (stop_experiment)', staleCommandIds);
    } catch (orchErr) {
      console.warn('Advertencia desarmando actuadores:', orchErr);
    }

    if (isSupabaseConfigured()) {
      // 1. Metadatos del sensor: reflejar que ya no hay monitor activo.
      // El `stop` ya se encoló vía broadcastState para los 4 nodos; aquí sólo se limpia la metadata.
      // 2. Actualizar metadatos de sensor
      const { data: dev } = await supabaseAdmin
        .from('devices')
        .select('metadata')
        .eq('id', targetDeviceId)
        .single();

      const currentMeta = dev?.metadata || {};
      await supabaseAdmin
        .from('devices')
        .update({
          metadata: {
            ...currentMeta,
            monitor_active: false,
            active_experiment: null
          }
        })
        .eq('id', targetDeviceId);

      // 3. Finalizar en lista de experimentos
      const { data: settingRow } = await supabaseAdmin
        .from('system_settings')
        .select('value')
        .eq('key', 'experiments_registry')
        .maybeSingle();

      if (settingRow && Array.isArray(settingRow.value)) {
        const updated = settingRow.value.map((e: Experiment) => {
          if (!experiment_id || e.id === experiment_id || e.status === 'active') {
            return {
              ...e,
              status: 'completed',
              ended_at: new Date().toISOString()
            };
          }
          return e;
        });

        await supabaseAdmin
          .from('system_settings')
          .upsert({
            key: 'experiments_registry',
            value: updated
          });
      }
    } else {
      inMemoryExperiments = inMemoryExperiments.map(e => {
        if (!experiment_id || e.id === experiment_id || e.status === 'active') {
          return {
            ...e,
            status: 'completed',
            ended_at: new Date().toISOString()
          };
        }
        return e;
      });
    }

    return NextResponse.json({
      success: true,
      message: 'Experimento finalizado correctamente'
    });

  } catch (err: any) {
    console.error('Error deteniendo experimento:', err);
    return NextResponse.json({ error: err.message }, { status: 500 });
  }
}
