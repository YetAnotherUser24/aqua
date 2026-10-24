-- ==============================================================================
-- AQUACONTROL: ESQUEMA DE BASE DE DATOS SUPABASE (POSTGRESQL)
-- Monitoreo y Control de Sistema de Aireación para Acuicultura
-- ==============================================================================

-- 0. Extensión para funciones criptográficas y UUID
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. TABLA: devices (Dispositivos IoT: Sensores OD y Motores Thruster)
CREATE TABLE IF NOT EXISTS public.devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    type VARCHAR(50) NOT NULL CHECK (type IN (
        'sensor_do',
        'aerator_motor',   -- ODrive S1: aireador principal (0-600 RPM, FOC UART)
        'mixer',           -- T-200: agitador auxiliar (PWM 50 Hz)
        'dosing_pump',     -- Bomba peristáltica 12V (Na2SO3)
        'motor_thruster',  -- LEGACY: agrupaba los tres actuadores; se conserva por compatibilidad
        'gateway'
    )),
    api_key_hash VARCHAR(64) NOT NULL, -- Hash SHA-256 del token X-Device-Key
    location VARCHAR(150) DEFAULT 'Estanque Principal',
    status VARCHAR(20) DEFAULT 'offline' CHECK (status IN ('online', 'offline', 'warning', 'error')),
    last_seen_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. TABLA: sensor_readings (Mediciones del ESP32 con sensor óptico de OD)
CREATE TABLE IF NOT EXISTS public.sensor_readings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seconds_since_2000 BIGINT,
    -- Oxígeno Disuelto
    dissolved_oxygen_raw NUMERIC NOT NULL,
    dissolved_oxygen_mg_l NUMERIC(6,3) NOT NULL, -- Ej: 7874 -> 7.874 mg/L
    -- Saturación de Oxígeno
    oxygen_saturation_raw NUMERIC,
    oxygen_saturation_pct NUMERIC(5,2),           -- Ej: 98.5%
    -- Temperatura del Agua
    water_temperature_raw NUMERIC NOT NULL,
    water_temperature_c NUMERIC(5,2) NOT NULL,    -- Ej: 2305 -> 23.05 °C
    -- Parámetros adicionales en crudo para investigación
    param3_raw NUMERIC,
    param4_raw NUMERIC,
    -- Batería del sensor
    battery_mv NUMERIC,
    battery_v NUMERIC(4,2),                       -- Ej: 4246 -> 4.25 V
    -- Temperatura RTC
    rtc_temperature_raw NUMERIC,
    rtc_temperature_c NUMERIC(5,2),
    -- Estado técnico y transmisión
    status INTEGER DEFAULT 0,
    sent BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. TABLA: motor_telemetry (Telemetría en tiempo real del Thruster Blue Robotics T200)
CREATE TABLE IF NOT EXISTS public.motor_telemetry (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_on BOOLEAN NOT NULL DEFAULT FALSE,
    speed_percent NUMERIC(5,2) NOT NULL DEFAULT 0.00, -- 0.0 a 100.0%
    rpm NUMERIC(6,2),                                 -- Velocidad en RPM del motor
    pwm_us INTEGER NOT NULL DEFAULT 1500,             -- 1100µs (max reversa), 1500µs (stop), 1900µs (max avance)
    voltage_v NUMERIC(5,2),                           -- Voltaje batería motor (ej: 14.8V o 12.0V)
    current_a NUMERIC(5,2),                           -- Corriente consumida en Amperios
    power_w NUMERIC(6,2),                             -- Potencia calculada en Watts
    status_code INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. TABLA: motor_events (Eventos significativos: encendido, apagado, cambio de velocidad)
CREATE TABLE IF NOT EXISTS public.motor_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('start', 'stop', 'speed_change', 'warning', 'error', 'offline')),
    speed_percent NUMERIC(5,2) DEFAULT 0.00,
    pwm_us INTEGER DEFAULT 1500,
    source VARCHAR(50) NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'automatic', 'system', 'emergency')),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. TABLA: alerts (Alertas y notificaciones del sistema de acuicultura)
