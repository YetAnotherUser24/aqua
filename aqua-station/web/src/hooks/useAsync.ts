'use client';

/**
 * Request/response loading with an explicit reload.
 *
 * Deliberately not a data-fetching library. The dashboard has a handful of reads that are
 * fetched on user action (range change, filter change, refresh) and must show *why* they
 * failed when they do — a cache layer would obscure the one distinction that matters here,
 * between "no data" and "the gateway cannot tell you".
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export interface AsyncState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** False until the first attempt settles, so panels can avoid flashing a skeleton. */
  settled: boolean;
  reload: () => void;
}

export function useAsync<T>(fn: () => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [settled, setSettled] = useState(false);
  const [nonce, setNonce] = useState(0);

  // Keep the latest closure without making it a dependency: re-running on every render
  // because an inline arrow changed identity is the usual way this hook misbehaves.
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    fnRef
      .current()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err);
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        setSettled(true);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, error, loading, settled, reload };
}

/**
 * Re-run a callback on an interval, pausing while the document is hidden.
 *
 * A dashboard left open on a bench laptop overnight should not keep issuing queries for a
 * screen nobody is looking at — on this hardware every avoided read is real.
 */
export function useInterval(callback: () => void, ms: number | null): void {
  const saved = useRef(callback);
  saved.current = callback;

  useEffect(() => {
    if (ms === null) return;
    let timer: ReturnType<typeof setInterval> | null = null;

    const start = () => {
      if (timer !== null) return;
      timer = setInterval(() => saved.current(), ms);
    };
    const stop = () => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    };

    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        saved.current(); // catch up immediately rather than waiting a full interval
        start();
      }
    };

    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [ms]);
}
