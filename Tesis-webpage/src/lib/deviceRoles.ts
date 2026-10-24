/**
 * Registro canónico de nodos del banco y sus ROLES (AquaControl).
 *
 * Antes todos los actuadores eran `devices.type = 'motor_thruster'` y el resto del sistema
 * deducía qué nodo era cuál por heurísticas frágiles (buscar "ODrive" en el nombre, comparar
 * `metadata.controller_model`, o simplemente "cualquier nodo motor"). Eso provocó defectos reales:
 *   - `/api/commands/pending` no podía distinguir el ODrive del mixer y un nodo se robaba la orden
 *     dirigida al otro.
 *   - El dashboard identificaba el ODrive por coincidencia de cadena en el nombre.
 *
 * Ahora el ROL es explícito, vive ligado al `device_id` canónico y es lo único que consultan la
 * autorización, el enrutado de comandos y el enrutado de telemetría.
 *
 * Modelo de datos objetivo (ver `supabase/migrations/20261008_separate_actuator_roles.sql`):
 *   - `devices.type` pasa a valores específicos por rol (`aerator_motor`, `mixer`, `dosing_pump`).
 *     El valor legacy `motor_thruster` se conserva para no romper filas antiguas.
 *   - Cada actuador tiene su tabla de telemetría propia.
 */
import { Device, DeviceRole, DeviceType } from '@/types';

export type { DeviceRole, DeviceType };

export interface KnownDevice {
  id: string;
  role: DeviceRole;
  /** Tipo objetivo en `devices.type` una vez aplicada la migración. */
  type: DeviceType;
  name: string;
  location: string;
  metadata: Record<string, any>;
  /** Variables de entorno de Vercel que pueden contener la clave del nodo. */
  envVarKeys: string[];
  /** Claves por defecto usadas por el firmware. */
  defaultTokens: string[];
}

/** Registro canónico: única fuente de verdad de identidad y rol de cada nodo. */
export const KNOWN_DEVICES: KnownDevice[] = [
  {
    id: 'a0000000-0000-0000-0000-000000000001',
    role: 'sensor',
    type: 'sensor_do',
    name: 'Sensor Óptico OD - Estanque 1',
    location: 'Estanque Principal (Zona Norte)',
    metadata: { sensor_model: 'Aqualabo DIGISENS', interface: 'Modbus RS485' },
    envVarKeys: ['ESP32_OD_SENSOR', 'ESP32_SENSOR_DEVICE_KEY'],
    defaultTokens: ['ESP32_OD_SENSOR', 'ESP32_SENSOR_KEY_2026']
  },
  {
    id: 'b0000000-0000-0000-0000-000000000002',
    role: 'odrive',
    type: 'aerator_motor',
    name: 'Controlador ODrive S1 - Estanque 1',
    location: 'Estanque Principal (Zona Central)',
    metadata: { controller_model: 'ODrive S1', interface: 'UART ASCII', control_mode: 'pid' },
    envVarKeys: ['ESP32_ODRIVE', 'ESP32_MOTOR_DEVICE_KEY'],
    defaultTokens: ['ESP32_ODRIVE', 'ESP32_MOTOR_KEY_2026']
  },
  {
    id: 'c0000000-0000-0000-0000-000000000003',
    role: 'mixer',
    type: 'mixer',
    name: 'Aireador Auxiliar ESC (Banco de Pruebas)',
    location: 'Laboratorio / Banco de Pruebas',
    metadata: { controller_model: 'ESP32-S3 ESC PWM', actuator: 'Blue Robotics T200', status: 'auxiliary_backup' },
    envVarKeys: ['ESP32_T_200', 'ESP32_ESC_DEVICE_KEY'],
    defaultTokens: ['ESP32_T_200', 'ESP32_ESC_KEY_2026']
  },
  {
    id: 'd0000000-0000-0000-0000-000000000004',
    role: 'pump',
    type: 'dosing_pump',
    name: 'Bomba Dosificadora Peristáltica (Planta 1)',
    location: 'Laboratorio / Banco de Pruebas',
    metadata: { controller_model: 'ESP32 + AS5600', actuator: '12V Peristaltic Pump', dosing_unit: 'mL' },
    envVarKeys: ['ESP32_PUMP', 'ESP32_PUMP_DEVICE_KEY'],
    defaultTokens: ['ESP32_PUMP', 'ESP32_PUMP_KEY_2026']
  }
];

