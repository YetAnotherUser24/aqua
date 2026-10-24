/**
 * Utilidades de fecha y hora para AquaControl
 * Garantiza la visualización consistente en la zona horaria de Perú (America/Lima / UTC-5)
 */

export const PERU_TIMEZONE = 'America/Lima';

/**
 * Formatea una fecha en formato de hora de Perú: HH:mm (o HH:mm:ss)
 */
export function formatPeruTime(
  dateInput: string | number | Date | null | undefined,
  includeSeconds: boolean = false
): string {
  if (!dateInput) return '--:--';
  const date = typeof dateInput === 'string' || typeof dateInput === 'number' ? new Date(dateInput) : dateInput;
  if (isNaN(date.getTime())) return '--:--';

  return date.toLocaleTimeString('es-PE', {
    timeZone: PERU_TIMEZONE,
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false
  });
}

/**
 * Formatea una fecha en formato corto con fecha y hora de Perú: DD/MM HH:mm
 */
export function formatPeruDateTime(
  dateInput: string | number | Date | null | undefined,
  includeSeconds: boolean = false
): string {
  if (!dateInput) return '--/-- --:--';
  const date = typeof dateInput === 'string' || typeof dateInput === 'number' ? new Date(dateInput) : dateInput;
  if (isNaN(date.getTime())) return '--/-- --:--';

  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: PERU_TIMEZONE,
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: includeSeconds ? '2-digit' : undefined,
    hour12: false
  }).formatToParts(date);

  const getPart = (type: string) => parts.find(p => p.type === type)?.value || '00';
  const time = includeSeconds
    ? `${getPart('hour')}:${getPart('minute')}:${getPart('second')}`
    : `${getPart('hour')}:${getPart('minute')}`;

  return `${getPart('day')}/${getPart('month')} ${time}`;
}

/**
 * Formatea una fecha completa para tooltips o logs: DD/MM/YYYY HH:mm:ss (GMT-5)
 */
export function formatPeruFull(
  dateInput: string | number | Date | null | undefined
): string {
  if (!dateInput) return '--';
  const date = typeof dateInput === 'string' || typeof dateInput === 'number' ? new Date(dateInput) : dateInput;
  if (isNaN(date.getTime())) return '--';

  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: PERU_TIMEZONE,
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).formatToParts(date);

  const getPart = (type: string) => parts.find(p => p.type === type)?.value || '00';
  const dateStr = `${getPart('day')}/${getPart('month')}/${getPart('year')}`;
  const timeStr = `${getPart('hour')}:${getPart('minute')}:${getPart('second')}`;

  return `${dateStr} ${timeStr} (GMT-5)`;
}
