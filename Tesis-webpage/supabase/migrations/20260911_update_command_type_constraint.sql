-- Migración: Actualización de la tabla control_commands para soporte de comandos de sensor y ensayos
-- Ejecutar en el SQL Editor de Supabase si se desea actualizar las restricciones nativas en base de datos.

-- 1. Agregar columna payload tipo JSONB si no existe
ALTER TABLE public.control_commands ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT '{}'::jsonb;

-- 2. Actualizar la restricción CHECK para permitir 'set_config' y 'set_mode'
ALTER TABLE public.control_commands DROP CONSTRAINT IF EXISTS control_commands_command_type_check;
ALTER TABLE public.control_commands ADD CONSTRAINT control_commands_command_type_check 
  CHECK (command_type IN ('start', 'stop', 'set_speed', 'emergency_stop', 'reboot', 'set_mode', 'set_config'));
