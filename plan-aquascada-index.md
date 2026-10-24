# AquaSCADA Lab refactor — plan index

Owner-confirmed decisions (2026-10-10):
- Milestones: drawn **and** listed on the chart — implementation choice delegated.
- Architecture: **gateway = master orchestrator** (issues abstract setpoints/commands);
  **nodes = inner-loop controllers** (obey and satisfy via their own firmware loops).
- Charting: **keep recharts** (no Chart.js).
- **No kLa estimation.** Saturation is empirically ~7.15 mg/L, shown as a constant.
- **Revive `StateRequest`** (`api/app.py` line 52) into a real `POST /api/state` route.
- **Pause = supervisory only** (confirmed; do not disarm actuators on pause).
- **Export**: add a full-fidelity CSV download — every sample from actuators, sensors and
  events at their native rate (no resampling to a grid). New endpoint permitted.

Plan artifacts:
- `plan-ws1-orchestrator.prompt.md` — state machine, recipes, conditions, command issuance via the existing outbox.
- `plan-ws2-lifecycle.prompt.md` — run lifecycle on the existing `experiments` table, phase progress table, milestones as events.
- `plan-ws3-ui.prompt.md` — AquaSCADA Lab re-skin + observatory chart + multivariable inspector + recipe phases.

Ground truth already in the schema (reuse, do not recreate):
- `system_settings.system_state` JSONB — `IDLE | ACTIVE_EXPERIMENT | MANUAL_OVERRIDE` (`migrations/001_core.sql` line 73).
- `experiments` table with `status IN ('planned','active','paused','completed','aborted')` (line 88).
- `control_commands` outbox with `SUPERSEDING_ACTIONS` / `SAFETY_ACTIONS` (`commands/outbox.py`).
- Event types `EXPERIMENT_*`, `STATE_CHANGE`, `KLA_DEGRADATION_SUSPECTED` (`events/model.py`).

Recommended ordering: WS1 + WS2 (gateway-first, verified with pytest) → WS3 (UI against the
stable API). Do not start WS3 until the new endpoints exist.

## Progress
- **WS1 — DONE.** Orchestrator engine, recipes, conditions; 22 tests. `306 passed`.
- **WS2 — DONE.** Lifecycle (pause/resume/stop), milestones-as-events, native-rate export;
  10 tests. `316 passed`. New routes: `/api/state/{pause,resume}`, `/api/experiments/{id}/milestones`,
  `/api/experiments/{id}/export/native`.
- **WS3 — DONE.** UI refactor to the AquaSCADA Lab console. `npm run typecheck` clean,
  `npm run build` (static export) succeeds. Verified in-browser against the mock gateway
  (all panels render with live data).

### WS3 files
New components: `RunHeader`, `KpiRibbon`, `ObservatoryChart`, `MultivariableInspector`,
`RecipePhases`, `ActuatorDeck`, `MilestoneComposer`. New hook: `useRunState`.
Edited: `page.tsx` (recomposed), `tailwind.config.js` (scada/telemetry theme),
`types.ts`, `api.ts`, `registry.ts` (readField/readMotor/readDoDetail + `Reading.missing`),
`format.ts` (formatDurationClock/formatRelativeClock).
Also extended `tools/mock-gateway.py` with `/api/state`, `/api/recipes`, `/api/samples`,
milestones — so the UI can be verified end to end without a database.

Bug found during WS3: WS1's `dose_complete` condition used `pmp_dosed_ml`; the registry
field is `pump_dosed_ml`. Fixed in `conditions.py` + its test.
