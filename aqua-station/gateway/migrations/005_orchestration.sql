-- 005: per-phase progress for an orchestrated run.
--
-- The global state machine already lives in ``system_settings.system_state`` (001_core.sql)
-- and the run's own lifecycle is the ``experiments.status`` enum. What was missing is where a
-- run *is* inside its recipe: which phase is active, which are done, and in what mode each was
-- run. That is what this table holds.
--
-- Kept separate from ``experiments`` rather than a JSONB blob on it, because the dashboard and
-- any post-hoc analysis both want to ask "which phase was active at 14:32" as a query, and a
-- blob makes that a scan-and-parse.
--
-- ``awaiting_confirm`` is the hybrid-mode wait state: the phase's own condition was met, but
-- the operator has not yet given their visto bueno. Modelling it as a status rather than a UI
-- flag is what lets the run survive a gateway restart without silently advancing.

CREATE TABLE IF NOT EXISTS experiment_phases (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    -- Recipe-local identifier, e.g. 'p2_step'. Stable across runs of the same recipe.
    phase_id      TEXT NOT NULL,
    -- Display ordinal as the recipe declares it ('1'..'4'); not necessarily numeric.
    phase_num     TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'auto'
                  CHECK (mode IN ('auto','hibrido','manual')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','active','awaiting_confirm','completed','skipped')),
    -- The parameter values the phase ran with. Recorded so a dose can be reconstructed
    -- after the fact, the same reason ``experiments.calibration`` exists.
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    entered_at    TIMESTAMPTZ,
    exited_at     TIMESTAMPTZ,
    -- One row per phase per run. The orchestrator upserts on this key.
    UNIQUE (experiment_id, phase_id)
);

CREATE INDEX IF NOT EXISTS idx_phases_exp    ON experiment_phases (experiment_id, phase_num);
CREATE INDEX IF NOT EXISTS idx_phases_status ON experiment_phases (status)
    WHERE status IN ('active', 'awaiting_confirm');
