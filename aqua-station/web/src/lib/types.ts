/**
 * Wire types.
 *
 * These describe exactly what the gateway sends — not what the UI wants. All reshaping
 * happens in `registry.ts` and `format.ts`.
 *
 * Hand-written rather than generated because the gateway deliberately exposes no OpenAPI
 * schema (`docs_url=None`, `openapi_url=None`): the surface is small enough that a
 * generator would cost more than it saves, and a stale generated client is worse than a
 * stale hand-written one because it looks authoritative.
 *
 * If a field in here disagrees with the gateway, that is a bug in this file.
 */

// ---------------------------------------------------------------------------
// Schema registry — GET /api/roles
// ---------------------------------------------------------------------------

/** Mirrors `FieldType` in gateway `schema/registry.py`. Note there is no `string`. */
export type FieldType = 'float' | 'int' | 'bool' | 'enum';

export interface FieldDef {
  name: string;
  type: FieldType;
  unit: string | null;
  precision: number | null;
  /** Non-empty only for `enum`. */
  values: string[];
  /**
   * `false` marks an echo of *another* node's measurement (the `peer_*` fields).
   * A peer value is a real reading, but it is not this node's claim to make, so it must
   * never be averaged with, or preferred over, the authoritative source.
   */
  authoritative: boolean;
  note: string;
  /** Rollup aggregate suffixes backed by real columns, e.g. `["avg","min","max"]`. */
  aggregates: string[];
}

export interface RoleDef {
  display_name: string;
  node_id: string;
  topic: string;
  /** Quantities this role is the source of truth for. */
  authoritative_for: string[];
  /** Declared cadence per mode, e.g. `{experiment: 5.0, idle: 0.0167}`. */
  expected_rate_hz: Record<string, number>;
  fields: FieldDef[];
}

export interface Registry {
  schema_version: number;
  max_batch: number;
  roles: Record<string, RoleDef>;
}

// ---------------------------------------------------------------------------
// Live state — GET /api/live and GET /api/stream (identical payload)
// ---------------------------------------------------------------------------

export type TimeSource = 'ntp' | 'espnow' | 'rtc' | 'monotonic';

/**
 * Trust ordering, mirroring `TimeSource.trust` in the gateway. Higher is better.
 * `monotonic` means the node's wall clock was reconstructed from an uptime counter, so
 * its timestamps are ordered but not anchored.
 */
export const TIME_SOURCE_TRUST: Record<TimeSource, number> = {
  ntp: 3,
  espnow: 2,
  rtc: 1,
  monotonic: 0,
};

export type MetricValue = number | boolean | string | null;

export interface LiveNode {
  role: string;
  node_id: string;
  /** From MQTT Last Will & Testament — the broker reports the drop, not a heuristic. */
  online: boolean;
  /** Seconds since the gateway last heard from this node. */
  age_s: number | null;
  rssi: number | null;
  t_src: TimeSource;
  /** Schema version of the last envelope. Null until the node has reported one: a node
   *  that has never connected has no version, and reporting 0 claimed it sent v0. */
  schema_version: number | null;
  /** Samples per second actually arriving, measured from the node's own sample timestamps.
   *  Null until two samples have. Compare against the declared cadence in the registry: a
   *  node can be online and still be delivering a fraction of what it declares. */
  observed_hz: number | null;
  /** Gateway arrival minus device timestamp. Drift, not an error, unless it is large. */
  clock_offset_ms: number | null;
  metrics: Record<string, MetricValue>;
}

export interface LiveSnapshot {
  /** Gateway receipt time, epoch seconds. Not the device clock. */
  ts: number;
  nodes: Record<string, LiveNode>;
}

// ---------------------------------------------------------------------------
// Events — GET /api/events
// ---------------------------------------------------------------------------

