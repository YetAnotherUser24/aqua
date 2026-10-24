# Workstream 2 — Experiment Run Lifecycle & Milestones

> Part of the AquaSCADA Lab refactor. Companion plans:
> `plan-ws1-orchestrator.prompt.md`, `plan-ws3-ui.prompt.md`.

## Rung decisions

- **Run record**: rung 2 — `experiments` table already exists
  (`migrations/001_core.sql` line 88) with `status CHECK IN ('planned','active','paused',
  'completed','aborted')`, `started_at`, `ended_at`, `roles[]`, `calibration JSONB`.
  **Reuse it wholesale.** The run lifecycle is a status transition on this table.
- **Per-phase progress**: rung 7 — one new table. Nothing persists phase state today.
- **Milestones**: rung 2 — **reuse the `events` table.** A milestone is an operator-sourced,
  experiment-scoped event carrying `{label, detail, kind, do_mg_l}` in its `detail` JSONB.
  This satisfies the owner's "drawn and listed, don't care how" at zero new storage, and gets
  the milestone into the event export for free (`export/bundle.py` already merges events).
- **Timer / `t-t₀`**: rung 2 — derive from `started_at`; never store a counter.
- **SD logging toggle**: rung 2 — already a `set_config` command in `commands.ts`; just wire a
  button (WS3) → `postCommand`.
- **Event types**: rung 2 — `EXPERIMENT_CREATED/STARTED/PAUSED/RESUMED/COMPLETED/ABORTED/
  PHASE_CHANGE` all already exist in `events/model.py` (lines 62–68). Reuse.

## Files to touch

| File | Change |
|---|---|
| `gateway/migrations/005_run_phases.sql` | New. `experiment_phases` table (per-run phase progress). |
| `gateway/aqua_gateway/events/model.py` | Add `MILESTONE_RECORDED = "milestone_recorded"` (scope OPERATOR, source UI). Nothing else — the rest exists. |
| `gateway/aqua_gateway/api/app.py` | Add `POST /api/experiments/{id}/milestones`, `GET /api/experiments/{id}/milestones`; extend experiment create/start to seed phase rows. |
| `gateway/aqua_gateway/orchestration/engine.py` | Owns phase-row writes (WS1) and experiment status transitions (this WS). |
| `gateway/tests/test_lifecycle.py` | New. Status transitions + milestone persistence + phase rows. |

## Design detail

### `experiment_phases` table (migration 005)

```sql
CREATE TABLE IF NOT EXISTS experiment_phases (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    phase_id      TEXT NOT NULL,              -- recipe-local id, e.g. 'p2_step'
    phase_num     TEXT NOT NULL,              -- display ordinal, '1'..'4'
    mode          TEXT NOT NULL DEFAULT 'auto'
                  CHECK (mode IN ('auto','hibrido','manual')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','active','awaiting_confirm','completed','skipped')),
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    entered_at    TIMESTAMPTZ,
    exited_at     TIMESTAMPTZ,
    UNIQUE (experiment_id, phase_id)
);
CREATE INDEX IF NOT EXISTS idx_phases_exp ON experiment_phases (experiment_id, phase_num);
```

Seeded when an experiment is created (from the recipe), updated by the orchestrator as phases
advance. `status='awaiting_confirm'` is the HIBRIDO wait state (WS1).

### Run lifecycle — status transitions on `experiments`

| Action | Route | Status change | Event |
|---|---|---|---|
| Create | `POST /api/experiments` (exists) | → `planned` | `EXPERIMENT_CREATED` |
| Start | `POST /api/state {ACTIVE_EXPERIMENT}` | `planned`→`active`, set `started_at` | `EXPERIMENT_STARTED` |
| Pause | `POST /api/state {state:'IDLE'}` (or pause verb) | `active`→`paused` | `EXPERIMENT_PAUSED` |
| Resume | `POST /api/state {ACTIVE_EXPERIMENT}` | `paused`→`active` | `EXPERIMENT_RESUMED` |
| Stop | `POST /api/state {state:'IDLE'}` | `active`/`paused`→`completed`, set `ended_at` | `EXPERIMENT_COMPLETED` |
| Abort | E-STOP / explicit | any→`aborted`, set `ended_at` | `EXPERIMENT_ABORTED` |

**Stop must disarm actuators with safety**: the orchestrator issues `stop` (and `stop_mixer`,
`force_off` for the pump) through the outbox *before* flipping status, so a completed run never
leaves an actuator spinning. This mirrors the mockup's `stopExperiment()` intent, but done at
the gateway (the authority) rather than the browser.

