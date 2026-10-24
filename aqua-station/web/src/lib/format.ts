/**
 * Value formatting, driven by the schema registry.
 *
 * The point of routing every number through here is that precision is declared once, in
 * `roles.yaml`, and the UI obeys it. Hardcoding `.toFixed(2)` at each call site is how a
 * display ends up claiming two decimal places of a value the sensor only resolved to one.
 */

import type { FieldDef, MetricValue } from './types';

/** `null` is rendered as an em dash, never as `0`. A missing reading and a reading of
 *  zero are different claims, and the old schema conflated them. */
export const ABSENT = '—';

export function formatNumber(
  value: number | null | undefined,
  precision: number | null | undefined,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return ABSENT;
  if (precision === null || precision === undefined) return String(value);
  return value.toFixed(precision);
}

/** Format a wire value using its declared field definition. */
export function formatField(value: MetricValue, field: FieldDef | undefined): string {
  if (value === null || value === undefined) return ABSENT;

  if (typeof value === 'boolean') return value ? 'ON' : 'OFF';
  if (typeof value === 'string') return value;

  return formatNumber(value, field?.precision ?? null);
}

/** `7.874 mg/L`, or just `7.874` when the field declares no unit. */
export function formatWithUnit(value: MetricValue, field: FieldDef | undefined): string {
  const text = formatField(value, field);
  if (text === ABSENT || !field?.unit) return text;
  return `${text} ${field.unit}`;
}

/** Split so the unit can be styled separately: `{ value: "7.874", unit: "mg/L" }`. */
export function splitWithUnit(
  value: MetricValue,
  field: FieldDef | undefined,
): { value: string; unit: string | null } {
  const text = formatField(value, field);
  return { value: text, unit: text === ABSENT ? null : (field?.unit ?? null) };
}

/** Compact durations: `4.2 s`, `3.5 min`, `2.1 h`, `3.4 d`. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return ABSENT;
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${minutes.toFixed(1)} min`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} d`;
}

/**
 * A run timer: `HH:MM:SS`, zero-padded, never negative.
 *
 * Separate from :func:`formatDuration` because the two answer different questions. A
 * duration wants a compact human unit ("2.1 h"); a run clock wants a stopwatch reading that
 * ticks uniformly, so a glance at it tells you the run is still advancing. Negative input is
 * clamped to zero rather than rendered as `-00:00:01`, which would look like a countdown.
 */
export function formatDurationClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '--:--:--';
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/**
 * A relative-time axis label: `+MM:SS`, or `+H:MM:SS` past an hour.
 *
 * The leading `+` is deliberate: it marks the value as elapsed time, not a wall clock, so a
 * chart axis cannot be mistaken for one.
 */
export function formatRelativeClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '--:--';
  const isNeg = seconds < 0;
  const total = Math.floor(Math.abs(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const sign = isNeg ? '-' : '+';
  if (h > 0) return `${sign}${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${sign}${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** `age_s` as an operator reads it: how long since we last heard from the node. */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'never';
  if (seconds < 1) return 'now';
  if (seconds < 60) return `${seconds.toFixed(0)} s ago`;
  return `${formatDuration(seconds)} ago`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return ABSENT;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/** Percentages where a fraction (0..1) is the input, as `uncertain_frac` is. */
export function formatFractionAsPercent(fraction: number | null | undefined, digits = 1): string {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) return ABSENT;
  return `${(fraction * 100).toFixed(digits)}%`;
}

/** RSSI in dBm, with a qualitative label. Signal *stability* matters more than strength
 *  for ESP-NOW, but an operator still needs to know when a node is marginal. */
export function describeRssi(rssi: number | null | undefined): {
  label: string;
  severity: 'ok' | 'warn' | 'bad';
} {
  if (rssi === null || rssi === undefined) return { label: ABSENT, severity: 'warn' };
  if (rssi >= -70) return { label: `${rssi} dBm`, severity: 'ok' };
  if (rssi >= -85) return { label: `${rssi} dBm`, severity: 'warn' };
  return { label: `${rssi} dBm`, severity: 'bad' };
}

/** `pump_dosed_ml` -> `Dosed`. Small helper for turning field names into labels when the
 *  registry supplies no display name. */
export function humaniseField(name: string): string {
  return name
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}
