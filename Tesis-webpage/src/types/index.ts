// Definición de tipos de datos para AquaControl IoT & Supabase

/**
 * Tipo de hardware en `devices.type`.
 *
 * `motor_thruster` es legacy: agrupaba bajo una sola etiqueta al aireador ODrive, al mixer T-200 y
 * a la bomba dosificadora, lo que impedía enrutar comandos y telemetría sin ambigüedad.
 * Los roles concretos son `aerator_motor`, `mixer` y `dosing_pump`; el valor legacy se mantiene
 * porque la restricción CHECK de producción todavía lo admite y hay filas antiguas con él.
 */
export type DeviceType =
  | 'sensor_do'
  | 'aerator_motor'
  | 'mixer'
  | 'dosing_pump'
  | 'motor_thruster'
  | 'gateway';

/** Rol funcional inequívoco de un nodo. Ver `src/lib/deviceRoles.ts`. */
export type DeviceRole = 'sensor' | 'odrive' | 'mixer' | 'pump';

export type DeviceStatus = 'online' | 'offline' | 'warning' | 'error';
export type AlertSeverity = 'info' | 'warning' | 'critical';
export type AlertStatus = 'activa' | 'reconocida' | 'resuelta';
export type CommandStatus = 'pending' | 'sent' | 'acknowledged' | 'failed' | 'expired';
export type EventSource = 'manual' | 'automatic' | 'system' | 'emergency';

export interface Device {
  id: string;
  name: string;
  type: DeviceType;
  /** Rol funcional derivado del `device_id` canónico (ver `deviceRoles.ts`). */
  role?: DeviceRole;
  api_key_hash?: string;
  location: string;
  status: DeviceStatus;
  last_seen_at: string | null;
  metadata?: Record<string, any>;
  created_at: string;
  updated_at?: string;
}

export interface SensorReading {
  id?: number;
  device_id: string;
  recorded_at: string;
  seconds_since_2000?: number;
  // Oxígeno Disuelto
  dissolved_oxygen_raw: number;
  dissolved_oxygen_mg_l: number;
  // Saturación
  oxygen_saturation_raw?: number;
  oxygen_saturation_pct?: number;
  // Temperatura
  water_temperature_raw: number;
  water_temperature_c: number;
  // Parámetros crudos adicionales
  param3_raw?: number;
  param4_raw?: number;
  // Batería
  battery_mv?: number;
  battery_v?: number;
  // RTC
  rtc_temperature_raw?: number;
  rtc_temperature_c?: number;
  status?: number;
  sent?: boolean;
  created_at?: string;
}

export interface MotorTelemetry {
  id?: number;
  device_id: string;
  recorded_at: string;
  is_on: boolean;
  speed_percent: number; // 0 - 100%
  rpm?: number;          // Real RPM telemetry
  pwm_us: number;        // e.g. 1500 (stop), 1760
  voltage_v?: number;
  current_a?: number;
  power_w?: number;
  status_code?: number;
  target_rpm?: number;
  actual_rpm?: number;
  target_rad_s?: number;
  actual_rad_s?: number;
  commanded_duty?: number;
  kp?: number;
  ki?: number;
  kd?: number;
  created_at?: string;
}

export interface MotorEvent {
  id?: number;
  device_id: string;
  started_at: string;
  ended_at?: string | null;
  event_type: 'start' | 'stop' | 'speed_change' | 'warning' | 'error' | 'offline';
  speed_percent: number;
  pwm_us: number;
  source: EventSource;
  notes?: string;
  created_at?: string;
}

export interface Alert {
  id: number;
  device_id: string | null;
  created_at: string;
  alert_type: 'low_do' | 'critical_do' | 'sensor_offline' | 'motor_overtime' | 'communication_error' | 'battery_low' | 'system_error';
  severity: AlertSeverity;
  status: AlertStatus;
  message: string;
  metadata?: Record<string, any>;
  resolved_at?: string | null;
  resolved_by?: string | null;
}

export type SensorCommandAction = 
  | 'start_monitor' 
  | 'stop_monitor' 
  | 'set_sampling_rate' 
  | 'manual_sample' 
  | 'sleep' 
  | 'set_sleep_cycle'
  | 'start_experiment'
  | 'stop_experiment';

export interface SensorCommandPayload {
  action: SensorCommandAction;
  interval_sec?: number;
  minutes?: number;
  indefinite?: boolean;
  measure_time_min?: number;
  experiment_id?: string;
  name?: string;
  csv_filename?: string;
  [key: string]: any;
}

