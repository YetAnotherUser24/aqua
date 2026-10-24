'use client';

/**
 * Live state subscription.
 *
 * Replaces four polling endpoints per open tab with one long-lived SSE connection. The
 * gateway pushes a full snapshot at 1 Hz, which is the arrangement that makes a Pi 3B+
 * viable: it already holds the latest value per metric, so a refresh costs a memory read
 * and a write to the socket, not a query.
 *
 * Two failure modes are handled explicitly, because both are common and both look like
 * "the dashboard is broken":
 *
 *   1. **A buffering proxy.** If `flush_interval -1` is missing from the Caddy route (or
 *      something else in the path buffers), frames arrive in clumps or not at all. After
 *      repeated stream errors the hook stops trusting SSE and falls back to polling
 *      `GET /api/live`, retrying the stream periodically. Degraded beats dead.
 *   2. **A connected but silent node.** The watchdog compares the age of the last frame,
 *      not the connection state, so a stream that is open but delivering nothing is
 *      reported as `stale` rather than `live`.
 */

import { useEffect, useState } from 'react';
import { getLive } from '@/lib/api';
import type { LiveSnapshot } from '@/lib/types';

export type StreamState = 'connecting' | 'live' | 'stale' | 'polling' | 'offline';

export interface LiveState {
  snapshot: LiveSnapshot | null;
  state: StreamState;
  /** Rolling client-side buffer for sparklines, keyed `${role}.${field}`. */
  spark: Record<string, number[]>;
  frames: number;
  reconnects: number;
  lastFrameAt: number | null;
  error: string | null;
}

/** No frame for this long means the stream is not doing its job. Three missed frames at
 *  1 Hz, with room for a slow tick. */
const STALE_AFTER_MS = 5_000;
const POLL_INTERVAL_MS = 2_000;
const ERRORS_BEFORE_POLL = 3;
/** While polling, re-attempt the stream this often (in poll ticks). */
const POLL_TICKS_BEFORE_RETRY = 15;
const SPARK_POINTS = 120;

export function useLive(enabled = true): LiveState {
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  const [spark, setSpark] = useState<Record<string, number[]>>({});
  const [state, setState] = useState<StreamState>('connecting');
  const [frames, setFrames] = useState(0);
  const [reconnects, setReconnects] = useState(0);
  const [lastFrameAt, setLastFrameAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;

    let disposed = false;
    let source: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let pollTicks = 0;
    let lastFrame = 0;
    let errors = 0;

    const ingest = (next: LiveSnapshot) => {
      if (disposed) return;
      lastFrame = Date.now();
      errors = 0;
      setSnapshot(next);
      setLastFrameAt(lastFrame);
      setFrames((n) => n + 1);
      setState('live');
      setError(null);

      // Sparkline buffer, kept client-side. The gateway's own history ring is not exposed
      // on the live payload, and building it here costs the Pi nothing at all.
      setSpark((prev) => {
        const merged: Record<string, number[]> = { ...prev };
        for (const [role, node] of Object.entries(next.nodes ?? {})) {
          for (const [field, value] of Object.entries(node.metrics ?? {})) {
            if (typeof value !== 'number') continue;
            const key = `${role}.${field}`;
            const series = merged[key] ? merged[key].slice() : [];
            series.push(value);
            if (series.length > SPARK_POINTS) series.splice(0, series.length - SPARK_POINTS);
            merged[key] = series;
          }
        }
        return merged;
      });
    };

    const stopPolling = () => {
      if (pollTimer === null) return;
      clearInterval(pollTimer);
      pollTimer = null;
    };

    const openStream = () => {
      if (disposed) return;
      try {
        source = new EventSource('/api/stream');
      } catch {
        // Constructing EventSource throws only for a malformed URL, but if the browser
        // refuses it there is no stream to fall back from, so go straight to polling.
        startPolling();
        return;
      }

      source.onopen = () => {
        if (disposed) return;
        errors = 0;
      };

      source.onmessage = (event: MessageEvent<string>) => {
        try {
          ingest(JSON.parse(event.data) as LiveSnapshot);
        } catch {
          // One malformed frame is not a reason to tear down a working stream.
        }
      };

      source.onerror = () => {
        if (disposed) return;
        errors += 1;
        setReconnects((n) => n + 1);
        // EventSource retries on its own, honouring the `retry:` hint the gateway sends.
        // Only after repeated failures do we conclude the stream is unusable here.
        if (errors >= ERRORS_BEFORE_POLL) {
          source?.close();
          source = null;
          startPolling();
        }
      };
    };

    function startPolling() {
      if (pollTimer !== null || disposed) return;
      setState((current) => (current === 'offline' ? current : 'polling'));

      const tick = async () => {
        pollTicks += 1;
        try {
          ingest(await getLive());
        } catch (err: unknown) {
          if (disposed) return;
          setState('offline');
          setError(err instanceof Error ? err.message : 'gateway unreachable');
        }

        // A proxy restart or a Pi reboot should not leave the dashboard on the fallback
        // path for the rest of the session.
        if (pollTicks % POLL_TICKS_BEFORE_RETRY === 0) {
          stopPolling();
          errors = 0;
          openStream();
        }
      };

      void tick();
      pollTimer = setInterval(() => void tick(), POLL_INTERVAL_MS);
    }

    openStream();

    const watchdog = setInterval(() => {
      if (disposed || lastFrame === 0 || pollTimer !== null) return;
      if (Date.now() - lastFrame > STALE_AFTER_MS) setState('stale');
    }, 2_000);

    return () => {
      disposed = true;
      source?.close();
      stopPolling();
      clearInterval(watchdog);
    };
  }, [enabled]);

  return { snapshot, state, spark, frames, reconnects, lastFrameAt, error };
}
