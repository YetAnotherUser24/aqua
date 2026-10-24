import { NextRequest, NextResponse } from 'next/server';
import { supabaseAdmin, hashApiKey, isSupabaseConfigured } from '@/lib/supabase';
import { Device, DeviceRole } from '@/types';
import { KNOWN_DEVICES, KnownDevice, resolveDeviceRole } from '@/lib/deviceRoles';

function toDevice(cfg: KnownDevice): Device {
  return {
    id: cfg.id,
    name: cfg.name,
    type: cfg.type,
    role: cfg.role,
    location: cfg.location,
    status: 'online',
    last_seen_at: new Date().toISOString(),
    metadata: cfg.metadata,
    created_at: new Date().toISOString()
  };
}

function resolveKnownDevice(deviceKey: string): Device | null {
  for (const cfg of KNOWN_DEVICES) {
    // 1. Coincidencia directa con tokens por defecto o alias conocidos
    if (cfg.defaultTokens.includes(deviceKey)) {
      return toDevice(cfg);
    }

    // 2. Coincidencia con variables de entorno (e.g. configuradas en Vercel)
    for (const envKey of cfg.envVarKeys) {
      const envVal = process.env[envKey];
      if (envVal && (envVal === deviceKey || envKey === deviceKey)) {
        return toDevice(cfg);
      }
    }
  }
  return null;
}

/**
 * Autoriza una petición de dispositivo (`X-Device-Key`).
 *
 * Prefiere `expectedRole` sobre `expectedType`: el rol identifica al nodo sin ambigüedad, mientras
 * que `motor_thruster` agrupaba a tres actuadores distintos. `expectedType` se mantiene para las
 * rutas antiguas.
 */
export async function authenticateDevice(
  req: NextRequest,
  expectedType?: string,
  expectedRole?: DeviceRole
): Promise<{ device: Device | null; role: DeviceRole | null; errorResponse: NextResponse | null }> {
  const deviceKey = req.headers.get('x-device-key') || req.headers.get('X-Device-Key') || req.nextUrl?.searchParams?.get('device_key') || null;

  if (!deviceKey) {
    return {
      device: null,
      role: null,
      errorResponse: NextResponse.json(
        { error: 'Encabezado X-Device-Key faltante' },
        { status: 401 }
      )
    };
  }

  // Nivel 1: Verificar contra dispositivos conocidos y variables de entorno de Vercel
  const knownDevice = resolveKnownDevice(deviceKey);
  if (knownDevice) {
    const role = resolveDeviceRole(knownDevice);

    // El rol manda: es inequívoco y no depende de la migración de `devices.type`.
    if (expectedRole && role !== expectedRole) {
      return {
        device: null,
        role: null,
        errorResponse: NextResponse.json(
          { error: `Dispositivo '${knownDevice.name}' (rol: ${role ?? 'desconocido'}) no autorizado para endpoints de rol '${expectedRole}'` },
          { status: 403 }
        )
      };
    }

    // Compatibilidad con rutas antiguas: acepta tanto el tipo específico como el legacy.
    if (expectedType && !typeSatisfiesExpected(knownDevice, expectedType)) {
      return {
        device: null,
        role: null,
        errorResponse: NextResponse.json(
          { error: `Dispositivo '${knownDevice.name}' no autorizado para endpoints de tipo '${expectedType}'` },
          { status: 403 }
        )
      };
    }

    // Si Supabase está enlazado en producción, sincronizar dispositivo (upsert transparente)
    // Esto garantiza que el device_id exista para claves foráneas sin requerir SQL manual.
    // El ROL se persiste también en metadata: `resolveDeviceRole()` lo usa como respaldo si el
    // `devices.type` no se pudo migrar, y este upsert es quien lo sobrescribe en cada arranque.
    if (isSupabaseConfigured()) {
      const metadataWithRole = { ...knownDevice.metadata, role: knownDevice.role };
      try {
        const upsert = (type: string) => supabaseAdmin.from('devices').upsert({
          id: knownDevice.id,
          name: knownDevice.name,
          type,
          api_key_hash: hashApiKey(deviceKey),
          location: knownDevice.location,
          status: 'online',
          last_seen_at: new Date().toISOString(),
          metadata: metadataWithRole
        }, { onConflict: 'id' });

        const first = await upsert(knownDevice.type);
        // Reintento con el tipo legacy si el CHECK de producción aún no admite los tipos por rol.
        if (first.error) await upsert('motor_thruster');
      } catch (upsertErr) {
        console.warn('Advertencia al sincronizar dispositivo en Supabase:', upsertErr);
      }
    }

    return { device: knownDevice, role, errorResponse: null };
  }

  // Si Supabase no está configurado y no coincidió con ninguna clave conocida
  if (!isSupabaseConfigured()) {
    return {
      device: null,
      role: null,
      errorResponse: NextResponse.json(
        { error: 'Clave de dispositivo no reconocida en modo local' },
        { status: 403 }
      )
    };
  }

  // Nivel 2: Búsqueda dinámica en Supabase mediante hash SHA-256
  const keyHash = hashApiKey(deviceKey);

  try {
    let query = supabaseAdmin
      .from('devices')
      .select('*')
      .eq('api_key_hash', keyHash);

    if (expectedType && expectedRole === undefined) {
      query = query.eq('type', expectedType);
    }

    const { data: devices, error } = await query.limit(1);

    if (error || !devices || devices.length === 0) {
      return {
        device: null,
        role: null,
        errorResponse: NextResponse.json(
          { error: 'Clave de dispositivo inválida o no autorizada' },
          { status: 403 }
        )
      };
    }

    const device = devices[0] as Device;
    const role = resolveDeviceRole(device);

    if (expectedRole && role !== expectedRole) {
      return {
        device: null,
        role: null,
        errorResponse: NextResponse.json(
          { error: `Dispositivo no autorizado para endpoints de rol '${expectedRole}'` },
          { status: 403 }
        )
      };
    }

    // Actualizar último contacto y estado del dispositivo
    await supabaseAdmin
      .from('devices')
      .update({
        last_seen_at: new Date().toISOString(),
        status: 'online',
        updated_at: new Date().toISOString()
      })
      .eq('id', device.id);

    return { device, role, errorResponse: null };
  } catch (err: any) {
    console.error('Error en autenticación de dispositivo:', err);
    return {
      device: null,
      role: null,
      errorResponse: NextResponse.json(
        { error: 'Error interno en verificación de seguridad' },
        { status: 500 }
      )
    };
  }
}

/**
 * Un nodo satisface el tipo esperado si su tipo específico coincide, o si el tipo esperado es el
 * legacy `motor_thruster` y el nodo es un actuador (que es lo que esa etiqueta significaba).
 */
function typeSatisfiesExpected(device: Device, expectedType: string): boolean {
  if (device.type === expectedType) return true;
  const role = resolveDeviceRole(device);
  if (expectedType === 'motor_thruster') return role !== null && role !== 'sensor';
  if (expectedType === 'sensor_do') return role === 'sensor' || device.type === 'sensor_do';
  return false;
}

