'use client';

/**
 * Optimistic control state with a hard reconciliation deadline.
 *
 * The console's controls used to be *pure* mirrors of the live snapshot: a switch moved only
 * when the next `drv_rpm` sample arrived, so the operator pressed a toggle and watched nothing
 * happen for up to a second — and forever, for an actuator whose measured value cannot move
 * (an aerator whose ODrive is absent stays at 0 rpm, so its switch could never leave "off").
 * The screen looked broken while the command had in fact been accepted.
 *
 * This hook keeps a short-lived *intent* beside the measured value:
 *
 *   displayed = pending ?? measured
 *
 * `pending` is set the instant a command is issued and cleared the moment either
 *
 *   * the measurement catches up to the intent (the node confirms it), or
 *   * the deadline passes (the node never confirmed — show the truth, do not lie).
 *
 * The deadline is the safety property, and it is an **absolute timestamp**, not a timer that
 * re-arms on every render. A `setTimeout` inside an effect keyed on `measured` would be
 * re-created on every 1 Hz live frame, so an intent the node never confirms would stand in for
 * the truth for as long as the stream kept flowing. Storing `expiresAt` and comparing it with
 * `Date.now()` means the intent dies on schedule regardless of how many frames arrive.
 */

import { useEffect, useRef, useState } from 'react';

export interface Optimistic<T> {
  /** What the UI should draw: the pending intent while it is fresh, else the measurement. */
  value: T;
  /** True while an issued command is still unconfirmed by the node. */
  pending: boolean;
  /** Adopt `next` as the optimistic value until the measurement confirms it or the deadline
   *  passes. */
  set: (next: T) => void;
  /** Drop the intent immediately (e.g. after a failed write). */
  clear: () => void;
}

interface Intent<T> {
  value: T;
  expiresAt: number;
}

/**
 * @param measured  The authoritative value, recomputed each render from the live snapshot.
 * @param matches   Whether a fresh measurement agrees with the intent. Defaults to strict
 *                  equality; pass a predicate for floats ("within 2 rpm of the target").
 * @param ttlMs     How long an unconfirmed intent may stand in for the truth.
 */
export function useOptimistic<T>(
  measured: T,
  matches: (measured: T, intent: T) => boolean = (a, b) => a === b,
  ttlMs = 4_000,
): Optimistic<T> {
  const [intent, setIntent] = useState<Intent<T> | null>(null);
  const matchesRef = useRef(matches);
  matchesRef.current = matches;

  // Reconcile on every render: if the measurement has caught up, hand authority back to it.
  // Done during render (not in an effect) so the confirmed value is drawn on this pass, with
  // no extra frame of a stale intent.
  if (intent !== null && matchesRef.current(measured, intent.value)) {
    setIntent(null);
  }

  // An unconfirmed intent must still disappear on schedule even if no new sample arrives to
  // re-run the render. A single timer armed for the remaining time does that, and it is
  // re-armed only when the intent itself changes — never once per incoming frame.
  useEffect(() => {
    if (intent === null) return;
    const remaining = intent.expiresAt - Date.now();
    if (remaining <= 0) {
      setIntent(null);
      return;
    }
    const timer = setTimeout(() => setIntent(null), remaining);
    return () => clearTimeout(timer);
  }, [intent]);

  return {
    value: intent === null ? measured : intent.value,
    pending: intent !== null,
    set: (next: T) => setIntent({ value: next, expiresAt: Date.now() + ttlMs }),
    clear: () => setIntent(null),
  };
}

/** Convenience: an optimistic boolean switch (on/off). */
export function useOptimisticSwitch(measured: boolean, ttlMs = 4_000): Optimistic<boolean> {
  return useOptimistic<boolean>(measured, (a, b) => a === b, ttlMs);
}

/** Convenience: an optimistic number within a tolerance (for rpm-style setpoints). */
export function useOptimisticNumber(
  measured: number,
  tolerance: number,
  ttlMs = 4_000,
): Optimistic<number> {
  return useOptimistic<number>(measured, (a, b) => Math.abs(a - b) <= tolerance, ttlMs);
}