export type Severity = 'info' | 'warning' | 'critical';
export type EventScope = 'system' | 'node' | 'experiment' | 'process' | 'operator' | 'data';
/** Who observed the event. Provenance changes what it means. */
export type EventSource = 'firmware' | 'gateway' | 'ui';

export interface AquaEvent {
  id: number;
  /** ISO 8601 from the gateway clock. This one is always trustworthy. */
  ts: string;
  /** Device clock at origin, when the producer had one. May be null or untrusted. */
  t_dev_ms: number | null;
  experiment_id: string | null;
  event_type: string;
  severity: Severity;
  scope: EventScope;
  source: EventSource;
  node_id: string | null;
  correlation_id: string | null;
  action: string | null;
  detail: Record<string, unknown>;
}

export interface EventTypesResponse {
  types: string[];
  severities: Severity[];
  scopes: EventScope[];
}

// ---------------------------------------------------------------------------
// Derived rule state — GET /api/events/levels
// ---------------------------------------------------------------------------

export type RuleLevel = 'OK' | 'WARNING' | 'CRITICAL';

export interface DerivedThresholds {
  do_warning_mg_l: number;
  do_critical_mg_l: number;
  do_recovery_deadband_mg_l: number;
  do_drop_rate_mg_l_per_min: number;
  do_jump_mg_l: number;
  temp_warning_c: number;
  battery_warning_v: number;
}

export interface LevelsResponse {
  /** `{role: {rule: level}}`. A latched rule stays quiet until it recovers past its
   *  deadband, so "no recent alert" is not the same claim as "value is fine". */
  levels: Record<string, Record<string, RuleLevel>>;
  thresholds: DerivedThresholds;
}

// ---------------------------------------------------------------------------
// History — GET /api/history
// ---------------------------------------------------------------------------

export interface HistoryPoint {
  bucket: string;
  value: number | null;
  samples: number;
  /** Null when the role declares no cadence, so completeness is unknowable. */
  samples_expected: number | null;
  /** Fraction of samples whose source clock was low-trust. Non-zero means: look closer. */
  uncertain_frac: number;
}

export interface HistoryResponse {
  role: string;
  metric: string;
  column: string;
  range: string;
  /** `rollup_1min` or `rollup_1h` — the API picks based on the range span. */
  source: string;
  data: HistoryPoint[];
}

// ---------------------------------------------------------------------------
// Raw samples — GET /api/samples
// ---------------------------------------------------------------------------

export interface RawSample {
  bucket: string;
  value: number | null;
}

/**
 * Every point, not one per minute.
 *
 * Carries the same `bucket`/`value` keys as :interface:`HistoryPoint` so a chart can move
 * between the two tiers without a second parser — the rollup fields are simply absent, which
 * is why the chart reads them through a shared structural type rather than branching.
 */
export interface SamplesResponse {
  role: string;
  metric: string;
  range: string;
  /** Always `archive`. Named so the UI can say which tier answered. */
  source: string;
  count: number;
  /** True when the server capped the series at its limit and this is a subset. */
  truncated: boolean;
  data: RawSample[];
}

// ---------------------------------------------------------------------------
// Commands — POST/GET /api/commands
// ---------------------------------------------------------------------------

export type CommandStatus =
  | 'queued'
  | 'published'
  | 'acknowledged'
  | 'rejected'
  | 'failed'
  | 'expired'
  | 'superseded';

export interface CommandRecord {
  id: string;
  role: string;
  action: string;
  target: Record<string, unknown>;
  status: CommandStatus;
  experiment_id: string | null;
  requested_by: string;
  issued_at: string | null;
  deadline_at: string | null;
  published_at: string | null;
  ack_at: string | null;
  superseded_by: string | null;
  error: string | null;
  /** Ids of commands this one replaced. Non-empty means an intent was withdrawn. */
  superseded: string[];
}

/** Actions that must bypass the database, because blocking a stop on a write is the
 *  wrong trade. Mirrors `SAFETY_ACTIONS` in `commands/outbox.py`. */