CREATE TABLE IF NOT EXISTS public.alerts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id UUID REFERENCES public.devices(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_type VARCHAR(50) NOT NULL CHECK (alert_type IN ('low_do', 'critical_do', 'sensor_offline', 'motor_overtime', 'communication_error', 'battery_low', 'system_error')),
    severity VARCHAR(20) NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
    status VARCHAR(20) NOT NULL DEFAULT 'activa' CHECK (status IN ('activa', 'reconocida', 'resuelta')),
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    resolved_at TIMESTAMPTZ,
    resolved_by VARCHAR(100)
);

-- 6. TABLA: control_commands (Cola de comandos enviados desde la dashboard al ESP32 del Thruster)
CREATE TABLE IF NOT EXISTS public.control_commands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id UUID NOT NULL REFERENCES public.devices(id) ON DELETE CASCADE,
    command_type VARCHAR(50) NOT NULL CHECK (command_type IN ('start', 'stop', 'set_speed', 'emergency_stop', 'reboot', 'set_mode', 'set_config')),
    speed_percent NUMERIC(5,2) DEFAULT 0.00,
    pwm_us INTEGER DEFAULT 1500,
    payload JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(30) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'acknowledged', 'failed', 'expired')),
    requested_by VARCHAR(100) DEFAULT 'dashboard_user',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    error_message TEXT
);

