-- ============================================================================
-- AquaControl V5: separación de roles de actuador y tablas de archivo propias
-- ============================================================================
-- MOTIVO
--   `devices.type` usaba la etiqueta legacy `motor_thruster` para el aireador ODrive, el mixer
--   T-200 Y la bomba dosificadora. Al no poder distinguirlos, el sistema deducía qué nodo era cuál
--   con heurísticas (buscar "ODrive" en el nombre, comparar `metadata.controller_model`, o
--   simplemente "cualquier nodo motor"). Eso produjo defectos reales:
--     - `/api/commands/pending` entregaba a un actuador la orden dirigida a OTRO (el mixer se
--       robaba el `set_mode` del ODrive y lo marcaba como enviado).
--     - Toda la telemetría de los tres actuadores caía en la misma tabla `motor_telemetry`.
--   Además las tablas de archivo de V4 nunca se crearon en producción, así que la fidelidad de
--   5 Hz se perdía en silencio y la descarga CSV del experimento salía vacía.
--
-- SEGURIDAD
--   Migración ADITIVA e IDEMPOTENTE: no elimina filas ni columnas, y el valor legacy
--   `motor_thruster` se sigue admitiendo para no romper filas antiguas ni firmware ya flasheado.
--   Se puede re-ejecutar sin efectos secundarios.
--
-- APLICACIÓN
--   Pegar completo en Supabase -> SQL Editor -> Run (o `supabase db push` si hay CLI/credenciales).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. `devices.type`: admitir los tipos específicos por rol conservando el legacy
-- ----------------------------------------------------------------------------
ALTER TABLE public.devices DROP CONSTRAINT IF EXISTS devices_type_check;

ALTER TABLE public.devices
  ADD CONSTRAINT devices_type_check
  CHECK (type IN (
    'sensor_do',
    'aerator_motor',   -- ODrive S1: aireador principal (0-600 RPM, FOC UART)
    'mixer',           -- T-200: agitador auxiliar (PWM 50 Hz)
    'dosing_pump',     -- Bomba peristáltica 12V (Na2SO3)
    'motor_thruster',  -- LEGACY: se conserva por compatibilidad
    'gateway'
  ));

-- ----------------------------------------------------------------------------
-- 2. Re-tipar los actuadores con su rol definitivo
-- ----------------------------------------------------------------------------
UPDATE public.devices SET type = 'aerator_motor' WHERE id = 'b0000000-0000-0000-0000-000000000002';
UPDATE public.devices SET type = 'mixer'         WHERE id = 'c0000000-0000-0000-0000-000000000003';
UPDATE public.devices SET type = 'dosing_pump'   WHERE id = 'd0000000-0000-0000-0000-000000000004';

-- El rol también queda en metadata para que el código pueda resolverlo aunque el `type` no se
-- haya podido migrar (p. ej. firmware antiguo que hace upsert con el tipo legacy).
UPDATE public.devices
   SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('role', 'odrive')
 WHERE id = 'b0000000-0000-0000-0000-000000000002';
UPDATE public.devices
   SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('role', 'mixer')
 WHERE id = 'c0000000-0000-0000-0000-000000000003';
UPDATE public.devices
   SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('role', 'pump')
 WHERE id = 'd0000000-0000-0000-0000-000000000004';

-- ----------------------------------------------------------------------------
-- 3. Tablas de ARCHIVO de alta fidelidad: una por actuador
--    `motor_telemetry` queda como vista en vivo submuestreada a 1 Hz (ADR-6) compartida.
-- ----------------------------------------------------------------------------

