/**
 * Typed client for the gateway.
 *
 * One rule runs through this file: a failed request and an empty result are different
 * answers and must stay distinguishable. The gateway enforces that deliberately — it
 * returns 503 with "event stream unavailable: database offline" rather than `[]`, because
 * an empty array is a claim that nothing happened.
 *
 * So nothing here swallows an error into a default. Callers get either data or an
 * `ApiError`, and the UI decides what to show.
 *
 * Every path is relative. In production Caddy serves this app and proxies /api on the same
 * origin, so there is no base URL and no CORS.
 */

import type {
  AquaEvent,
  CommandRecord,
  EventTypesResponse,
  Experiment,
  ExportReport,
  ExportsResponse,
  HealthResponse,
  HistoryResponse,
  LevelsResponse,
  LiveSnapshot,
  MilestonePayload,
  MilestonesResponse,
  NativeExportReport,
  PhaseMode,
  RecipesResponse,
  Registry,
  RunState,
  RunStateResponse,
  SamplesResponse,
  Severity,
} from './types';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
    this.name = 'ApiError';
  }
}

/** The gateway exists but cannot answer right now (database offline, etc.). The right
 *  response is "degraded", not "empty". */
export function isUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503;
}

/** No gateway at all — wrong host, Caddy down, gateway not started. */
export function isUnreachable(error: unknown): boolean {
  return error instanceof TypeError;
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return `HTTP ${error.status}: ${error.detail}`;
  if (isUnreachable(error)) return 'gateway unreachable';
  if (error instanceof Error) return error.message;
  return String(error);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    cache: 'no-store',
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    // FastAPI puts the reason in `detail`. Fall back to the status text so a proxy error
    // page (which is not JSON) still produces a usable message.
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (body.detail !== undefined) detail = JSON.stringify(body.detail);
    } catch {
      /* not JSON — keep statusText */
    }
    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

export function getRegistry(): Promise<Registry> {
  return request<Registry>('/api/roles');
}

export function getLive(): Promise<LiveSnapshot> {
  return request<LiveSnapshot>('/api/live');
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/healthz');
}

export function getLevels(): Promise<LevelsResponse> {
  return request<LevelsResponse>('/api/events/levels');
}

export function getEventTypes(): Promise<EventTypesResponse> {
  return request<EventTypesResponse>('/api/events/types');
}

export interface EventQuery {
  experiment_id?: string;
  node_id?: string;
  event_type?: string;
  severity?: Severity;
  scope?: string;
  limit?: number;
}

export function getEvents(query: EventQuery = {}): Promise<{ events: AquaEvent[] }> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  }
  const suffix = params.toString();
  return request<{ events: AquaEvent[] }>(`/api/events${suffix ? `?${suffix}` : ''}`);
}

export function getHistory(role: string, metric: string, range: string): Promise<HistoryResponse> {
  const params = new URLSearchParams({ role, metric, range });
  return request<HistoryResponse>(`/api/history?${params.toString()}`);
}

/**
 * Raw samples for a window: every point, not one per minute.
 *
 * Separate from :func:`getHistory` rather than a flag on it because the two answer from
 * different tiers and can legitimately disagree — the rollup is a summary, this is the series.
 * `limit` is left to the server's default unless a caller has a reason, and the response says
 * whether it was hit.
 */
export function getSamples(
  role: string,
  metric: string,
  range: string,
  limit?: number,
): Promise<SamplesResponse> {
  const params = new URLSearchParams({ role, metric, range });
  if (limit !== undefined) params.set('limit', String(limit));
  return request<SamplesResponse>(`/api/samples?${params.toString()}`);
}

export function getExperiments(limit = 50): Promise<{ experiments: Experiment[] }> {
  return request<{ experiments: Experiment[] }>(`/api/experiments?limit=${limit}`);
}

export interface ExperimentPreview {
  experiment_id: string;
  title: string;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  points: { t: string; t_rel_s: number; do: number }[];
  count: number;
  min_do: number | null;
  max_do: number | null;
  duration_s: number;
}

