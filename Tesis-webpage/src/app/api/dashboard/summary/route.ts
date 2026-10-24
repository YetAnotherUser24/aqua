import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import {
  demoSensorDevice,
  demoMotorDevice,
  demoEscDevice,
  getDemoLatestSensorReading,
  getDemoLatestMotorTelemetry,
  getDemoLatestEscTelemetry,
  demoThresholds,
  demoExperiments
} from '@/lib/demoData';
import { DashboardSummaryResponse, Device, Experiment } from '@/types';
import { getSystemState } from '@/lib/systemState';
import { DEVICE_ID_BY_ROLE, resolveDeviceRole } from '@/lib/deviceRoles';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

const HEARTBEAT_TIMEOUT_MS = 60 * 1000; // 60 segundos sin telemetría -> offline

function evaluateDeviceStatus(device: Device | null): Device | null {
  if (!device) return null;
  const isFresh = Boolean(
    device.last_seen_at &&
    (Date.now() - new Date(device.last_seen_at).getTime() < HEARTBEAT_TIMEOUT_MS)
  );

  return {
    ...device,
    status: isFresh ? 'online' : 'offline'
  };
}

/**
 * Devuelve el estado general del sistema, última medición de OD, estado de motor y alertas
 */
export async function GET(req: NextRequest) {
  try {
    if (!isSupabaseConfigured()) {
      // Retornar datos demo simulados en modo local/demo
      const demoActiveExp = (demoExperiments as Experiment[]).find(e => e.status === 'active') || null;
      const summary: DashboardSummaryResponse = {
        sensorDevice: demoSensorDevice,
        motorDevice: demoMotorDevice,
        escDevice: demoEscDevice,
        latestSensorReading: getDemoLatestSensorReading(),
        latestMotorTelemetry: getDemoLatestMotorTelemetry(),
        latestEscTelemetry: getDemoLatestEscTelemetry(),
        activeExperiment: demoActiveExp,
        thresholds: demoThresholds,
        activeAlertsCount: 1,
        systemHealth: 'optimal',
        isLive: false
      };
      return NextResponse.json(summary);
    }

    // 1. Obtener última lectura de sensor en tiempo real desde sensor_readings (ordenada por ID descendente)
    const { data: latestReadings } = await supabaseAdmin
      .from('sensor_readings')
      .select('*')
      .order('id', { ascending: false })
      .limit(1);
    const latestSensorReading = latestReadings && latestReadings.length > 0 ? latestReadings[0] : null;

    // 2. Obtener dispositivos desde Supabase
    const { data: rawDevices } = await supabaseAdmin
      .from('devices')
      .select('*');

    // Sensor de Oxígeno Disuelto: vinculado por ROL (sensor) o por la lectura más reciente
    let rawSensor = (latestSensorReading?.device_id && rawDevices?.find(d => d.id === latestSensorReading.device_id))
      || rawDevices?.find(d => d.id === DEVICE_ID_BY_ROLE.sensor)
      || rawDevices?.find(d => resolveDeviceRole(d) === 'sensor')
      || null;

    if (!rawSensor) {
      rawSensor = {
        id: latestSensorReading?.device_id || 'a0000000-0000-0000-0000-000000000001',
        name: 'Sensor Óptico OD - Estanque 1',
        type: 'sensor_do',
        location: 'Estanque Principal (Zona Norte)',
        status: 'offline',
        last_seen_at: latestSensorReading?.recorded_at || null,
        metadata: {
          interface: 'Modbus RS485',
          sensor_model: 'Aqualabo DIGISENS',
          monitor_active: false,
          monitor_interval_sec: 5,
          sleep_cycle_min: 15
        },
        created_at: new Date().toISOString()
      };
    } else {
      rawSensor = {
        ...rawSensor,
        metadata: {
          interface: 'Modbus RS485',
          sensor_model: 'Aqualabo DIGISENS',
          monitor_active: false,
          monitor_interval_sec: 5,
          sleep_cycle_min: 15,
          ...(rawSensor.metadata || {})
        }
      };
    }

    // El timestamp más reciente entre devices.last_seen_at y la última lectura real determina la conexión viva
    const sensorLastSeen = latestSensorReading?.recorded_at
      ? (rawSensor.last_seen_at && new Date(rawSensor.last_seen_at).getTime() > new Date(latestSensorReading.recorded_at).getTime()
          ? rawSensor.last_seen_at
          : latestSensorReading.recorded_at)
      : rawSensor.last_seen_at;

    const sensorDevice = evaluateDeviceStatus({
      ...rawSensor,
      last_seen_at: sensorLastSeen
    });

    // Actuador Principal: ODrive S1 (M8325s).
    // Se resuelve por ROL y no por heurística de texto: antes se buscaba "ODrive" en el nombre o
    // `metadata.controller_model`, lo que se rompía al renombrar el dispositivo en la BD.
    let rawMotor = rawDevices?.find(d => d.id === DEVICE_ID_BY_ROLE.odrive)
      || rawDevices?.find(d => resolveDeviceRole(d) === 'odrive')
      || null;

    if (rawMotor) {
      // Normalizar nombre oficial de ODrive S1 si la BD aún tenía nombre legacy
      rawMotor = {
        ...rawMotor,
        name: 'Controlador ODrive S1 - Estanque 1',
        metadata: {
          ...rawMotor.metadata,
          controller_model: 'ODrive S1',
          motor: 'M8325s',
          interface: 'UART ASCII'
        }
      };
    }

    // 3. Obtener última telemetría de ODrive S1
    let latestMotorTelemetry = null;
    if (rawMotor) {
      const { data: telemetries } = await supabaseAdmin
        .from('motor_telemetry')
        .select('*')
        .eq('device_id', rawMotor.id)
        .order('id', { ascending: false })
        .limit(1);
      
      if (telemetries && telemetries.length > 0) {
        latestMotorTelemetry = telemetries[0];
      }
    }

    const motorLastSeen = latestMotorTelemetry?.recorded_at
      ? (rawMotor?.last_seen_at && new Date(rawMotor.last_seen_at).getTime() > new Date(latestMotorTelemetry.recorded_at).getTime()
          ? rawMotor.last_seen_at
          : latestMotorTelemetry.recorded_at)
      : rawMotor?.last_seen_at;

    const motorDevice = evaluateDeviceStatus(rawMotor ? { ...rawMotor, last_seen_at: motorLastSeen } : null);

    if (latestMotorTelemetry && (motorDevice?.status === 'offline' || !latestMotorTelemetry.is_on)) {
      latestMotorTelemetry = {
        ...latestMotorTelemetry,
        is_on: false,
        speed_percent: 0,
        power_w: motorDevice?.status === 'offline' ? 0 : (latestMotorTelemetry.power_w ?? 0)
      };
    }

    // Actuador Auxiliar: Blue Robotics T-200 con ESC. Resuelto por ROL (mixer).
    let rawEsc = rawDevices?.find(d => d.id === DEVICE_ID_BY_ROLE.mixer)
      || rawDevices?.find(d => resolveDeviceRole(d) === 'mixer')
      || null;

    if (!rawEsc) {
      rawEsc = {
        id: 'c0000000-0000-0000-0000-000000000003',
        name: 'Aireador Auxiliar ESC (Banco de Pruebas)',
        type: 'mixer',
        location: 'Laboratorio / Banco de Pruebas',
        status: 'offline',
        last_seen_at: null,
        metadata: { controller_model: 'ESP32-S3 ESC PWM', motor: 'Blue Robotics T200' },
        created_at: new Date().toISOString()
      };
    }
    const escDevice = evaluateDeviceStatus(rawEsc);

    // 4. Obtener última telemetría de ESC Auxiliar (T-200)
    let latestEscTelemetry = null;
    if (escDevice) {
      const { data: escTelemetries } = await supabaseAdmin
        .from('motor_telemetry')
        .select('*')
        .eq('device_id', escDevice.id)
        .order('id', { ascending: false })
        .limit(1);
      
      if (escTelemetries && escTelemetries.length > 0) {
        const escTelem = escTelemetries[0];
        if (escDevice.status === 'offline') {
          latestEscTelemetry = {
            ...escTelem,
            is_on: false,
            speed_percent: 0,
            power_w: 0
          };
        } else {
          latestEscTelemetry = escTelem;
        }
      }
    }

    // 5. Contar alertas activas
    const { count: activeAlertsCount } = await supabaseAdmin
      .from('alerts')
      .select('*', { count: 'exact', head: true })
      .eq('status', 'activa');

    // 6. Determinar salud general del sistema
    let systemHealth: 'optimal' | 'warning' | 'critical' | 'offline' = 'optimal';
    if (!sensorDevice || sensorDevice.status === 'offline') {
      systemHealth = 'warning'; // Sensor fuera de línea
    } else if (latestSensorReading) {
      const doVal = Number(latestSensorReading.dissolved_oxygen_mg_l);
      if (doVal < demoThresholds.critical) {
        systemHealth = 'critical';
      } else if (doVal < demoThresholds.warning) {
        systemHealth = 'warning';
      }
    } else {
      systemHealth = 'offline';
    }

    // 7. Obtener experimento activo desde la fuente canónica (system_settings -> experiments_registry)
    let activeExperiment: Experiment | null = null;
    const { data: settingRow } = await supabaseAdmin
      .from('system_settings')
      .select('value')
      .eq('key', 'experiments_registry')
      .maybeSingle();

    if (settingRow && Array.isArray(settingRow.value)) {
      activeExperiment = settingRow.value.find((e: Experiment) => e.status === 'active') || null;
    }

    // Fallback a metadata de sensor si no se encontró en el registro
    if (!activeExperiment && sensorDevice?.metadata?.active_experiment) {
      activeExperiment = sensorDevice.metadata.active_experiment;
    }

    // 8. Estado global del orquestador (AquaControl V4) — IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE
    const systemState = await getSystemState();

    const summary: DashboardSummaryResponse = {
      sensorDevice,
      motorDevice,
      escDevice,
      latestSensorReading,
      latestMotorTelemetry,
      latestEscTelemetry,
      activeExperiment,
      systemState,
      thresholds: demoThresholds,
      activeAlertsCount: activeAlertsCount || 0,
      systemHealth,
      isLive: true
    };

    return NextResponse.json(summary, {
      headers: {
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'CDN-Cache-Control': 'no-store',
        'Vercel-CDN-Cache-Control': 'no-store'
      }
    });
  } catch (error: any) {
    console.error('Error obteniendo resumen de dashboard:', error);
    return NextResponse.json(
      { error: 'Error al obtener resumen de la dashboard', details: error.message },
      { status: 500 }
    );
  }
}