**Pause semantics**: pausing holds the *timer* and *phase advancement*; it does **not** stop the
physical loop unless the operator also stops the actuator. Stated explicitly because the mockup
conflates them; the safer reading is that pause is supervisory only. Flagged as a question below.

### Milestones — reuse events (owner: "don't care how")

`POST /api/experiments/{id}/milestones`
body `{kind, label, detail, at_t_dev_ms?}` →
inserts an `events` row: `event_type='milestone_recorded'`, `scope='operator'`, `source='ui'`,
`experiment_id=id`, `detail={kind, label, detail, do_mg_l, t_rel_s}`.

- **Listed**: `GET /api/experiments/{id}/milestones` filters the events table by
  `event_type='milestone_recorded'` — the existing `/api/events` already supports
  `experiment_id` + `event_type` filtering, so this route is a thin convenience wrapper.
- **Drawn on the chart**: WS3 overlays them as scatter points. The `t_rel_s` (computed at
  insert from `started_at`) is what lets the chart place them on the relative-time axis without
  the browser recomputing `t₀`. This is why `at_t_dev_ms` is optional and `t_rel_s` is stored.

Kinds mirror the mockup: `sulfite`, `step`, `winkler`, `note`.

### Timer

`t_rel_s = now - started_at`, computed server-side and returned on `GET /api/state` and
`GET /api/experiments/{id}`. The browser never counts; it renders `started_at` against its own
clock and re-syncs on each SSE frame. This is the edge-case-correct choice: a browser tab that
was backgrounded for an hour shows the true elapsed time, not a throttled `setInterval` count.
Marked `ponytail:` in the code — the ceiling is client-clock skew, upgrade path is a
server-issued `t_rel_s` on every frame (already returned).

## Verification

- `test_lifecycle.py`:
  - create→start→pause→resume→stop walks the status enum and writes `started_at`/`ended_at`.
  - stop issues disarm commands (assert via fake publisher) *before* status flips.
  - milestone insert is retrievable via both the wrapper route and `/api/events?event_type=`.
  - phase rows are seeded on create and advance with the orchestrator.
- Migration applies cleanly on the existing DB (idempotent `IF NOT EXISTS`).

## Risks / open questions

- **Pause: supervisory only, or also disarm?** Plan assumes supervisory-only (actuators keep
  running). Confirm — if pause should also disarm, stop/pause become near-identical.
- **Milestone `t_rel_s` when no experiment is active**: reject with 409, or allow `null`? Plan
  allows `null` (a note before `t₀`), charted at the left edge.
- **Who flips experiment status — the API route or the orchestrator?** Plan: the orchestrator
  owns it, the API route delegates. Keeps one writer for `experiments.status`.
- **Concurrent experiments**: `system_state` holds one `experiment_id`, so only one run can be
  `ACTIVE_EXPERIMENT` at a time by construction. Intentional; flagged.
- **Migration numbering**: 002 is missing in the repo (001→003→004). 005 is next free; using it.

## Status — IMPLEMENTED (2026-10-10)

Owner decisions applied: **pause = supervisory only** (confirmed, no disarm); **native-rate
CSV export required** (new endpoint).

Files created:
- `gateway/migrations/005_orchestration.sql` — `experiment_phases` (shared with WS1).
- `gateway/aqua_gateway/export/flat.py` — native-rate long-format export + events + manifest.
- `gateway/tests/test_lifecycle.py` (10 tests).

Files edited:
- `events/model.py` — `MILESTONE_RECORDED` event type, mapped to `Scope.OPERATOR`.
- `api/app.py` — `POST/GET /api/experiments/{id}/milestones`,
  `POST /api/experiments/{id}/export/native`, `POST /api/state/pause`,
  `POST /api/state/resume`; `ExperimentCreateRequest.recipe_id`; create route now persists
  `recipe:<id>` into notes, seeds phase rows, and emits `EXPERIMENT_CREATED`.
- `orchestration/engine.py` — pause/resume with a paused-span-excluding timer; `_tick` inert
  while paused; pause state reset on begin/end.

Verified: `316 passed` (306 + 10 new); ruff clean on both new files; all 12 lifecycle/export
routes register.

Key decisions recorded:
- **Milestones reuse the events table** (`milestone_recorded`), so they appear in the event
  stream, the export, and the chart overlay with no second store. `t_rel_s` computed at insert.
- **Pause vs stop are distinct**: pause holds the timer and phase advancement without disarming;
  stop disarms before flipping status. Confirmed by tests both ways.
- **Native export is long-format** (`role` + `t_dev_ms` per row), not wide — a wide join at
  native rate would have to invent rows for roles that were not sampling at that instant.
- The native export streams per role, so peak memory is one row, not one file (905 MB box).

