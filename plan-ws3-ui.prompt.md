# Workstream 3 — Web UI/UX Refactor (AquaSCADA Lab)

> Part of the AquaSCADA Lab refactor. Companion plans:
> `plan-ws1-orchestrator.prompt.md`, `plan-ws2-lifecycle.prompt.md`.
> Target mockup: `Tesis-webpage/public/mockup.html`.

## Rung decisions

- **Charting**: rung 5 — **keep `recharts`** (owner-confirmed). The mockup uses Chart.js, but
  switching is a dependency swap with zero functional gain. Dual-axis (`yAxisId`), reference
  lines, scatter overlays and dashed/stepped series are all native to recharts.
- **Theme colors**: rung 6 — extend `tailwind.config.js` with the mockup's `scada.*` /
  `telemetry.*` palette. No component rewrite beyond class remapping.
- **Icons**: rung 2 — already on `lucide-react` (mockup uses Lucide CDN). Same icon names.
- **State**: rung 2 — reuse `useLive()` (SSE) for telemetry and add thin `useAsync` calls for
  the new `/api/state`, `/api/recipes`. **No new global store.**
- **kLa**: rung 0 — **cut.** Owner confirmed no kLa estimation. The KPI ribbon shows 5 cards
  (DO, saturation, temperature, aerator, timer); saturation uses empirical `C* ≈ 7.15 mg/L`
  as a display constant, not a computed quantity.
- **Milestones**: rung 2 — overlaid from `/api/events?event_type=milestone_recorded` (WS2).

## What exists vs. what the mockup adds

| Mockup block | Current file | Action |
|---|---|---|
| Header HUD (nodes, clock, run controls, E-STOP) | `page.tsx` header + `StatusStrip.tsx` | Rework header; keep `PresenceSummary`/`SessionAge` logic |
| 4-node health matrix | `StatusStrip.tsx` | Condense to a compact matrix, reuse data |
| KPI ribbon (DO/sat/temp/motor/timer) | `MetricCards.tsx` | Rework into ribbon; fold in sat/temp; add timer |
| Observatory chart (DO + ODrive + milestones) | `ChartsSection.tsx` | Rewrite as `ObservatoryChart.tsx` |
| Multivariable inspector (free vars, dual axis) | — | New `MultivariableInspector.tsx` |
| Actuators tab (ODrive slider, mixer PI, pump dose) | `ActuatorCards.tsx` + `CommandConsole.tsx` | New `ActuatorDeck.tsx`; keep command logic |
| Recipe phases (AUTO/HÍBRIDO/MANUAL) | — | New `RecipePhases.tsx` |
| Event feed + interlocks | `EventsTable.tsx` + `LevelsPanel.tsx` | Keep, restyle, add milestone composer |
| Diagnostics | `HealthPanel.tsx` | Restyle into a tab |
| Export dropdown (1 Hz / 5 Hz / dataset) | `ExperimentsPanel.tsx` | Repoint to real export; drop kLa dataset |

## Files to touch

**Config**
- `web/tailwind.config.js` — add `scada.canvas/surface/elevated/well/border*` and
  `telemetry.do/motor/mixer/pump/temp/success/warning/danger`; add `fontFamily.mono = JetBrains Mono`.

**Library**
- `web/src/lib/types.ts` — add `RunState`, `RecipeDef`, `PhaseDef`, `PhaseProgress`,
  `Milestone` types matching WS1/WS2 payloads.
- `web/src/lib/api.ts` — add `getState()`, `postState()`, `getRecipes()`,
  `getMilestones()`, `postMilestone()`. Reuse `postCommand` unchanged.
- `web/src/lib/registry.ts` — add `readField(registry, role, field)` helper for sat/temp/motor
  KPIs, and a `readMotor()` for the aerator card (RPM/power/current).
- `web/src/lib/format.ts` — add `formatDurationClock(s)` → `HH:MM:SS` for the run timer.

