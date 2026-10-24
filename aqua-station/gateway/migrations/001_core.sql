-- ============================================================================
-- 001_core.sql — operational tables
-- ============================================================================
-- PostgreSQL 17, tuned for a 1 GB Raspberry Pi 3B+ (see deploy/postgres/).
--
-- Scope note: this database holds OPERATIONAL state and 1-minute rollups only.
-- Full-fidelity telemetry lives in append-only CSV files on disk and is archived
-- to Supabase Storage. A relational row costs ~200 B effectively and dirties an
-- 8 KB page per write; a CSV row costs ~85 B appended sequentially. Telemetry is
-- an immutable time-ordered log, so files are the right structure for it.
--
-- 5 Hz is confined to ~3 hour experiment windows, and idle telemetry runs at
-- 1/minute, so this database stays small by design.
-- ============================================================================

-- `gen_random_uuid()` has been core PostgreSQL since 13; `pgcrypto` only provides it on
-- older servers. Creating the extension unconditionally makes this migration fail outright on
-- any build without contrib installed — including the embedded PostgreSQL used for local
-- development — which blocks the entire install for a function that is already present.
-- So: ask for the extension only when the function is genuinely missing.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE p.proname = 'gen_random_uuid'
    ) THEN
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Node registry. Mirrors gateway/aqua_gateway/schema/roles.yaml — that file is
-- the source of truth for TELEMETRY fields; this table is the source of truth
-- for node IDENTITY and credentials.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nodes (
    id              UUID PRIMARY KEY,
    role            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    location        TEXT,
    -- SHA-256 of the MQTT password. Never store the plaintext.
    secret_hash     TEXT,
    schema_version  INTEGER NOT NULL DEFAULT 2,
    -- Expected telemetry cadence per mode, used to detect a node that has gone
    -- quiet without disconnecting (the failure MQTT LWT cannot see).
    expected_hz_experiment  DOUBLE PRECISION,
    expected_hz_idle        DOUBLE PRECISION,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nodes IS
    'Node identity and credentials. Telemetry field definitions live in roles.yaml.';

-- ---------------------------------------------------------------------------
-- Key/value settings. Retains the previous system's system_settings shape so the
-- orchestrator state machine and experiment registry port across unchanged.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Orchestrator state machine: IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE.
-- Seeded with an explicit IDLE so the dashboard never has to invent a default.
INSERT INTO system_settings (key, value, description)
VALUES (
    'system_state',
    '{"state":"IDLE","since":"1970-01-01T00:00:00Z","experiment_id":null,
      "override":{"master":false,"pump":false,"mixer":false,"odrive":false},"updated_by":"system"}'::jsonb,
    'Global orchestrator state (IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE)'
)
ON CONFLICT (key) DO NOTHING;

INSERT INTO system_settings (key, value, description)
VALUES ('experiments_registry', '[]'::jsonb, 'Registry of experiments (array of experiment objects)')
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Experiments. Promoted out of the JSONB registry into a real table so that
-- bundles, events and retention can reference an experiment by key, and so the
-- export can be built with a join rather than by scanning a JSON array.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiments (
    id           TEXT PRIMARY KEY,           -- EXP-YYYY-MM-DD-NN
    title        TEXT NOT NULL,
    hypothesis   TEXT,
    status       TEXT NOT NULL DEFAULT 'planned'
                 CHECK (status IN ('planned','active','paused','completed','aborted')),
    -- Roles that participate. Recorded because you cannot know at write time
    -- which nodes will take part, and the export must not guess.
    roles        TEXT[] NOT NULL DEFAULT '{}',
    started_at   TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    -- Calibration constants in force. ML_PER_REV must be pinned here or a dose
    -- cannot be reconstructed after the fact.
    calibration  JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes        TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments (status, started_at DESC);

-- ---------------------------------------------------------------------------
-- Command queue. Retained as a durable outbox: MQTT delivers, but the record of
-- what was requested, delivered and applied must survive a broker restart.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control_commands (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id        UUID REFERENCES nodes(id) ON DELETE SET NULL,
    role           TEXT NOT NULL,
    experiment_id  TEXT REFERENCES experiments(id) ON DELETE SET NULL,
    action         TEXT NOT NULL,
    target         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status         TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued','published','acknowledged','rejected','failed','expired','superseded')),
    requested_by   TEXT NOT NULL DEFAULT 'operator',
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at   TIMESTAMPTZ,
    ack_at         TIMESTAMPTZ,
    -- A stale command must never execute: a node waking from deep sleep after a
    -- day must refuse rather than act on a setpoint from a finished experiment.
    deadline_at    TIMESTAMPTZ NOT NULL,
    superseded_by  UUID REFERENCES control_commands(id) ON DELETE SET NULL,
    -- The previous system discovered that a queued set_state could silently undo a
    -- manual override. Recording the supersession makes that resolution explicit
    -- instead of leaving it to be inferred from timestamps.
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_commands_pending  ON control_commands (role, status, issued_at)
    WHERE status IN ('queued','published');
CREATE INDEX IF NOT EXISTS idx_commands_deadline ON control_commands (deadline_at)
    WHERE status IN ('queued','published');
CREATE INDEX IF NOT EXISTS idx_commands_exp      ON control_commands (experiment_id, issued_at DESC);

-- ---------------------------------------------------------------------------
-- Alerts. Kept separate from the event stream: an alert is a persistent
-- condition with a lifecycle (raised/acknowledged/resolved), whereas an event is
-- an instantaneous, immutable fact. Conflating them is why the previous system
-- could not answer "is this still a problem?".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id       UUID REFERENCES nodes(id) ON DELETE SET NULL,
    experiment_id TEXT REFERENCES experiments(id) ON DELETE SET NULL,
    alert_type    TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT 'warning'
                  CHECK (severity IN ('info','warning','critical')),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','acknowledged','resolved')),
    message       TEXT NOT NULL,
    metric        TEXT,
    value         DOUBLE PRECISION,
    threshold     DOUBLE PRECISION,
    -- Correlation id links the alert to the event that raised it and to the
    -- command that responded, so the whole causal chain is queryable.
    correlation_id TEXT,
    raised_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at   TIMESTAMPTZ,
    resolved_by   TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (status, severity, raised_at DESC)
    WHERE status <> 'resolved';
CREATE INDEX IF NOT EXISTS idx_alerts_exp    ON alerts (experiment_id, raised_at DESC);