export const SAFETY_ACTIONS = ['emergency_stop', 'clear_estop', 'failsafe_clear'] as const;

/** Actions where a newer command atomically withdraws an older pending one.
 *  Mirrors `SUPERSEDING_ACTIONS` in `commands/outbox.py`. */
export const SUPERSEDING_ACTIONS = [
  'set_speed',
  'set_config',
  'set_mode',
  'set_recipe',
  'set_state',
  'start',
  'stop',
  'start_mixer',
  'stop_mixer',
  'dose',
  'prime',
  'drain',
  'suckback',
  'retract',
  'force_off',
  'set_meniscus',
] as const;

// ---------------------------------------------------------------------------
// Experiments — GET /api/experiments
// ---------------------------------------------------------------------------

export interface Experiment {
  id: string;
  title: string;
  status: string;
  roles: string[] | null;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
}

export interface ExportReport {
  experiment_id: string;
  out_dir: string;
  bundle: string | null;
  rows: number;
  columns: string[];
  events: number;
  grid_seconds: number;
  roles_present: string[];
  /** Roles that took part but produced no data in the window. Worth surfacing: it is
   *  the difference between "nothing happened" and "the node was not recording". */
  roles_missing: string[];
  warnings: string[];
}

// ---------------------------------------------------------------------------
// Health — GET /healthz
// ---------------------------------------------------------------------------

export interface PowerHealth {
  voltage_v: number | null;
  current_a: number | null;
  power_w: number | null;
  shunt_mv?: number | null;
}

export interface RtcHealth {
  temp_c: number | null;
  valid: boolean;
  status: 'ok' | 'power_lost' | 'unavailable' | string;
}

export interface HardwareHealth {
  available: boolean;
  cpu_temp_c: number | null;
  rtc: RtcHealth;
  power_in?: PowerHealth | null;
  power_5v?: PowerHealth | null;
}

export interface HealthResponse {
  status: string;
  ingest: Record<string, unknown>;
  archive: Record<string, unknown>;
  rollup: Record<string, unknown>;
  events: Record<string, unknown>;
  commands: Record<string, unknown>;
  database: Record<string, unknown>;
  storage?: Record<string, unknown>;
  hardware?: HardwareHealth;
  /**
   * Age of the oldest archive file not yet pushed to object storage. With a 14-day local
   * window this is the only warning before data loss becomes permanent.
   */
  flush_backlog_s: number | null;
  live_subscribers: number;
}

// ---------------------------------------------------------------------------
// Orchestration — GET/POST /api/state, GET /api/recipes
// ---------------------------------------------------------------------------

/** The three global states, mirroring `orchestration/engine.py`. The string values are the
 *  contract; do not rename one here without renaming it there. */
export type RunState = 'IDLE' | 'ACTIVE_EXPERIMENT' | 'MANUAL_OVERRIDE';

/** A phase's mode. The operator may override any phase for the length of a run. */
export type PhaseMode = 'auto' | 'hibrido' | 'manual';

/** Where a phase is in its own lifecycle. `awaiting_confirm` is the HIBRIDO wait state:
 *  the exit condition was met and the engine is holding for the operator's visto bueno. */
export type PhaseStatus = 'pending' | 'active' | 'awaiting_confirm' | 'completed' | 'skipped';

/** A phase as declared in the recipe — the static description. */
export interface PhaseDef {
  phase_id: string;
  num: string;
  title: string;
  /** Operator-facing objective. May contain a little `<strong>` markup from the recipe. */
  desc?: string;
  mode: PhaseMode;
  condition_text: string;
  /**
   * Hard ceiling in seconds, or `null` for a supervised phase that waits for its operator.
   *
   * `null` means "no clock", not "use the default": `auto` phases always carry a number
   * (the gateway refuses to load a recipe where one doesn't), while `manual`/`hibrido`
   * phases are released by the operator, or by stopping the run.
   */
  timeout_s: number | null;
  params: Record<string, unknown>;
}

