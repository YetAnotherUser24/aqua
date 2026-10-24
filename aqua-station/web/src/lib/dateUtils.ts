/**
 * Date and time helpers.
 *
 * The deployment runs in America/Lima and the operators read the dashboard in
 * America/Lima, but the browser's local timezone is whatever laptop happens to be
 * connected. So every timestamp is rendered in an explicit zone rather than the ambient
 * one — otherwise a laptop left on UTC silently disagrees with the CSV export by five
 * hours, and the disagreement is invisible because the labels look fine.
 *
 * The gateway always sends ISO 8601 with an offset, so what arrives is unambiguous. It is
 * only the *display* zone that needs pinning.
 */

export const PROJECT_TIMEZONE = 'America/Lima';
export const PROJECT_TZ_LABEL = 'GMT-5';

function toDate(input: string | number | Date | null | undefined): Date | null {
  if (input === null || input === undefined || input === '') return null;
  const date = input instanceof Date ? input : new Date(input);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `14:32` or `14:32:07`, in project time. */
export function formatTime(
  input: string | number | Date | null | undefined,
  withSeconds = false,
): string {
  const date = toDate(input);
  if (!date) return '--:--';
  return date.toLocaleTimeString('en-GB', {
    timeZone: PROJECT_TIMEZONE,
    hour: '2-digit',
    minute: '2-digit',
    second: withSeconds ? '2-digit' : undefined,
    hour12: false,
  });
}

/** `18:25:10.120` in project time — the master RTC clock in the header, milliseconds included
 *  so the operator can see it is a live clock and not a frozen last-frame stamp. */
export function formatRtcClock(input: string | number | Date | null | undefined): string {
  const date = toDate(input);
  if (!date) return '--:--:--.---';
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: PROJECT_TIMEZONE,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00';
  const ms = String(date.getMilliseconds()).padStart(3, '0');
  return `${get('hour')}:${get('minute')}:${get('second')}.${ms}`;
}

/** `09/10 14:32` in project time. */
export function formatDateTime(
  input: string | number | Date | null | undefined,
  withSeconds = false,
): string {
  const date = toDate(input);
  if (!date) return '--/-- --:--';
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: PROJECT_TIMEZONE,
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: withSeconds ? '2-digit' : undefined,
    hour12: false,
  }).formatToParts(date);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00';
  const time = withSeconds
    ? `${get('hour')}:${get('minute')}:${get('second')}`
    : `${get('hour')}:${get('minute')}`;
  return `${get('day')}/${get('month')} ${time}`;
}

/** `09/10/2026 14:32:07 (GMT-5)` — for tooltips and log lines. */
export function formatFull(input: string | number | Date | null | undefined): string {
  const date = toDate(input);
  if (!date) return '--';
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: PROJECT_TIMEZONE,
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00';
  return (
    `${get('day')}/${get('month')}/${get('year')} ` +
    `${get('hour')}:${get('minute')}:${get('second')} (${PROJECT_TZ_LABEL})`
  );
}

/** Epoch milliseconds -> `HH:mm` in project time. Used for chart axis ticks. */
export function epochMsToClock(ms: number): string {
  return formatTime(new Date(ms));
}

/**
 * Device clock offset, phrased as latency rather than error.
 *
 * A few hundred milliseconds is normal message transit. Seconds mean the node's clock is
 * wrong, which matters because it decides which minute bucket a sample lands in.
 */
export function describeClockOffset(offsetMs: number | null | undefined): {
  label: string;
  severity: 'ok' | 'warn' | 'bad';
} {
  if (offsetMs === null || offsetMs === undefined) {
    return { label: 'no offset yet', severity: 'warn' };
  }
  const abs = Math.abs(offsetMs);
  const label =
    abs < 1000
      ? `${offsetMs >= 0 ? '+' : ''}${offsetMs} ms`
      : `${(offsetMs / 1000).toFixed(1)} s`;
  if (abs < 2000) return { label, severity: 'ok' };
  if (abs < 10_000) return { label, severity: 'warn' };
  return { label, severity: 'bad' };
}
