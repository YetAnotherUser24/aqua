import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, isSupabaseConfigured } from '@/lib/supabase';
import { generateDemoHistory } from '@/lib/demoData';
import { formatPeruTime, formatPeruDateTime } from '@/lib/dateUtils';
import { HistoryDataPoint } from '@/types';

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const fetchCache = 'force-no-store';

/**
 * Obtiene series de tiempo para las gráficas según el rango: '1h', '24h', '7d', o fechas personalizadas
 */
export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const range = searchParams.get('range') || '24h';
  const startDateParam = searchParams.get('startDate');
  const endDateParam = searchParams.get('endDate');

  try {
    let hours = 24;
    if (range === '1h') hours = 1;
    else if (range === '24h') hours = 24;
    else if (range === '7d') hours = 24 * 7;

    const formatLabel = (date: Date) => (range === '7d' ? formatPeruDateTime(date) : formatPeruTime(date));

    if (!isSupabaseConfigured()) {
      const demoData = generateDemoHistory(hours);
      return NextResponse.json({
        success: true,
        range,
        data: demoData,
        isDemo: true
      });
    }

    // Calcular límites temporales
    let since = new Date(Date.now() - hours * 60 * 60 * 1000).toISOString();
    let until = new Date().toISOString();

    if (startDateParam) {
      since = new Date(startDateParam).toISOString();
    }
    if (endDateParam) {
      until = new Date(endDateParam).toISOString();
    }

    // Consultar lecturas de sensor (orden descendente para garantizar puntos recientes si supera el límite)
    const { data: rawSensorData, error: sensorErr } = await supabaseAdmin
      .from('sensor_readings')
      .select('*')
      .gte('recorded_at', since)
      .lte('recorded_at', until)
      .order('recorded_at', { ascending: false })
      .limit(1000);

    const sensorData = rawSensorData ? [...rawSensorData].reverse() : null;

    // Consultar telemetría de Aireador ODrive S1 (orden descendente para garantizar puntos recientes)
    const { data: rawMotorData, error: motorErr } = await supabaseAdmin
      .from('motor_telemetry')
      .select('*')
      .eq('device_id', 'b0000000-0000-0000-0000-000000000002')
      .gte('recorded_at', since)
      .lte('recorded_at', until)
      .order('recorded_at', { ascending: false })
      .limit(300);

    const motorData = rawMotorData ? [...rawMotorData].reverse() : null;

    if (sensorErr || motorErr) {
      console.error('Error consultando historial:', sensorErr || motorErr);
      // Fallback a demo si la consulta falla
      return NextResponse.json({
        success: true,
        range,
        data: generateDemoHistory(hours),
        isDemo: true
      });
    }

    // Si la BD está conectada pero no tiene registros aún en este rango, retornar array vacío real
    if ((!sensorData || sensorData.length === 0) && (!motorData || motorData.length === 0)) {
      return NextResponse.json({
        success: true,
        range,
        data: [],
        isDemo: false,
        emptyDatabase: true
      });
    }

    // Fusionar y estructurar puntos temporales
    const historyPoints: HistoryDataPoint[] = [];

    // Mapear lecturas de sensor
    sensorData?.forEach(s => {
      const date = new Date(s.recorded_at);
      historyPoints.push({
        timestamp: s.recorded_at,
        timeLabel: formatLabel(date),
        dissolved_oxygen_mg_l: Number(s.dissolved_oxygen_mg_l),
        oxygen_saturation_pct: s.oxygen_saturation_pct ? Number(s.oxygen_saturation_pct) : undefined,
        water_temperature_c: Number(s.water_temperature_c),
        battery_v: s.battery_v ? Number(s.battery_v) : undefined
      });
    });

    // Añadir telemetría de Aireador ODrive S1 al punto temporal más cercano o nuevo (Escala 0 a 600 RPM, sin PWM)
    motorData?.forEach(m => {
      const date = new Date(m.recorded_at);
      const isMotorRunning = Boolean(m.is_on) && Number(m.speed_percent) > 0;
      // Se prefiere la RPM real almacenada; `speed_percent` viene cuantizado a 0.1 % (pasos de
      // 0.6 RPM), así que sólo se usa como respaldo de las filas antiguas sin `rpm`.
      const rpmStored = m.rpm === null || m.rpm === undefined ? null : Number(m.rpm);
      const rpmVal = !isMotorRunning ? 0
        : (rpmStored !== null ? Math.round(rpmStored) : Math.round((Number(m.speed_percent) / 100) * 600));
      const existing = historyPoints.find(p => Math.abs(new Date(p.timestamp).getTime() - date.getTime()) < 30000);
      if (existing) {
        existing.motor_speed_percent = isMotorRunning ? Number(m.speed_percent) : 0;
        existing.odrive_rpm = rpmVal;
        existing.motor_is_on = Boolean(m.is_on);
        existing.motor_power_w = m.power_w ? Number(m.power_w) : undefined;
      } else {
        historyPoints.push({
          timestamp: m.recorded_at,
          timeLabel: formatLabel(date),
          motor_speed_percent: isMotorRunning ? Number(m.speed_percent) : 0,
          odrive_rpm: rpmVal,
          motor_is_on: Boolean(m.is_on),
          motor_power_w: m.power_w ? Number(m.power_w) : undefined
        });
      }
    });

    // Ordenar cronológicamente
    historyPoints.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

    return NextResponse.json({
      success: true,
      range,
      data: historyPoints,
      isDemo: false
    });

  } catch (error: any) {
    console.error('Error generando historial de dashboard:', error);
    return NextResponse.json(
      { error: 'Error al obtener historial', details: error.message },
      { status: 500 }
    );
  }
}