**New components (`web/src/components/`)**
- `RunHeader.tsx` — brand HUD, 4-node matrix, master clock, Start/Pause/Stop, SD toggle, E-STOP.
- `KpiRibbon.tsx` — 5 cards; supersedes the reading half of `MetricCards.tsx`.
- `ObservatoryChart.tsx` — DO (left) + ODrive RPM (right, stepped dashed) + milestone scatter;
  time-window buttons + relative/clock axis toggle.
- `MultivariableInspector.tsx` — variable checkbox pills, axis-preset buttons, manual axis
  min/max panel; independent `yLeft`/`yRight`.
- `RecipePhases.tsx` — phase cards with mode toggle, params, exit condition; ports the mockup's
  `phaseCard()` renderer against `RecipeDef` from the API.
- `ActuatorDeck.tsx` — 3 actuator panels; each control maps to a `commandsFor()` verb.
- `MilestoneComposer.tsx` — the "Añadir Hito" modal → `postMilestone()`.
- `DiagnosticsTab.tsx` — restyle of `HealthPanel.tsx` content.

**Edited components**
- `CommandConsole.tsx` — keep the outbox table + safety row; fold the free-form form into
  `ActuatorDeck.tsx` (the mockup has no raw JSON target editor — verbs carry their targets).
- `EventsTable.tsx` — restyle; add `milestone_recorded` to the kind filter.
- `LevelsPanel.tsx` — restyle into the interlocks panel.

**Composition**
- `web/src/app/page.tsx` — recompose: `RunHeader` → `KpiRibbon` → two-column main
  (`ObservatoryChart` + `MultivariableInspector` / `RecipePhases` + `ActuatorDeck` +
  feed/interlocks) → footer. Keep the existing registry-gate/error handling exactly.

## Design detail

### Data flow (unchanged where possible)

- Telemetry: `useLive()` SSE at 1 Hz — **unchanged**. The mockup's 5 Hz ODrive series is a
  *log* concern, not a *screen* concern; the screen stays 1 Hz and the 5 Hz data comes from
  export/download (WS2/export), matching the mockup's own "5.0 Hz SD" download option.
- Run state: `getState()` on mount + after each transition; `useInterval` 5 s as a safety net.
- Milestones: fetched alongside events; re-fetched after a milestone is posted.
- Phases: `getRecipes()` once (static), `phase progress` from `getState()`.

### Charts — recharts specifics

- **Observatory**: two `<YAxis yAxisId="left"|"right">`, `XAxis` with a formatter that switches
  between `+MM:SS` and `HH:MM:SS` based on the axis-mode toggle. Milestones = a second
  `<Scatter>`/`<Line>` with `dot` and no stroke, `data` being `{t_rel_s, do}` points.
- **Inspector**: N `<Line>`s sharing two axes; visibility toggled by checkbox state (the mockup
  hides rather than unmounts — same here, so axis titles recompute from visible series).
- **Axis presets**: pure functions mapping preset → visible variable set; no chart re-init.
- Relative-time axis: WS2 stores `t_rel_s` on each milestone; telemetry points derive `t_rel_s`
  from `snapshot.ts - started_at`. Both share the same origin ⇒ points align.

### Visual system

- Map mockup classes to theme tokens once in `tailwind.config.js`; components use
  `bg-scada-surface`, `text-telemetry-do`, etc. This keeps a single source for the palette.
- ISA-101 tone: muted surfaces, high-contrast values, colour reserved for **state** (blue=DO,
  amber=motor, teal=mixer, violet=pump, red=E-STOP). Matches the existing `TONE` map in `ui.tsx`
  — reuse it rather than introduce a parallel scheme.

### Cut from the mockup (deliberate)

- **kLa KPI + both kLa modals** — owner confirmed out. Drop the math entirely.
- **Diagnostics tab** — the mockup hardcodes DC bus/current/MOSFET temps the gateway does not
  expose per-node. Show only fields that exist in the registry; no invented numbers.
- **"5 Hz" live chart cadence** — screen stays 1 Hz; 5 Hz is an export artefact.
- **Cover images / history modal** — no backing endpoint for a *list* of stored bundles yet, so
  the history modal is deferred (flagged below).

