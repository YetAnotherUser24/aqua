-- ==============================================================================
-- AquaControl V4 — Orquestación, Reloj Maestro y Trazabilidad de Alta Frecuencia
-- Fecha: 2026-10-07
--
-- NOTA DE DISEÑO: la implementación funcional de V4 opera sobre el esquema existente:
--   * system_settings.system_state           -> máquina de estados global (sin DDL)
--   * control_commands.payload.executed_rtc_ms -> instante exacto de ejecución (JSONB)
--   * mixer_events (event_type='dose_pump'|'manual_confirmation') -> bitácora de actuadores
-- Esta migración es OPCIONAL y habilita las columnas/tablas dedicadas descritas en el ADD V4.
-- ==============================================================================

-- 1. Columnas dedicadas de alta frecuencia
ALTER TABLE public.control_commands  ADD COLUMN IF NOT EXISTS executed_rtc_ms BIGINT;
ALTER TABLE public.sensor_readings   ADD COLUMN IF NOT EXISTS rtc_timestamp_ms BIGINT;
ALTER TABLE public.motor_telemetry   ADD COLUMN IF NOT EXISTS target_rpm NUMERIC(6,2);

-- 2. Índices para consultas por experimento y por instante del Reloj Maestro
CREATE INDEX IF NOT EXISTS idx_sensor_readings_rtc      ON public.sensor_readings(device_id, rtc_timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_bulk_experiment   ON public.sensor_telemetry_bulk(experiment_id);
CREATE INDEX IF NOT EXISTS idx_odrive_bulk_experiment   ON public.odrive_telemetry_bulk(experiment_id);

-- 3. Eventos de la bomba dosificadora (Planta 1)
CREATE TABLE IF NOT EXISTS public.pump_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('dose_started', 'dose_completed', 'manual_confirmation', 'fault')),
    volume_ml NUMERIC(10,3),
    target_ml NUMERIC(10,3),
    status VARCHAR(50),
    rtc_timestamp_ms BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pump_events_experiment ON public.pump_events(experiment_id, rtc_timestamp_ms DESC);

-- 4. Bitácora de seguridad de anulaciones manuales (Manual Override)
CREATE TABLE IF NOT EXISTS public.manual_overrides_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id UUID REFERENCES public.devices(id) ON DELETE SET NULL,
    experiment_id VARCHAR(50),
    action VARCHAR(50) NOT NULL,
    target VARCHAR(50),
    payload JSONB DEFAULT '{}'::jsonb,
    requested_by VARCHAR(100),
    rtc_timestamp_ms BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_manual_overrides_created ON public.manual_overrides_log(created_at DESC);

-- 5. Estado inicial del orquestador
INSERT INTO public.system_settings (key, value, description)
VALUES (
    'system_state',
    '{"state":"IDLE","since":"1970-01-01T00:00:00Z","experiment_id":null,"override":{"master":false,"pump":false,"mixer":false,"odrive":false},"updated_by":"system"}'::jsonb,
    'Estado global del orquestador AquaControl (IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE)'
)
ON CONFLICT (key) DO NOTHING;

-- 6. Nodo bomba dosificadora (Planta 1)
INSERT INTO public.devices (id, name, type, api_key_hash, location, status, metadata)
VALUES (
    'd0000000-0000-0000-0000-000000000004',
    'Bomba Dosificadora Peristáltica (Planta 1)',
    'motor_thruster',
    encode(digest('ESP32_PUMP_KEY_2026', 'sha256'), 'hex'),
    'Laboratorio / Banco de Pruebas',
    'offline',
    '{"controller_model":"ESP32 + AS5600","actuator":"12V Peristaltic Pump","dosing_unit":"mL"}'::jsonb
)
ON CONFLICT (id) DO NOTHING;

-- 7. RLS de las tablas nuevas (coherente con las bitácoras existentes del banco)
ALTER TABLE public.pump_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manual_overrides_log ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow all on pump_events" ON public.pump_events;
CREATE POLICY "Allow all on pump_events" ON public.pump_events FOR ALL USING (true);

DROP POLICY IF EXISTS "Allow all on manual_overrides_log" ON public.manual_overrides_log;
CREATE POLICY "Allow all on manual_overrides_log" ON public.manual_overrides_log FOR ALL USING (true);