-- 3.1 Sensor óptico OD (0.2 Hz)
CREATE TABLE IF NOT EXISTS public.sensor_telemetry_bulk (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3.2 Aireador ODrive S1 (5 Hz) — dinámica electromecánica para identificar KLa
CREATE TABLE IF NOT EXISTS public.odrive_telemetry_bulk (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3.3 Mixer T-200 (PWM): tabla propia, ya no comparte motor_telemetry
CREATE TABLE IF NOT EXISTS public.mixer_telemetry (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3.4 Bomba dosificadora: tabla propia
CREATE TABLE IF NOT EXISTS public.pump_telemetry (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3.5 Columnas que schema.sql declara pero producción NO tenía.
--      `motor_telemetry.rpm` estaba referenciada por schema.sql y por los tres INSERT de telemetría,
--      pero la columna no existía: PostgREST rechaza el INSERT COMPLETO cuando se envía una columna
--      inexistente (PGRST204), así que la telemetría del aireador nunca se guardaba y solo entraban
--      las filas derivadas de los ACK de comandos. El código ya no depende de ella (el dashboard
--      calcula las RPM desde `speed_percent`); se añade para converger con el esquema declarado.
ALTER TABLE public.motor_telemetry ADD COLUMN IF NOT EXISTS rpm NUMERIC(6,2);
ALTER TABLE public.motor_telemetry ADD COLUMN IF NOT EXISTS target_rpm NUMERIC(6,2);
CREATE INDEX IF NOT EXISTS idx_motor_telemetry_rpm ON public.motor_telemetry(device_id, recorded_at DESC) WHERE rpm IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 4. Eventos de actuadores y anulación manual (referenciados por el código V4)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.mixer_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('start_mixer', 'stop_mixer', 'dose_pump', 'manual_confirmation')),
    status VARCHAR(50),
    rtc_timestamp_ms BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.pump_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id VARCHAR(50) NOT NULL,
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN ('dose_pump', 'start_pump', 'stop_pump', 'manual_confirmation')),
    volume_ml NUMERIC(10,3),
    status VARCHAR(50),
    rtc_timestamp_ms BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Bitácora de seguridad de comandos directos y paradas de emergencia
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

-- ----------------------------------------------------------------------------
-- 5. Índices para las consultas de descarga por experimento
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_sensor_bulk_experiment ON public.sensor_telemetry_bulk(experiment_id);
CREATE INDEX IF NOT EXISTS idx_odrive_bulk_experiment ON public.odrive_telemetry_bulk(experiment_id);
CREATE INDEX IF NOT EXISTS idx_mixer_telemetry_experiment ON public.mixer_telemetry(experiment_id);
CREATE INDEX IF NOT EXISTS idx_pump_telemetry_experiment ON public.pump_telemetry(experiment_id);
CREATE INDEX IF NOT EXISTS idx_mixer_events_experiment ON public.mixer_events(experiment_id);
CREATE INDEX IF NOT EXISTS idx_pump_events_experiment ON public.pump_events(experiment_id);
CREATE INDEX IF NOT EXISTS idx_overrides_log_created ON public.manual_overrides_log(created_at DESC);

-- ----------------------------------------------------------------------------
-- 6. RLS: mismo criterio permisivo que el resto del esquema del banco
-- ----------------------------------------------------------------------------
ALTER TABLE public.sensor_telemetry_bulk ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.odrive_telemetry_bulk ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mixer_telemetry       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pump_telemetry        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mixer_events          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pump_events           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manual_overrides_log  ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'sensor_telemetry_bulk', 'odrive_telemetry_bulk', 'mixer_telemetry', 'pump_telemetry',
    'mixer_events', 'pump_events', 'manual_overrides_log'
  ]
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', 'Permitir full en ' || t, t);
    EXECUTE format(
      'CREATE POLICY %I ON public.%I FOR ALL USING (true) WITH CHECK (true)',
      'Permitir full en ' || t, t
    );
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- 7. Verificación (debe devolver 7 filas con created=true)
-- ----------------------------------------------------------------------------
-- SELECT tablename FROM pg_tables
--  WHERE schemaname = 'public'
--    AND tablename IN ('sensor_telemetry_bulk','odrive_telemetry_bulk','mixer_telemetry',
--                      'pump_telemetry','mixer_events','pump_events','manual_overrides_log');
