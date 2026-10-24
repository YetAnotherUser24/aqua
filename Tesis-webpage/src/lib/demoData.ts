import {
  Device,
  SensorReading,
  MotorTelemetry,
  MotorEvent,
  Alert,
  ControlCommand,
  SystemThresholds,
  DashboardSummaryResponse,
  HistoryDataPoint
} from '@/types';

export const demoThresholds: SystemThresholds = {
  critical: 4.0,
  warning: 6.0,
  optimal: 7.5,
  unit: 'mg/L'
};

export const demoSensorDevice: Device = {
  id: 'a0000000-0000-0000-0000-000000000001',
  name: 'Sensor Óptico OD - Estanque 1',
  type: 'sensor_do',
  location: 'Estanque Principal (Zona Norte)',
  status: 'online',
  last_seen_at: new Date().toISOString(),
  created_at: '2026-08-01T00:00:00Z',
  metadata: {
    sensor_model: 'Aqualabo DIGISENS',
    interface: 'Modbus RS485',
    monitor_active: false,
    monitor_interval_sec: 5,
    sleep_cycle_min: 15
  }
};

export const demoMotorDevice: Device = {
  id: 'b0000000-0000-0000-0000-000000000002',
  name: 'Controlador ODrive S1 (M8325s) - Estanque 1',
  type: 'motor_thruster',
  location: 'Estanque Principal (Zona Central)',
  status: 'online',
  last_seen_at: new Date().toISOString(),
  created_at: '2026-08-01T00:00:00Z',
  metadata: { controller_model: 'ODrive S1', motor: 'M8325s', interface: 'UART ASCII' }
};

export const demoEscDevice: Device = {
  id: 'c0000000-0000-0000-0000-000000000003',
  name: 'Aireador Auxiliar T-200 (ESC) - Banco de Pruebas',
  type: 'motor_thruster',
  location: 'Laboratorio / Banco de Pruebas',
  status: 'online',
  last_seen_at: new Date().toISOString(),
  created_at: '2026-08-01T00:00:00Z',
  metadata: { controller_model: 'ESP32-S3 ESC PWM', motor: 'Blue Robotics T200' }
};

export function getDemoLatestSensorReading(): SensorReading {
  return {
    id: 101,
    device_id: demoSensorDevice.id,
    recorded_at: new Date().toISOString(),
    seconds_since_2000: 841500000,
    dissolved_oxygen_raw: 7874,
    dissolved_oxygen_mg_l: 7.874,
    oxygen_saturation_raw: 985,
    oxygen_saturation_pct: 98.5,
    water_temperature_raw: 2305,
    water_temperature_c: 23.05,
    param3_raw: 1024,
    param4_raw: 2048,
    battery_mv: 4246,
    battery_v: 4.25,
    rtc_temperature_raw: 2410,
    rtc_temperature_c: 24.10,
    status: 0,
    sent: true
  };
}

export function getDemoLatestMotorTelemetry(): MotorTelemetry {
  return {
    id: 201,
    device_id: demoMotorDevice.id,
    recorded_at: new Date().toISOString(),
    is_on: true,
    speed_percent: 65,
    rpm: 1200,
    pwm_us: 1760,
    voltage_v: 24.1,
    current_a: 6.2,
    power_w: 149.4,
    status_code: 0
  };
}

export function getDemoLatestEscTelemetry(): MotorTelemetry {
  return {
    id: 202,
    device_id: demoEscDevice.id,
    recorded_at: new Date().toISOString(),
    is_on: false,
    speed_percent: 0,
    rpm: 0,
    pwm_us: 1500,
    voltage_v: 14.8,
    current_a: 0.0,
    power_w: 0.0,
    status_code: 0
  };
}

