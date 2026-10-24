-- ============================================================================
-- 004_rollups.sql — 1-minute aggregates
-- ============================================================================
-- The dashboard's charts read from here, never from raw telemetry. That is the
-- point: a 24-hour chart over 1 Hz raw data is 86,400 rows, over 1-minute rollups
-- it is 1,440. On a Pi 3B+ that difference is CPU, and it is also SD reads.
--
-- Populated by the gateway's rollup/aggregator.py, which accumulates in memory and
-- issues one small INSERT per minute per role. No continuous-aggregate extension is
-- needed at these volumes — a plain table maintained by the writer is simpler and
-- has no background job to babysit.
--
-- Wide by role, one row per (role, minute). Derived from the archive's definition
-- of an experiment window, so a rollup always covers the same wall-clock minute
-- regardless of which nodes happened to be connected.
-- ============================================================================

CREATE TABLE IF NOT EXISTS rollup_1min (
    bucket      TIMESTAMPTZ NOT NULL,
    role        TEXT NOT NULL,
    experiment_id TEXT,

    -- Sample accounting. `samples_expected` comes from the role's declared cadence
    -- for the mode in force, so a shortfall is visible rather than silently
    -- producing an average over fewer points than intended.
    samples          INTEGER NOT NULL DEFAULT 0,
    samples_expected INTEGER,
    downtime_s       INTEGER NOT NULL DEFAULT 0,

    -- Aggregate columns are nullable because not every role reports every metric,
    -- and a missing value must be distinguishable from a zero. The previous schema
    -- fabricated 0.0 for absent readings, which is a different claim entirely.
    drv_rpm_avg        DOUBLE PRECISION,
    drv_rpm_min        DOUBLE PRECISION,
    drv_rpm_max        DOUBLE PRECISION,
    drv_rpm_target_avg DOUBLE PRECISION,
    drv_power_w_avg    DOUBLE PRECISION,
    drv_power_w_max    DOUBLE PRECISION,
    drv_vbus_v_avg     DOUBLE PRECISION,
    drv_ibus_a_avg     DOUBLE PRECISION,
    drv_ibus_a_max     DOUBLE PRECISION,
    drv_running_pct    DOUBLE PRECISION,

    od_do_mg_l_avg     DOUBLE PRECISION,
    od_do_mg_l_min     DOUBLE PRECISION,
    od_do_mg_l_max     DOUBLE PRECISION,
    od_sat_pct_avg     DOUBLE PRECISION,
    od_temp_c_avg      DOUBLE PRECISION,
    od_temp_c_min      DOUBLE PRECISION,
    od_temp_c_max      DOUBLE PRECISION,

    pump_dosed_ml_sum  DOUBLE PRECISION,
    pwr_battery_v_min  DOUBLE PRECISION,

    -- Fraction of samples whose source clock was low-trust. Non-zero means this
    -- bucket should be examined before being trusted in the analysis.
    uncertain_frac     DOUBLE PRECISION NOT NULL DEFAULT 0,

    PRIMARY KEY (role, bucket)
);

CREATE INDEX IF NOT EXISTS idx_rollup_exp    ON rollup_1min (experiment_id, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_rollup_bucket ON rollup_1min (bucket DESC);

COMMENT ON TABLE rollup_1min IS
    'Dashboard read path. Raw telemetry never leaves the CSV archive; charts read here.';

-- ---------------------------------------------------------------------------
-- Hourly rollup for long-range views. Populated from rollup_1min by the same
-- aggregator, so there is exactly one place that knows how to aggregate.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rollup_1h (
    bucket      TIMESTAMPTZ NOT NULL,
    role        TEXT NOT NULL,
    experiment_id TEXT,
    minutes_with_data INTEGER NOT NULL DEFAULT 0,
    od_do_mg_l_avg DOUBLE PRECISION,
    od_do_mg_l_min DOUBLE PRECISION,
    od_temp_c_avg  DOUBLE PRECISION,
    drv_rpm_avg    DOUBLE PRECISION,
    drv_power_w_avg DOUBLE PRECISION,
    drv_running_pct DOUBLE PRECISION,
    pump_dosed_ml_sum DOUBLE PRECISION,
    PRIMARY KEY (role, bucket)
);

-- ---------------------------------------------------------------------------
-- Node presence history, built from MQTT LWT transitions. Kept separate from the
-- event stream because uptime is queried as a continuous property ("was the
-- aerator online at 14:32 during experiment X?") rather than as discrete facts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS node_presence (
    node_id     UUID NOT NULL,
    role        TEXT NOT NULL,
    online_from TIMESTAMPTZ NOT NULL,
    online_to   TIMESTAMPTZ,
    reason      TEXT,
    PRIMARY KEY (node_id, online_from)
);

CREATE INDEX IF NOT EXISTS idx_presence_window ON node_presence (role, online_from, online_to);

COMMENT ON TABLE node_presence IS
    'Interval-based presence. Answers gap questions that a discrete event log cannot.';