/** Mapa `device_id` -> rol (enrutado O(1) sin heurísticas). */
export const ROLE_BY_DEVICE_ID: Record<string, DeviceRole> = KNOWN_DEVICES.reduce(
  (acc, d) => ({ ...acc, [d.id]: d.role }),
  {}
);

/** Compatibilidad: los tipos legacy se resuelven por rol, nunca por suposición. */
const LEGACY_TYPE_TO_ROLE: Record<string, DeviceRole> = {
  sensor_do: 'sensor'
};

/**
 * Resuelve el rol de un dispositivo.
 *
 * Orden: `device_id` canónico -> `metadata.role` -> tipo legacy inequívoco.
 * Devuelve `null` cuando el rol es ambiguo. Es deliberado: un `motor_thruster` genérico NO se
 * resuelve a un rol concreto, porque esa ambigüedad es justo la que rompía el enrutado.
 */
export function resolveDeviceRole(device: Partial<Device> | null | undefined): DeviceRole | null {
  if (!device) return null;
  if (device.id && ROLE_BY_DEVICE_ID[device.id]) return ROLE_BY_DEVICE_ID[device.id];
  const metaRole = device.metadata?.role;
  if (metaRole && isDeviceRole(metaRole)) return metaRole;
  const legacy = LEGACY_TYPE_TO_ROLE[String(device.type)];
  return legacy ?? null;
}

export function isDeviceRole(value: unknown): value is DeviceRole {
  return value === 'sensor' || value === 'odrive' || value === 'mixer' || value === 'pump';
}

export function isActuatorRole(role: DeviceRole | null): boolean {
  return role === 'odrive' || role === 'mixer' || role === 'pump';
}

/** Ids canónicos por rol (usado por el orquestador para no repetir UUIDs). */
export const DEVICE_ID_BY_ROLE: Record<DeviceRole, string> = KNOWN_DEVICES.reduce(
  (acc, d) => ({ ...acc, [d.role]: d.id }),
  {} as Record<DeviceRole, string>
);

/**
 * Tabla de telemetría EXCLUSIVA por rol.
 *
 * Cada actuador archiva su serie de alta frecuencia en su propia tabla: mezclarlos en
 * `motor_telemetry` impedía distinguir la dinámica electromecánica del aireador (que es lo que se
 * identifica para el modelo de $K_L a$) de los eventos del mixer o de la dosificación.
 */
export const ROLE_ARCHIVE_TABLE: Record<DeviceRole, string> = {
  sensor: 'sensor_telemetry_bulk',
  odrive: 'odrive_telemetry_bulk',
  mixer: 'mixer_telemetry',
  pump: 'pump_telemetry'
};

/**
 * Tabla legacy compartida con la vista en vivo del dashboard.
 * Se conserva como respaldo mientras la migración de rol no esté aplicada, para no perder datos.
 */
export const LEGACY_DASHBOARD_TABLE: Record<DeviceRole, string> = {
  sensor: 'sensor_readings',
  odrive: 'motor_telemetry',
  mixer: 'motor_telemetry',
  pump: 'motor_telemetry'
};

/** `true` si el error de Supabase/PostgREST indica que la tabla no existe todavía. */
export function isMissingTableError(error: any): boolean {
  if (!error) return false;
  const code = String(error.code || '');
  const msg = String(error.message || '');
  return code === '42P01' || code === 'PGRST205' || /does not exist|schema cache/i.test(msg);
}

/** Ids canónicos como lista (compatibilidad con el difusor del orquestador). */
export const ALL_DEVICE_IDS: string[] = KNOWN_DEVICES.map(d => d.id);
