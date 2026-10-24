/**
 * Chart utilities for downsampling and processing telemetry series.
 */

export interface RawDataPoint {
  bucket: string;
  value: number | null;
}

export interface TimedPoint {
  ms: number;
  val: number;
}

/**
 * Resamples a high-frequency series (e.g. 5 Hz aerator RPM) down to a maximum
 * rate of 1.0 Hz (1 sample per 1000 ms), computing the average of all valid
 * readings within each 1-second window.
 */
export function resampleTo1Hz(samples: RawDataPoint[], maxVal?: number): TimedPoint[] {
  if (!samples || samples.length === 0) return [];

  const result: TimedPoint[] = [];
  let currentSec = -1;
  let sum = 0;
  let count = 0;

  for (let i = 0; i < samples.length; i++) {
    const s = samples[i];
    if (s.value === null || s.value === undefined) continue;
    if (maxVal !== undefined && s.value > maxVal) continue;

    const ms = new Date(s.bucket).getTime();
    if (isNaN(ms)) continue;

    const sec = Math.floor(ms / 1000) * 1000;

    if (sec !== currentSec) {
      if (count > 0) {
        result.push({ ms: currentSec, val: sum / count });
      }
      currentSec = sec;
      sum = s.value;
      count = 1;
    } else {
      sum += s.value;
      count++;
    }
  }

  if (count > 0) {
    result.push({ ms: currentSec, val: sum / count });
  }

  return result;
}

