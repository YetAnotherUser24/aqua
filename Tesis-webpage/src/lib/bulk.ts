/**
 * Utilidades compartidas para la ingesta Bulk de alta frecuencia (AquaControl V4).
 *
 * Convención de tiempo (ADR-3): `rtc_timestamp_ms` significa **epoch Unix UTC en milisegundos**.
 * Sin embargo, firmwares históricos del OD-Logger emitían "ms desde 2000-01-01" (aprox. 8.4e11 en 2026).
 * Para no romper la compatibilidad, `resolveItemEpochMs` distingue ambos formatos.
 */

/** Máximo de muestras aceptadas por POST bulk (protege la ruta serverless de payloads abusivos). */
export const MAX_BULK_ITEMS = 200;

/** Epoch ms de 2000-01-01T00:00:00Z. */
export const EPOCH_2000_MS = 946684800000;

/** Cualquier timestamp >= 1e12 corresponde a una fecha posterior a 2001-09-09, es decir epoch Unix ms. */
const EPOCH_MS_THRESHOLD = 1e12;

/** Resuelve el instante de una muestra bulk a epoch Unix ms, o null si es indeterminable. */
export function resolveItemEpochMs(item: any): number | null {
  if (!item || typeof item !== 'object') return null;

  if (item.datetime) {
    const parsed = Date.parse(String(item.datetime));
    if (!Number.isNaN(parsed)) return parsed;
  }

  const rtc = Number(item.rtc_timestamp_ms);
  if (Number.isFinite(rtc) && rtc > 0) {
    return rtc >= EPOCH_MS_THRESHOLD ? rtc : rtc + EPOCH_2000_MS;
  }

  const secondsSince2000 = Number(item.seconds_since_2000);
  if (Number.isFinite(secondsSince2000) && secondsSince2000 > 0) {
    return secondsSince2000 * 1000 + EPOCH_2000_MS;
  }

  return null;
}

/** Encabezados de respuesta que impiden cualquier caché de borde (Vercel) o de navegador. */
export const NO_STORE_HEADERS = {
  'Cache-Control': 'no-store',
  'CDN-Cache-Control': 'no-store',
  'Vercel-CDN-Cache-Control': 'no-store'
} as const;