export interface Experiment {
  id: string;
  name: string;
  description?: string;
  sampling_rate_sec: number;
  csv_filename: string;
  status: 'active' | 'completed' | 'stopped';
  mode?: 'manual' | 'pid' | 'fuzzy';
  plant_target?: 'planta_1' | 'planta_2' | 'both';
  // V4: protocolos experimentales (Planta 1 desoxigenación, Planta 2 escalón, Caso B lazo cerrado)
  case_type?: 'planta_1_deox' | 'planta_2_step' | 'closed_loop';
  setpoint_do?: number | null;
  controller_type?: 'none' | 'pid' | 'on_off';
  sampling_rate_sensor_sec?: number;
  sampling_rate_motor_sec?: number;
  parameters?: Record<string, any>;
  started_at: string;
  ended_at?: string | null;
  total_samples: number;
  min_do?: number | null;
  max_do?: number | null;
  avg_do?: number | null;
  metadata?: Record<string, any>;
}

/** Estados globales del orquestador AquaControl V4 */
export type OrchestratorState = 'IDLE' | 'ACTIVE_EXPERIMENT' | 'MANUAL_OVERRIDE';

export interface OverrideFlags {
  master: boolean;
  pump: boolean;
  mixer: boolean;
  odrive: boolean;
}

export interface SystemState {
  state: OrchestratorState;
  since: string;
  experiment_id: string | null;
  override: OverrideFlags;
  updated_by: string;
}

export interface MixerEvent {
  id?: number;
  experiment_id: string;
  event_type: 'start_mixer' | 'stop_mixer' | 'dose_pump' | 'manual_confirmation';
  status?: string | null;
  rtc_timestamp_ms?: number | null;
  created_at?: string;
}

export interface CommandAckPayload {
  success?: boolean;
  actual_speed_percent?: number;
  rtc_timestamp_ms?: number;
  sensor_state?: Record<string, any>;
  error_message?: string;
}

export interface ControlCommand {
  id: string;
  device_id: string;
  command_type: 'start' | 'stop' | 'set_speed' | 'emergency_stop' | 'reboot' | 'set_mode' | 'set_config';
  speed_percent?: number;
  target_rad_s?: number;
  target_rpm?: number;
  pwm_us?: number;
  payload?: Record<string, any> | SensorCommandPayload;
  status: CommandStatus;
  requested_by: string;
  created_at: string;
  sent_at?: string | null;
  executed_at?: string | null;
  error_message?: string | null;
}

export interface SensorNodeConfig {
  monitor_active: boolean;
  monitor_interval_sec: number;
  sleep_cycle_min: number;
  last_command?: string;
  last_command_at?: string;
}

export interface SystemThresholds {
  critical: number;  // e.g. 4.0 mg/L
  warning: number;   // e.g. 6.0 mg/L
  optimal: number;   // e.g. 7.5 mg/L
  unit: string;
}

export interface ScaleFactors {
  do_divider: number;      // e.g. 1000.0 (7874 -> 7.874)
  temp_divider: number;    // e.g. 100.0  (2305 -> 23.05)
  sat_divider: number;     // e.g. 10.0   (985 -> 98.5)
  battery_divider: number; // e.g. 1000.0 (4246 -> 4.246)
}

export interface DashboardSummaryResponse {
  sensorDevice: Device | null;
  motorDevice: Device | null;
  escDevice?: Device | null;
  latestSensorReading: SensorReading | null;
  latestMotorTelemetry: MotorTelemetry | null;
  latestEscTelemetry?: MotorTelemetry | null;
  activeExperiment?: Experiment | null;
  systemState?: SystemState | null;
  thresholds: SystemThresholds;
  activeAlertsCount: number;
  systemHealth: 'optimal' | 'warning' | 'critical' | 'offline';
  isLive: boolean;
}

export interface HistoryDataPoint {
  timestamp: string;
  timeLabel: string;
  dissolved_oxygen_mg_l?: number;
  oxygen_saturation_pct?: number;
  water_temperature_c?: number;
  battery_v?: number;
  motor_speed_percent?: number;
  odrive_rpm?: number;
  motor_is_on?: boolean;
  motor_power_w?: number;
}

export type ClearCategory = 'sensor_readings' | 'motor_telemetry' | 'experiments' | 'alerts_commands';
export type TimeScope = 'all' | 'older_than_1h' | 'older_than_24h' | 'older_than_today';