export function getExperimentPreview(experimentId: string): Promise<ExperimentPreview> {
  return request<ExperimentPreview>(`/api/experiments/${encodeURIComponent(experimentId)}/preview`);
}

/**
 * Create an experiment for a recipe, so "Iniciar Ensayo" can start a fresh run without the
 * operator first hunting for an existing experiment id. Returns the new row.
 */
export function createExperiment(body: {
  title: string;
  roles: string[];
  recipe_id: string;
  hypothesis?: string;
  notes?: string;
  /** Per-phase modes chosen before launch, keyed by `phase_id`. Persisted to the phase rows
   *  so the run starts from the operator's plan rather than the recipe default. */
  phase_modes?: Record<string, string>;
}): Promise<Experiment> {
  return request<Experiment>('/api/experiments', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export function getCommands(role?: string, limit = 50): Promise<{ commands: CommandRecord[] }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (role) params.set('role', role);
  return request<{ commands: CommandRecord[] }>(`/api/commands?${params.toString()}`);
}

// ---------------------------------------------------------------------------
// Writes
// ---------------------------------------------------------------------------

export interface CommandRequest {
  role: string;
  action: string;
  target?: Record<string, unknown>;
  experiment_id?: string | null;
  /** A command must not outlive its relevance: a node waking from deep sleep after a day
   *  has to refuse rather than act on a stale setpoint. */
  deadline_s?: number;
  requested_by?: string;
}

export function postCommand(body: CommandRequest): Promise<CommandRecord> {
  return request<CommandRecord>('/api/commands', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export function exportExperiment(
  experimentId: string,
  options: { gridSeconds?: number; compress?: boolean } = {},
): Promise<ExportReport> {
  const params = new URLSearchParams();
  if (options.gridSeconds !== undefined) params.set('grid_seconds', String(options.gridSeconds));
  if (options.compress !== undefined) params.set('compress', String(options.compress));
  const suffix = params.toString();
  return request<ExportReport>(
    `/api/experiments/${encodeURIComponent(experimentId)}/export${suffix ? `?${suffix}` : ''}`,
    { method: 'POST' },
  );
}

export function rotateArchive(): Promise<{ today: string; rotated: string[]; finalised: string[] }> {
  return request('/api/archive/rotate', { method: 'POST' });
}

// ---------------------------------------------------------------------------
// Orchestration (WS1) — the run state machine and the recipe catalogue
// ---------------------------------------------------------------------------

export function getState(): Promise<RunStateResponse> {
  return request<RunStateResponse>('/api/state');
}

/**
 * Request an orchestrator transition.
 *
 * `ACTIVE_EXPERIMENT` starts (or resumes) a run and needs an `experiment_id`;
 * `IDLE` stops one; `MANUAL_OVERRIDE` suspends sequencing without stopping the loop.
 * Pause and resume are their own routes because they are orthogonal to the state.
 */
export function postState(
  state: RunState,
  experimentId?: string | null,
): Promise<RunStateResponse> {
  return request<RunStateResponse>('/api/state', {
    method: 'POST',
    body: JSON.stringify({ state, experiment_id: experimentId ?? null }),
  });
}

/** Pause: hold the timer and stop phase advancement. Does not disarm the actuators. */
export function pauseRun(): Promise<RunStateResponse> {
  return request<RunStateResponse>('/api/state/pause', { method: 'POST' });
}

/** Resume a paused run; the timer excludes the paused span. */
export function resumeRun(): Promise<RunStateResponse> {
  return request<RunStateResponse>('/api/state/resume', { method: 'POST' });
}

/** Operator's visto bueno on a HIBRIDO phase that is waiting for it. */
export function confirmPhase(): Promise<RunStateResponse> {
  return request<RunStateResponse>('/api/state/confirm', { method: 'POST' });
}

/** Fire the active MANUAL phase's held entry commands ("Disparar Acción"). Optionally overrides parameters. */
export function triggerPhaseAction(params?: Record<string, unknown>): Promise<RunStateResponse> {
  return request<RunStateResponse>('/api/state/phase-action', {
    method: 'POST',
    body: params ? JSON.stringify({ params }) : undefined,
    headers: params ? { 'Content-Type': 'application/json' } : undefined,
  });
}

/** Override parameters for a phase (e.g. mixer_rpm, step_rpm, dose_ml). */
export function setPhaseParams(
  phaseId: string,
  params: Record<string, unknown>,
): Promise<RunStateResponse> {
  return request<RunStateResponse>('/api/state/phase-params', {
    method: 'POST',
    body: JSON.stringify({ phase_id: phaseId, params }),
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Override one phase's mode.
 *
 * With no `experimentId`, this targets the running experiment. Passing one prepares a plan
 * *before* launch: the mode is persisted per experiment and the orchestrator seeds the run
 * from it, so a pre-launch choice is honoured rather than reset when the run begins.
 */
export function setPhaseMode(
  phaseId: string,
  mode: PhaseMode,
  experimentId?: string | null,
): Promise<RunStateResponse> {
  const params = new URLSearchParams({ phase_id: phaseId, mode });
  if (experimentId) params.set('experiment_id', experimentId);
  return request<RunStateResponse>(`/api/state/phase-mode?${params.toString()}`, { method: 'POST' });
}

// ---------------------------------------------------------------------------
// DO Closed-Loop Control (Manual & Tuning)
// ---------------------------------------------------------------------------

export interface DOControlParams {
  setpoint_mg_l: number;
  kp?: number;
  ki?: number;
  kd?: number;
  beta?: number;
  tt?: number;
  tau_lag?: number;
  mode?: 'pid' | 'fuzzy';
  experiment_id?: string | null;
  requested_by?: string;
}

export function postControlDO(params: DOControlParams): Promise<Record<string, unknown>> {
  return request('/api/control/do', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

export function postControlDOStop(requested_by = 'operator:web'): Promise<Record<string, unknown>> {
  const p = new URLSearchParams({ requested_by });
  return request(`/api/control/do/stop?${p.toString()}`, {
    method: 'POST',
  });
}

export function getControlDO(): Promise<{
  defaults: {
    setpoint_mg_l: number;
    kp: number;
    ki: number;
    kd: number;
    beta: number;
    tt: number;
    tau_lag: number;
    mode: string;
  };
  current: {
    do_mg_l: number | null;
    rpm: number | null;
    ctl_mode: string;
    running: boolean;
    failsafe: boolean;
  };
}> {
  return request('/api/control/do');
}

export function getRecipes(): Promise<RecipesResponse> {
  return request<RecipesResponse>('/api/recipes');
}

// ---------------------------------------------------------------------------
// Milestones (WS2) — an operator's documented point on the run timeline
// ---------------------------------------------------------------------------

export function getMilestones(experimentId: string): Promise<MilestonesResponse> {
  return request<MilestonesResponse>(
    `/api/experiments/${encodeURIComponent(experimentId)}/milestones`,
  );
}

export function postMilestone(
  experimentId: string,
  body: MilestonePayload,
): Promise<Record<string, unknown>> {
  return request(`/api/experiments/${encodeURIComponent(experimentId)}/milestones`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/**
 * Build the native-rate export — every sample as recorded, plus events.
 *
 * Distinct from `exportExperiment` (the aligned bundle): this keeps the raw cadence. It is
 * the call behind the "full fidelity CSV" download.
 */
export function exportExperimentNative(
  experimentId: string,
  options: { compress?: boolean } = {},
): Promise<NativeExportReport> {
  const params = new URLSearchParams();
  if (options.compress !== undefined) params.set('compress', String(options.compress));
  const suffix = params.toString();
  return request<NativeExportReport>(
    `/api/experiments/${encodeURIComponent(experimentId)}/export/native${suffix ? `?${suffix}` : ''}`,
    { method: 'POST' },
  );
}

/** Exports already on disk for an experiment, newest first. */
export function getExperimentExports(experimentId: string): Promise<ExportsResponse> {
  return request<ExportsResponse>(
    `/api/experiments/${encodeURIComponent(experimentId)}/exports`,
  );
}