-- 7. TABLA: sensor_telemetry_bulk (Telemetría OD a 0.2Hz en lotes)
CREATE TABLE IF NOT EXISTS public.sensor_telemetry_bulk (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 8. TABLA: odrive_telemetry_bulk (Telemetría ODrive a 5Hz en lotes)
CREATE TABLE IF NOT EXISTS public.odrive_telemetry_bulk (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 9. TABLA: mixer_events (Log de inyecciones / activaciones de T-200 / Bomba)
CREATE TABLE IF NOT EXISTS public.mixer_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('start_mixer', 'stop_mixer', 'dose_pump', 'manual_confirmation')),
    status VARCHAR(50),
    rtc_timestamp_ms BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 10. TABLA: mixer_telemetry (Archivo de alta fidelidad del agitador T-200)
--     Cada actuador tiene su propia tabla de archivo: mezclarlos impedía separar la dinámica
--     electromecánica del aireador (necesaria para identificar KLa) de los eventos de mezcla.
CREATE TABLE IF NOT EXISTS public.mixer_telemetry (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 11. TABLA: pump_events (Dosificación de Na2SO3)
CREATE TABLE IF NOT EXISTS public.pump_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('dose_pump', 'start_pump', 'stop_pump', 'manual_confirmation')),
    volume_ml NUMERIC(10,3),
    status VARCHAR(50),
    rtc_timestamp_ms BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 12. TABLA: pump_telemetry (Archivo de alta fidelidad de la bomba dosificadora)
CREATE TABLE IF NOT EXISTS public.pump_telemetry (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 13. TABLA: manual_overrides_log (Bitácora de seguridad de anulación manual y E-Stop)
CREATE TABLE IF NOT EXISTS public.manual_overrides_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50),
    target VARCHAR(50) NOT NULL,
    device_id UUID,
    action VARCHAR(100) NOT NULL,
    requested_by VARCHAR(100),
    rtc_timestamp_ms BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 7. TABLA: system_settings (Configuración general de umbrales y factores de escala)
CREATE TABLE IF NOT EXISTS public.system_settings (
    key VARCHAR(50) PRIMARY KEY,
    value JSONB NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ==============================================================================
-- ÍNDICES DE ALTO RENDIMIENTO
-- ==============================================================================
CREATE INDEX IF NOT EXISTS idx_sensor_readings_device_date ON public.sensor_readings(device_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_readings_recorded_at ON public.sensor_readings(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_motor_telemetry_device_date ON public.motor_telemetry(device_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_motor_events_device_date ON public.motor_events(device_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON public.alerts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_control_commands_pending ON public.control_commands(device_id, status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_devices_api_key ON public.devices(api_key_hash);

-- ==============================================================================
-- VALORES DE CONFIGURACIÓN POR DEFECTO
-- ==============================================================================
INSERT INTO public.system_settings (key, value, description)
VALUES 
    ('do_thresholds', '{"critical": 4.0, "warning": 6.0, "optimal": 7.5, "unit": "mg/L"}'::jsonb, 'Umbrales de calidad para Oxígeno Disuelto'),
    ('sensor_scale_factors', '{"do_divider": 1000.0, "temp_divider": 100.0, "sat_divider": 10.0, "battery_divider": 1000.0}'::jsonb, 'Factores de normalización de datos crudos ESP32'),
    ('thruster_pwm_calibration', '{"min_pwm": 1100, "neutral_pwm": 1500, "max_pwm": 1900}'::jsonb, 'Calibración de microsegundos PWM para Blue Robotics T200 (ESC Basic / ESC500)'),
    ('aeration_control_config', '{"control_mode": "pid", "target_do_mg_l": 7.5, "pid_gains": {"kp": 100.0, "ki": 1.5, "kd": 10.0}, "fuzzy_profile": "standard_pond", "min_rpm": 0, "max_rpm": 3200, "failsafe_rpm": 500, "manual_throttle_pct": 0.0, "sampling_interval_ms": 2000, "telemetry_interval_ms": 5000, "config_version": 1}'::jsonb, 'Parámetros del algoritmo de control de aireación (PID, Fuzzy, límites RPM y consignas)'),
    ('experiments_registry', '[]'::jsonb, 'Registro canónico de experimentos activos e históricos')
ON CONFLICT (key) DO NOTHING;

-- ==============================================================================
-- DATOS INICIALES DE DISPOSITIVOS DE PRUEBA / PRODUCCIÓN
-- Token ESP32 Sensor: "ESP32_OD_SENSOR" -> SHA256: 2ae50779e5419027c848d0677699c75164387685b30ed1db9f801c611af0dd5e
-- Token ESP32 Motor:  "ESP32_ODRIVE"    -> SHA256: b40b683230acacb4cdc01d98cb7350a55fff80a4a5ef2dff76e6739e8810f114
-- Token ESP32 ESC:    "ESP32_T_200"     -> SHA256: e981fdf139c8da231cddd2bbdc40b5e372b9407320df6cd462747019cf9404a6
-- Token ESP32 Bomba:  "ESP32_PUMP_KEY_2026" -> SHA256: ver migración 20261007_v4_orchestration.sql
-- ==============================================================================
INSERT INTO public.devices (id, name, type, api_key_hash, location, status, last_seen_at, metadata)
VALUES
    ('a0000000-0000-0000-0000-000000000001', 'Sensor Óptico OD - Estanque 1', 'sensor_do', '2ae50779e5419027c848d0677699c75164387685b30ed1db9f801c611af0dd5e', 'Estanque Principal (Zona Norte)', 'online', NOW(), '{"sensor_model": "Aqualabo DIGISENS", "interface": "Modbus RS485"}'::jsonb),
    ('b0000000-0000-0000-0000-000000000002', 'Controlador ODrive S1 - Estanque 1', 'aerator_motor', 'b40b683230acacb4cdc01d98cb7350a55fff80a4a5ef2dff76e6739e8810f114', 'Estanque Principal (Zona Central)', 'online', NOW(), '{"controller_model": "ODrive S1", "interface": "UART ASCII", "control_mode": "pid", "role": "odrive"}'::jsonb),
    ('c0000000-0000-0000-0000-000000000003', 'Aireador Auxiliar ESC (Banco de Pruebas)', 'mixer', 'e981fdf139c8da231cddd2bbdc40b5e372b9407320df6cd462747019cf9404a6', 'Laboratorio / Banco de Pruebas', 'offline', NOW(), '{"controller_model": "ESP32-S3 ESC PWM", "actuator": "Blue Robotics T200", "status": "auxiliary_backup", "role": "mixer"}'::jsonb),
    ('d0000000-0000-0000-0000-000000000004', 'Bomba Dosificadora Peristáltica (Planta 1)', 'dosing_pump', encode(digest('ESP32_PUMP_KEY_2026', 'sha256'), 'hex'), 'Laboratorio / Banco de Pruebas', 'offline', NOW(), '{"controller_model": "ESP32 + AS5600", "actuator": "12V Peristaltic Pump", "dosing_unit": "mL", "role": "pump"}'::jsonb)
ON CONFLICT (id) DO UPDATE SET 
    name = EXCLUDED.name,
    type = EXCLUDED.type,
    api_key_hash = EXCLUDED.api_key_hash,
    status = EXCLUDED.status,
    metadata = EXCLUDED.metadata,
    last_seen_at = EXCLUDED.last_seen_at;

-- Insertar lectura inicial de ejemplo
INSERT INTO public.sensor_readings (
    device_id, recorded_at, seconds_since_2000, 
    dissolved_oxygen_raw, dissolved_oxygen_mg_l, 
    oxygen_saturation_raw, oxygen_saturation_pct, 
    water_temperature_raw, water_temperature_c, 
    param3_raw, param4_raw, 
    battery_mv, battery_v, 
    rtc_temperature_raw, rtc_temperature_c, 
    status, sent
)
VALUES (
    'a0000000-0000-0000-0000-000000000001', NOW(), 841500000,
    7874, 7.874,
    985, 98.50,
    2305, 23.05,
    1024, 2048,
    4246, 4.25,
    2410, 24.10,
    0, true
);

-- Insertar estado inicial de telemetría de motor
INSERT INTO public.motor_telemetry (
    device_id, recorded_at, is_on, speed_percent, pwm_us, voltage_v, current_a, power_w, status_code
)
VALUES (
    'b0000000-0000-0000-0000-000000000002', NOW(), true, 65.00, 1760, 14.80, 8.50, 125.80, 0
);

-- Insertar evento inicial
INSERT INTO public.motor_events (
    device_id, started_at, event_type, speed_percent, pwm_us, source, notes
)
VALUES (
    'b0000000-0000-0000-0000-000000000002', NOW() - INTERVAL '30 minutes', 'start', 65.00, 1760, 'manual', 'Arranque programado de oxigenación matutina'
);

-- ==============================================================================
-- POLÍTICAS ROW LEVEL SECURITY (RLS)
-- ==============================================================================
ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sensor_readings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.motor_telemetry ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.motor_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.control_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.system_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sensor_telemetry_bulk ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.odrive_telemetry_bulk ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mixer_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mixer_telemetry ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pump_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pump_telemetry ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manual_overrides_log ENABLE ROW LEVEL SECURITY;

-- Política de lectura pública/anónima autorizada para la dashboard (o lectura autenticada si hay Supabase Auth)
CREATE POLICY "Permitir lectura publica de dispositivos" ON public.devices FOR SELECT USING (true);
CREATE POLICY "Permitir lectura publica de lecturas de sensores" ON public.sensor_readings FOR SELECT USING (true);
CREATE POLICY "Permitir lectura publica de telemetria motor" ON public.motor_telemetry FOR SELECT USING (true);
CREATE POLICY "Permitir lectura publica de eventos motor" ON public.motor_events FOR SELECT USING (true);
CREATE POLICY "Permitir lectura publica de alertas" ON public.alerts FOR SELECT USING (true);
CREATE POLICY "Permitir actualizar estado de alertas" ON public.alerts FOR UPDATE USING (true) WITH CHECK (true);
CREATE POLICY "Permitir lectura y creacion de comandos" ON public.control_commands FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Permitir lectura de configuraciones" ON public.system_settings FOR SELECT USING (true);
CREATE POLICY "Permitir full en sensor bulk" ON public.sensor_telemetry_bulk FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Permitir full en odrive bulk" ON public.odrive_telemetry_bulk FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Permitir full en mixer events" ON public.mixer_events FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Permitir full en mixer telemetry" ON public.mixer_telemetry FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Permitir full en pump events" ON public.pump_events FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Permitir full en pump telemetry" ON public.pump_telemetry FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Permitir full en manual overrides log" ON public.manual_overrides_log FOR ALL USING (true) WITH CHECK (true);

-- Habilitar Realtime para tablas críticas en Supabase
ALTER PUBLICATION supabase_realtime ADD TABLE public.sensor_readings;
ALTER PUBLICATION supabase_realtime ADD TABLE public.motor_telemetry;
ALTER PUBLICATION supabase_realtime ADD TABLE public.alerts;
ALTER PUBLICATION supabase_realtime ADD TABLE public.control_commands;