/** A phase as the run is actually executing it — the recipe plus live status and mode. */
export interface PhaseProgress extends PhaseDef {
  status: PhaseStatus;
}

/** A recipe from `GET /api/recipes`. Static data; fetched once. */
export interface RecipeDef {
  recipe_id: string;
  title: string;
  filename: string;
  roles: string[];
  phases: PhaseDef[];
}

export interface RecipesResponse {
  recipes: RecipeDef[];
}

/** The orchestrator's live view, from `GET /api/state` and every transition. */
export interface RunStateResponse {
  state: RunState;
  /** Paused is orthogonal to `state`: a run can be ACTIVE_EXPERIMENT and paused. */
  paused: boolean;
  experiment_id: string | null;
  recipe_id: string | null;
  phase_id: string | null;
  phase_num: string | null;
  /** The phase awaiting the operator's confirmation, or null. Matches a `phase_id`. */
  awaiting_confirm: string | null;
  /** The MANUAL phase whose entry commands are held until the operator fires them
   *  ("Disparar Acción"), or null. Matches a `phase_id`. */
  action_pending: string | null;
  /** Run start, ISO 8601. The origin for the relative-time axis. */
  started_at: string | null;
  /** Milliseconds (well, seconds, per WS2) since `started_at`, excluding paused spans.
   *  Authoritative — the browser never counts its own. */
  t_rel_s: number | null;
  phases: PhaseProgress[];
}

// ---------------------------------------------------------------------------
// Milestones — GET/POST /api/experiments/{id}/milestones
// ---------------------------------------------------------------------------

/** The operator-facing kinds, mirroring the milestone composer's chips. The gateway stores
 *  `kind` verbatim, so an unknown one is preserved rather than rejected. */
export type MilestoneKind = 'sulfite' | 'step' | 'winkler' | 'note';

export interface MilestonePayload {
  kind: MilestoneKind;
  label: string;
  detail: string;
  /** Device clock at the annotation, when the client has one. Optional by design. */
  at_t_dev_ms?: number;
  requested_by?: string;
}

/** A milestone as returned. It is an event row, so it carries the event's fields too —
 *  `detail` holds `{kind, label, detail, t_rel_s, do_mg_l}`. */
export interface Milestone {
  id: number;
  ts: string;
  t_dev_ms: number | null;
  experiment_id: string;
  event_type: string;
  severity: Severity;
  scope: EventScope;
  source: EventSource;
  action: string | null;
  detail: {
    kind?: MilestoneKind;
    label?: string;
    detail?: string;
    /** Seconds since run start, computed at insert so the chart can place it directly. */
    t_rel_s?: number | null;
    /** Oxygen at the moment of the annotation, so the dot sits on the curve. */
    do_mg_l?: number;
  };
}

export interface MilestonesResponse {
  experiment_id: string;
  milestones: Milestone[];
}

/** The native-rate export report, from `POST /api/experiments/{id}/export/native`. */
export interface NativeExportReport {
  experiment_id: string;
  out_dir: string;
  bundle: string | null;
  format: 'native';
  rows: number;
  columns: string[];
  events: number;
  per_role_rows: Record<string, number>;
  roles_present: string[];
  roles_missing: string[];
  warnings: string[];
}

/** One export already on disk, from `GET /api/experiments/{id}/exports`. */
export interface ExportEntry {
  experiment_id: string;
  /** `aqua-native-1`, `aligned`, or `unknown` when the manifest could not be read. */
  format: string;
  dir: string;
  bundle: string | null;
  generated_at: string | null;
  rows: number;
  events: number;
  roles_present: string[];
  roles_missing: string[];
  size_bytes: number;
  modified_at: string;
}

export interface ExportsResponse {
  experiment_id: string;
  exports: ExportEntry[];
}
