-- ============================================================================
-- 003_events.sql — unified event stream
-- ============================================================================
-- One append-only table fed by three producers: firmware, gateway, and UI.
-- Identical shape for all three, so the experiment export merges without special
-- cases. Provenance is recorded in `source` because a `do_low_critical` derived by
-- the gateway from a threshold is a different claim from one reported by the probe
-- as a hardware fault, and merging them without provenance would be ambiguous.
--
-- Partitioned by month. Events are small and disproportionately valuable, so they
-- are retained far longer than telemetry — but they still grow, and dropping a
-- whole month's partition is instant where DELETE + VACUUM is not.
--
-- No TimescaleDB required: declarative partitioning gives the retention benefit
-- (DROP TABLE instead of DELETE), and this schema does not need compression.
-- ============================================================================

CREATE TABLE IF NOT EXISTS events (
    id             BIGINT GENERATED ALWAYS AS IDENTITY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Device clock at origin, when the producer had one. The gateway's own `ts` is
    -- always trustworthy; this one is not, which is why both are stored.
    t_dev_ms       BIGINT,
    experiment_id  TEXT,
    event_type     TEXT NOT NULL,
    severity       TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    scope          TEXT NOT NULL CHECK (scope IN ('system','node','experiment','process','operator','data')),
    source         TEXT NOT NULL CHECK (source IN ('firmware','gateway','ui')),
    node_id        UUID,
    correlation_id TEXT,
    action         TEXT,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

COMMENT ON TABLE events IS
    'Append-only unified event stream. See docs/EVENTS.md for the full taxonomy.';

-- Current month plus one ahead, so a month boundary never lands without a partition.
DO $$
DECLARE
    start_of_month DATE := date_trunc('month', now())::date;
    month_start    DATE;
    month_end      DATE;
    part_name      TEXT;
BEGIN
    FOR i IN 0..1 LOOP
        month_start := (start_of_month + (i || ' month')::interval)::date;
        month_end   := (month_start + '1 month'::interval)::date;
        part_name   := 'events_' || to_char(month_start, 'YYYY_MM');
        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part_name) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF events FOR VALUES FROM (%L) TO (%L)',
                part_name, month_start, month_end
            );
        END IF;
    END LOOP;
END $$;

-- Query patterns: "what happened during experiment X", "what went wrong on node Y",
-- "show me all criticals this week". The export reads by experiment, the dashboard
-- by recency.
CREATE INDEX IF NOT EXISTS idx_events_exp_ts     ON events (experiment_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_node_ts    ON events (node_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_ts    ON events (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_severity   ON events (severity, ts DESC) WHERE severity <> 'info';
CREATE INDEX IF NOT EXISTS idx_events_corr       ON events (correlation_id) WHERE correlation_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Create next month's partition ahead of time. Called by the gateway on the 1st,
-- and by deploy/install.sh. A missing partition would reject writes, so this is
-- the one schema operation that must not fail silently.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ensure_event_partition(months_ahead INTEGER DEFAULT 1)
RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE
    month_start DATE := (date_trunc('month', now()) + (months_ahead || ' month')::interval)::date;
    month_end   DATE := (month_start + '1 month'::interval)::date;
    part_name   TEXT := 'events_' || to_char(month_start, 'YYYY_MM');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part_name) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF events FOR VALUES FROM (%L) TO (%L)',
            part_name, month_start, month_end
        );
        RETURN part_name;
    END IF;
    RETURN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- Unified view for the dashboard: events joined to their experiment title, with a
-- human-readable node role. Keeps the API layer free of repetitive joins.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW events_readable AS
SELECT
    e.id,
    e.ts,
    e.t_dev_ms,
    e.event_type,
    e.severity,
    e.scope,
    e.source,
    e.experiment_id,
    x.title AS experiment_title,
    e.node_id,
    n.role  AS node_role,
    e.correlation_id,
    e.action,
    e.detail
FROM events e
LEFT JOIN experiments x ON x.id = e.experiment_id
LEFT JOIN nodes      n ON n.id = e.node_id;