export function generateDemoHistory(hours: number = 24): HistoryDataPoint[] {
  const points: HistoryDataPoint[] = [];
  const now = new Date();
  const stepMinutes = hours <= 1 ? 2 : hours <= 24 ? 30 : 180;
  const totalPoints = Math.floor((hours * 60) / stepMinutes);

  for (let i = totalPoints; i >= 0; i--) {
    const time = new Date(now.getTime() - i * stepMinutes * 60 * 1000);
    // Ciclo diurno simulado para OD y temperatura
    const hourOfDay = time.getHours() + time.getMinutes() / 60;
    const diurnalFactor = Math.sin(((hourOfDay - 6) / 24) * 2 * Math.PI); // Pico en la tarde, bajo en la madrugada

    // Simular que el aireador se enciende cuando OD < 6.2 mg/L
    const baseDO = 6.8 + diurnalFactor * 1.5;
    const motorActive = baseDO < 6.5 || (hourOfDay >= 2 && hourOfDay <= 7);
    const speed = motorActive ? (baseDO < 5.0 ? 85 : 60) : 0;
    const doWithAeration = motorActive ? baseDO + 1.2 : baseDO;

    const formattedDO = Number((Math.max(3.8, Math.min(9.5, doWithAeration + (Math.random() * 0.2 - 0.1)))).toFixed(2));
    const formattedTemp = Number((22.5 + diurnalFactor * 1.8 + (Math.random() * 0.1)).toFixed(2));
    const formattedSat = Number((formattedDO * 12.5).toFixed(1));

    points.push({
      timestamp: time.toISOString(),
      timeLabel: time.toLocaleTimeString('es-PE', { timeZone: 'America/Lima', hour: '2-digit', minute: '2-digit', hour12: false }),
      dissolved_oxygen_mg_l: formattedDO,
      oxygen_saturation_pct: formattedSat,
      water_temperature_c: formattedTemp,
      battery_v: Number((4.2 - (i / totalPoints) * 0.05).toFixed(2)),
      motor_speed_percent: speed,
      odrive_rpm: Math.round((speed / 100) * 600),
      motor_is_on: speed > 0,
      motor_power_w: speed > 0 ? Number((speed * 1.95 + (Math.random() * 5)).toFixed(1)) : 0
    });
  }

  return points;
}

export const demoEvents: MotorEvent[] = [
  {
    id: 1,
    device_id: demoMotorDevice.id,
    started_at: new Date(Date.now() - 35 * 60 * 1000).toISOString(),
    event_type: 'start',
    speed_percent: 65,
    pwm_us: 1760,
    source: 'manual',
    notes: 'Arranque de oxigenación ejecutado desde dashboard'
  },
  {
    id: 2,
    device_id: demoMotorDevice.id,
    started_at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
    event_type: 'speed_change',
    speed_percent: 40,
    pwm_us: 1660,
    source: 'automatic',
    notes: 'Ajuste de velocidad por umbral preventivo (OD = 6.1 mg/L)'
  },
  {
    id: 3,
    device_id: demoMotorDevice.id,
    started_at: new Date(Date.now() - 5 * 3600 * 1000).toISOString(),
    event_type: 'stop',
    speed_percent: 0,
    pwm_us: 1500,
    source: 'automatic',
    notes: 'Detención por nivel de oxígeno óptimo alcanzado (OD = 8.2 mg/L)'
  }
];

export const demoAlerts: Alert[] = [
  {
    id: 1,
    device_id: demoSensorDevice.id,
    created_at: new Date(Date.now() - 45 * 60 * 1000).toISOString(),
    alert_type: 'low_do',
    severity: 'warning',
    status: 'activa',
    message: 'Oxígeno disuelto en rango de advertencia (5.8 mg/L en Estanque Principal). Aireador compensando.',
    metadata: { value: 5.8, threshold: 6.0 }
  },
  {
    id: 2,
    device_id: demoMotorDevice.id,
    created_at: new Date(Date.now() - 120 * 60 * 1000).toISOString(),
    alert_type: 'motor_overtime',
    severity: 'info',
    status: 'reconocida',
    message: 'Thruster T200 continuo por más de 3 horas. Eficiencia térmica nominal.',
    metadata: { runtime_minutes: 195 }
  }
];

export const demoExperiments = [
  {
    id: 'exp_demo_01',
    name: 'Ensayo Curva Reoxigenación ODrive (600 RPM)',
    description: 'Evaluación de tasa de transferencia de oxígeno kLa con motor a 100% de aceleración.',
    sampling_rate_sec: 2,
    csv_filename: 'EXP_01.CSV',
    status: 'completed',
    started_at: new Date(Date.now() - 24 * 3600 * 1000).toISOString(),
    ended_at: new Date(Date.now() - 23 * 3600 * 1000).toISOString(),
    total_samples: 1800,
    min_do: 4.12,
    max_do: 8.45,
    avg_do: 6.85,
    metadata: { controller: 'ODrive S1', rpm: 600 }
  },
  {
    id: 'exp_demo_02',
    name: 'Prueba de Consumo Nocturno y Calibración DIGISENS',
    description: 'Registro de descenso biológico de OD sin aireación activa durante la noche.',
    sampling_rate_sec: 5,
    csv_filename: 'EXP_02.CSV',
    status: 'completed',
    started_at: new Date(Date.now() - 48 * 3600 * 1000).toISOString(),
    ended_at: new Date(Date.now() - 44 * 3600 * 1000).toISOString(),
    total_samples: 2880,
    min_do: 3.85,
    max_do: 7.92,
    avg_do: 5.40,
    metadata: { aerator_status: 'off' }
  }
];
