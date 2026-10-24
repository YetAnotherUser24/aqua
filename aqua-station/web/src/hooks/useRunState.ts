'use client';

/**
 * The run state, and the transitions against it.
 *
 * A thin wrapper rather than a store: one `GET /api/state` on mount, a re-read after every
 * transition, and a slow poll as a safety net for a change made from another tab. Nothing is
 * cached or optimistically applied — the gateway is the single writer, so the screen shows
 * what it says, not what we hoped it would say.
 *
 * The poll is deliberately slow (5 s). The state changes only when someone presses a button,
 * and every button re-reads immediately, so the poll exists purely to catch a second operator.
 * Polling is paused while the document is hidden, like every other interval here.
 */

import { useCallback, useState } from 'react';
import {
  confirmPhase as apiConfirmPhase,
  getState,
  pauseRun as apiPauseRun,
  postState,
  resumeRun as apiResumeRun,
  setPhaseMode as apiSetPhaseMode,
  triggerPhaseAction as apiTriggerPhaseAction,
} from '@/lib/api';
import type { PhaseMode, RunState, RunStateResponse } from '@/lib/types';
import { useAsync, useInterval } from './useAsync';

export interface RunStateApi {
  state: RunStateResponse | null;
  error: unknown;
  loading: boolean;
  /** True while a transition is in flight, so buttons can disable rather than double-send. */
  busy: boolean;
  reload: () => void;
  start: (experimentId: string) => Promise<void>;
  stop: () => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  confirm: () => Promise<void>;
  /** Fire the active MANUAL phase's held entry commands ("Disparar Acción"). */
  triggerAction: (params?: Record<string, unknown>) => Promise<void>;
  /**
   * Set a phase's mode. Without `experimentId` this targets the running experiment; passing
   * one persists the choice for an experiment that has not launched yet.
   */
  setMode: (phaseId: string, mode: PhaseMode, experimentId?: string | null) => Promise<void>;
  /** Global state transition from the header's Modo selector (ACTIVE_EXPERIMENT | IDLE |
   *  MANUAL_OVERRIDE). Distinct from start/stop because it can also request the override. */
  setState: (state: RunState) => Promise<void>;
}

export function useRunState(pollMs = 5_000): RunStateApi {
  const { data, error, loading, reload } = useAsync(() => getState(), []);
  const [busy, setBusy] = useState(false);

  useInterval(reload, pollMs);

  /** Run a transition, then re-read. Errors are surfaced by the caller's `error` field only
   *  if the read fails; a transition failure is swallowed here because the follow-up read
   *  reconciles the screen either way, and a toast for every refused command would be noise. */
  const transition = useCallback(
    async (fn: () => Promise<RunStateResponse>) => {
      setBusy(true);
      try {
        await fn();
      } catch {
        /* the re-read below shows the true state regardless */
      } finally {
        setBusy(false);
        reload();
      }
    },
    [reload],
  );

  const start = useCallback(
    (experimentId: string) => transition(() => postState('ACTIVE_EXPERIMENT', experimentId)),
    [transition],
  );
  const stop = useCallback(() => transition(() => postState('IDLE', null)), [transition]);
  const pause = useCallback(() => transition(() => apiPauseRun()), [transition]);
  const resume = useCallback(() => transition(() => apiResumeRun()), [transition]);
  const confirm = useCallback(() => transition(() => apiConfirmPhase()), [transition]);
  const triggerAction = useCallback(
    (params?: Record<string, unknown>) => transition(() => apiTriggerPhaseAction(params)),
    [transition],
  );
  const setMode = useCallback(
    (phaseId: string, mode: PhaseMode, experimentId?: string | null) =>
      transition(() => apiSetPhaseMode(phaseId, mode, experimentId)),
    [transition],
  );
  const setState = useCallback(
    (state: RunState) => transition(() => postState(state, null)),
    [transition],
  );

  return {
    state: data,
    error,
    loading,
    busy,
    reload,
    start,
    stop,
    pause,
    resume,
    confirm,
    triggerAction,
    setMode,
    setState,
  };
}

/** Convenience: the coarse state as a `RunState`, defaulting to IDLE before the first read. */
export function runStateOf(state: RunStateResponse | null): RunState {
  return state?.state ?? 'IDLE';
}