## Verification

- `npm run typecheck` clean. ✅
- `npm run build` → static export still succeeds (no SSR-only APIs introduced; every new
  component is `'use client'`). ✅
- Manual against the mock gateway (which now serves `/api/state`, `/api/recipes`,
  `/api/samples`, milestones): all panels render with live data. ✅

## Status — IMPLEMENTED & REVISED (2026-10-10)

First pass was too shallow and the owner rejected it. Revised against the mockup:

Components (`web/src/components/`):
- `RunHeader.tsx` — brand, **node health matrix with per-node metric lines** (Sensor OD /
  Aireador / Mezclador / Bomba, dot + accent), clock, Start/Pause/Stop, SD toggle, E-STOP.
- `KpiRibbon.tsx` — 5 KPI cards (DO hero, saturation, temp, aerator, run timer).
- `ObservatoryChart.tsx` — DO (left) + ODrive RPM (right) + **milestone dots on the DO line**;
  windows **10m/1h/24h/7d/Todo/⚙️ Personalizado** (custom modal), t-t₀/Hora toggle.
- `MultivariableInspector.tsx` — free variables on two independent axes, presets, and a
  **full axis-configuration panel** (auto-scale toggle + manual min/max per side).
- `RecipeStudio.tsx` — the mockup's **4 tabs** (Ensayo / Actuadores / Sensor / Diagnóstico).
  Ensayo = protocol selector, mode legend, and phase cards with objective (`desc`), params,
  exit condition, and three mode footers (AUTO / HÍBRIDO w/ confirm / MANUAL w/ Disparar
  Acción + Forzar Avance). Actuadores = `ActuatorDeck`. Sensor = rate + calibration + battery.
  Diagnóstico = ODrive bus + node registry.
- `ActuatorDeck.tsx` — ODrive / mixer PI / pump controls.
- `MilestoneComposer.tsx` — the "Añadir hito" modal.

Deleted (orphaned by the recomposition): `RecipePhases`, `ActuatorCards`, `MetricCards`,
`ChartsSection`, `CommandConsole`. **Manual control replaces the command console**, per the
owner — the E-STOP and safety actions live in the header.

Backend additions for the UI: `Phase.desc` on the recipe (rendered in the phase card), exposed
via `/api/recipes` and the run state.

Bug found & fixed during the revision: the observatory's DO `Line` used `connectNulls={false}`,
but the two nodes timestamp independently, so every DO point was split by an RPM-only point
and the line drew nothing. Now `connectNulls`.

Verified: `npm run typecheck` clean; `npm run build` static export succeeds; rendered
in-browser against the mock gateway — all four tabs, both charts, phase cards, the axis-config
panel, the custom-window modal, and the node matrix confirmed with live data.

## Risks / open questions

- **Scope**: the mockup is a full re-skin + 7 new components. Plan is to do it against the
  **stable WS1/WS2 API** (gateway-first), so the UI never mocks data. Confirm ordering.
- **`ExperimentsPanel` export**: the mockup's "1 Hz / 5 Hz / dataset" download is three buttons;
  the gateway's `exportExperiment` produces one bundle. Plan: keep the bundle export and label
  the buttons by what the backend actually emits (1 Hz aligned bundle; raw 5 Hz archive; events
  overlay). Confirm labels matter to you.
- **Node matrix fields**: mockup shows `4.12V`, `24.2V/34°C`, mixer `PI ACTIVO • 1200 RPM`.
  Only some exist as registry fields (`pwr_battery_v`, ODrive bus voltage/temp, mixer rpm). The
  matrix shows only real fields — the rest render as `—`.
- **Deferred**: bundle-history list modal (needs a `GET /api/experiments/{id}/exports` endpoint
  that does not exist). Listed as a follow-up, not in this pass.
- **Timezone**: footer claims `GMT-5 Lima`; keep `PROJECT_TZ_LABEL` (`America/Lima`) as the
  single source, as the current footer already does.
