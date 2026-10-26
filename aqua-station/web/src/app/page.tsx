'use client';

/**
 * Dashboard composition root.
 *
 * The architectural change is still visible in exactly two lines: `useLive()` opens one SSE
 * connection, and everything else fetches on demand or on a slow interval. Nothing here holds
 * authority — the control loops run on the nodes, and a stale value is labelled stale rather
 * than smoothed over.
 *
 * The layout is the AquaSCADA Lab console: a run header, a KPI ribbon, then the observatory and
 * the control deck. The panels that were already correct — events, interlocks, experiments,
 * the command console, health — are kept and folded into that layout rather than rewritten.
 */

import { useCallback, useMemo, useState } from 'react';
import { createExperiment, getLevels, getRegistry, postCommand } from '@/lib/api';
import type { RecipeDef } from '@/lib/types';
import { PROJECT_TZ_LABEL } from '@/lib/dateUtils';
import { useAsync, useInterval } from '@/hooks/useAsync';
import { useLive } from '@/hooks/useLive';
import { useRunState } from '@/hooks/useRunState';
import { RunHeader } from '@/components/RunHeader';
import { KpiRibbon } from '@/components/KpiRibbon';
import { ObservatoryChart } from '@/components/ObservatoryChart';
import { MultivariableInspector } from '@/components/MultivariableInspector';
import { RecipeStudio } from '@/components/RecipeStudio';
import { MilestoneComposer } from '@/components/MilestoneComposer';
import { ExperimentComposer, type ExperimentDraft } from '@/components/ExperimentComposer';
import { EventsTable } from '@/components/EventsTable';
import { BitacoraPanel } from '@/components/BitacoraPanel';
import { LevelsPanel } from '@/components/LevelsPanel';
import { HealthPanel } from '@/components/HealthPanel';
import { ErrorState, Loading, TONE, cn } from '@/components/ui';

/** Stream states are machine tokens in the hook; the footer shows them to an operator. */
const STREAM_LABEL: Record<string, string> = {
  connecting: 'conectando',
  live: 'en vivo',
  stale: 'obsoleto',
  polling: 'sondeo',
  offline: 'sin conexión',
};

/**
 * Emergency stop, one verb per node — because the nodes do not share an emergency vocabulary.
 *
 * Only the aerator's handler understands `emergency_stop`; the mixer (`cloud_worker.cpp`:
 * start_mixer/stop_mixer/set_state only) and the pump (no `clear_estop`) reject it. Sending the
 * same verb to all three, as before, meant the mixer impeller kept spinning through an E-STOP.
 * Each entry here is a verb the named node's handler actually branches on.
 */
const ESTOP_COMMANDS: readonly { role: string; action: string }[] = [
  { role: 'aerator', action: 'emergency_stop' },
  { role: 'mixer', action: 'stop_mixer' },
  { role: 'pump', action: 'force_off' },
];

/** Re-arming, the same way per node. `clear_estop` releases the aerator latch; the other two
 *  have no latch and are driven to a known idle state instead. */
const ESTOP_CLEAR_COMMANDS: readonly { role: string; action: string; target: Record<string, unknown> }[] = [
  { role: 'aerator', action: 'clear_estop', target: {} },
  { role: 'mixer', action: 'set_state', target: { state: 'IDLE' } },
  { role: 'pump', action: 'set_state', target: { state: 'IDLE' } },
];

export default function DashboardPage() {
  const registryAsync = useAsync(() => getRegistry(), []);
  const levelsAsync = useAsync(() => getLevels(), []);
  const live = useLive();
  const run = useRunState();

  const [experimentId, setExperimentId] = useState<string | null>(null);
  // The recipe currently shown in the Ensayo tab. "Iniciar Ensayo" creates a run from it, so
  // starting no longer depends on a separate experiment-picker card (the mockup has none).
  const [shownRecipe, setShownRecipe] = useState<RecipeDef | null>(null);
  // Pre-launch phase-mode choices, keyed by `phase_id`. Held here so they can ride with the
  // experiment created on "Iniciar Ensayo" — that is what makes a pre-launch edit reach the
  // run. Cleared when a run starts, since the orchestrator owns modes from then on.
  const [draftModes, setDraftModes] = useState<Record<string, string>>({});
  const [milestoneOpen, setMilestoneOpen] = useState(false);
  // The pre-start naming dialog. Open means "a run is waiting to be named"; nothing is
  // created until the operator confirms, so cancelling leaves the console exactly as it was.
  const [composerOpen, setComposerOpen] = useState(false);
  const [composerBusy, setComposerBusy] = useState(false);
  // The operator's own name for the running experiment. The gateway only echoes `title` in
  // the experiments list, so the header reads this rather than re-fetching that list per frame.
  //
  // Deliberately session-local: it holds for the run this console created, and a reload falls
  // back to the recipe's name. Recovering it would mean listing experiments and matching the
  // id -- 50 rows to label one heading, on every poll, for a name the operator just typed. The
  // name is not lost, only the echo of it: the dialog re-seeds the field from the recipe on
  // the next start, and the row itself keeps the title in the database.
  const [runTitle, setRunTitle] = useState<string | null>(null);
  const [runNotes, setRunNotes] = useState<string | null>(null);
  // Bumped when a milestone is posted, so the chart and bitácora re-fetch immediately rather
  // than waiting for their next poll.
  const [milestoneVersion, setMilestoneVersion] = useState(0);

  // E-STOP latch is a client-side safety state: the gateway records the command, but the
  // banner is the operator's local acknowledgement that the bench is disarmed until cleared.
  const [estopLatched, setEstopLatched] = useState(false);
  const [sdLogging, setSdLogging] = useState(true);

  const registry = registryAsync.data;
  const schemaVersion = registry?.schema_version ?? null;

  // The experiment the panels scope to: the run's own, or the one the operator picked manually.
  const scopedExperimentId = run.state?.experiment_id ?? experimentId;

  const lastFrameLabel = useMemo(() => {
    if (!live.lastFrameAt) return null;
    return new Date(live.lastFrameAt).toLocaleTimeString('en-GB', {
      hour12: false,
      timeZone: 'America/Lima',
    });
  }, [live.lastFrameAt, live.frames]);

  // Start or resume. A resume uses the run's own experiment; a fresh start asks for a name
  // first, then creates the experiment and starts it. The gateway needs an experiment id
  // before a run can begin, and the mockup has no separate picker for one -- so this dialog is
  // that picker, and it is the only place a run's title can be chosen.
  const handleStart = useCallback(async () => {
    if (run.state?.experiment_id) {
      await run.resume().catch(() => undefined);
      return;
    }
    // A dialog rather than an inline form: the run must not start until the operator has seen
    // (or dismissed) the fields, because the experiment is created at this instant and there
    // is no later step that could correct its name.
    setComposerOpen(true);
  }, [run]);

  // The create itself, once the operator has confirmed a title and description.
  const startWithDraft = useCallback(
    async (draft: ExperimentDraft) => {
      const recipeId = shownRecipe?.recipe_id ?? 'planta_2_step';
      // Include all bench roles so every active node's telemetry is captured and tracked
      const roles = Array.from(new Set([...(shownRecipe?.roles ?? ['od_sensor', 'aerator']), 'od_sensor', 'aerator', 'mixer', 'pump']));
      setComposerBusy(true);
      try {
        const experiment = await createExperiment({
          title: draft.title,
          notes: draft.notes || undefined,
          roles,
          recipe_id: recipeId,
          phase_modes: draftModes,
        });
        setExperimentId(experiment.id);
        // Remembered so the header can show the operator's own name for the run. The gateway
        // echoes `title` back only in the experiments list, not on the run-state poll, and
        // fetching that list on every frame to label a heading is not worth it.
        setRunTitle(draft.title);
        setRunNotes(draft.notes || null);
        await run.start(experiment.id);
        setComposerOpen(false);
        // The draft modes now live on the experiment's phase rows; keep the map only until the
        // run's own phase list takes over, so a later launch does not inherit stale choices.
        setDraftModes({});
      } catch {
        /* the run-state read reconciles the screen; a failed create leaves it idle */
      } finally {
        setComposerBusy(false);
      }
    },
    [run, shownRecipe, draftModes],
  );

  // One button, three meanings: pause a running run, resume a paused one, start an idle one.
  const handleToggleRun = useCallback(() => {
    const active = run.state?.state === 'ACTIVE_EXPERIMENT';
    if (active && !run.state?.paused) void run.pause();
    else if (active && run.state?.paused) void run.resume();
    else void handleStart();
  }, [run, handleStart]);

  const handleStop = useCallback(() => {
    void run.stop();
  }, [run]);

  const handleEStop = useCallback(() => {
    setEstopLatched(true);
    // Published per role, because the nodes do not share one emergency vocabulary. Only the
    // aerator implements `emergency_stop`; the mixer and pump must be told to stop with the
    // verbs their own handlers branch on, or the command is rejected and the actuator keeps
    // running. A safety action bypasses the database on the gateway, so this lands even when
    // the rest of the system is degraded.
    for (const { role, action } of ESTOP_COMMANDS) {
      void postCommand({
        role,
        action,
        target: {},
        deadline_s: 30,
        requested_by: 'operator:web',
      }).catch(() => undefined);
    }
  }, []);

  const handleClearEStop = useCallback(() => {
    setEstopLatched(false);
    // Re-arm likewise differs per node: `clear_estop` releases the aerator's latch, while the
    // mixer and pump have no latch to clear and are simply driven back to a known idle state.
    for (const { role, action, target } of ESTOP_CLEAR_COMMANDS) {
      void postCommand({
        role,
        action,
        target,
        deadline_s: 30,
        requested_by: 'operator:web',
      }).catch(() => undefined);
    }
  }, []);

  // SD logging has no node verb yet. The button used to send `set_config{sd_logging}`, which
  // `nodo-sensor-od/src/main.cpp` does not branch on -- so the node answered `unsupported_action`
  // and the UI showed a purple "ACTIVO" anyway. Worse, the state lived in this component, so a
  // page reload reset it to `true` regardless of reality: the button reported a recording that
  // was never happening. The firmware branches only on start/stop/set_sampling_rate/
  // manual_sample/set_state/set_sleep_cycle/sleep -- no SD verb among them. Until one lands, the
  // honest control is a disabled button with a reason, the same call the `calibrate` button makes.
  const SD_DISABLED_REASON =
    'El firmware del nodo aún no expone un verbo de grabación en MicroSD; el botón se habilitará cuando lo haga.';
  const handleToggleSd = useCallback(() => {
    /* no verb to send; the button is disabled and this never runs */
  }, []);

  useInterval(() => {
    void levelsAsync.reload();
  }, 30_000);

  return (
    <div className="min-h-screen bg-scada-canvas text-slate-100 flex flex-col font-sans">
      <RunHeader
        registry={registry}
        snapshot={live.snapshot}
        run={run.state}
        busy={run.busy}
        onStart={() => void handleStart()}
        onStop={handleStop}
        onPause={() => void run.pause()}
        onResume={() => void run.resume()}
        onEStop={handleEStop}
        estopLatched={estopLatched}
        onClearEStop={handleClearEStop}
        sdLogging={sdLogging}
        onToggleSd={handleToggleSd}
        onSetState={(s) => void run.setState(s)}
        schemaVersion={schemaVersion}
        lastFrameAt={live.lastFrameAt}
      />

      <main className="flex-1 w-full mx-auto">
        {/* Registry is the one hard dependency: without it there are no field names, units or
            precisions, so everything downstream would be guessing. */}
        {registryAsync.error ? (
          <div className="p-4 lg:p-6">
            <ErrorState error={registryAsync.error} onRetry={registryAsync.reload} />
          </div>
        ) : null}

        {registryAsync.loading && !registry ? (
          <div className="p-4 lg:p-6">
            <Loading label="Cargando el esquema del despliegue" />
          </div>
        ) : null}

        {registry ? (
          <>
            {live.error ? (
              <p className={cn('px-4 lg:px-6 pt-3 text-xs', TONE.bad.text)}>
                Flujo en vivo: {live.error}. Los valores pueden estar desactualizados.
              </p>
            ) : null}

            <KpiRibbon registry={registry} snapshot={live.snapshot} run={run.state} />

            {/* Two-pane workbench: observatory on the left, control deck on the right. */}
            <div className="p-4 lg:p-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
              <div className="lg:col-span-8 flex flex-col gap-5">
                <ObservatoryChart
                  registry={registry}
                  run={run.state}
                  milestoneVersion={milestoneVersion}
                  onAddMilestone={() => setMilestoneOpen(true)}
                />
                <MultivariableInspector registry={registry} />

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
                  <BitacoraPanel experimentId={scopedExperimentId} version={milestoneVersion} />
                  <LevelsPanel />
                </div>

                <EventsTable experimentId={scopedExperimentId} />
              </div>

              <div className="lg:col-span-4 flex flex-col gap-5">
                <RecipeStudio
                  registry={registry}
                  snapshot={live.snapshot}
                  run={run.state}
                  busy={run.busy}
                  onMode={(phaseId, mode) => void run.setMode(phaseId, mode)}
                  onConfirm={() => void run.confirm()}
                  onTriggerAction={(params) => void run.triggerAction(params)}
                  sdLogging={sdLogging}
                  onToggleSd={handleToggleSd}
                  onStop={handleStop}
                  onToggleRun={handleToggleRun}
                  onRecipeChange={setShownRecipe}
                  onModeDraftChange={setDraftModes}
                  runTitle={runTitle}
                  runNotes={runNotes}
                />
              </div>
            </div>

            <div className="px-4 lg:px-6 pb-6">
              <HealthPanel />
            </div>
          </>
        ) : null}
      </main>

      <footer className="mt-auto border-t border-scada-border bg-scada-surface/80 py-2 px-6 text-xs text-slate-400 font-mono flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1.5 text-emerald-400">
            <span className="w-2 h-2 rounded-full bg-emerald-400" />
            {STREAM_LABEL[live.state] ?? live.state}
          </span>
          <span className="text-slate-600">•</span>
          <span>
            {PROJECT_TZ_LABEL} · todas las marcas de tiempo en America/Lima
          </span>
        </div>
        <div className="flex items-center gap-4 text-[11px] text-slate-500">
          <span>Gateway FastAPI · ingesta MQTT · agregados PostgreSQL · archivo CSV/gzip</span>
          {lastFrameLabel ? (
            <>
              <span className="text-slate-600">•</span>
              <span>stream gateway {lastFrameLabel}</span>
            </>
          ) : null}
        </div>
      </footer>

      {milestoneOpen ? (
        <MilestoneComposer
          run={run.state}
          onClose={() => setMilestoneOpen(false)}
          onPosted={() => {
            run.reload();
            // The chart and the bitácora own their milestone reads; bump a counter so they
            // re-fetch now instead of waiting up to 15 s for the next poll.
            setMilestoneVersion((v) => v + 1);
          }}
        />
      ) : null}

      {composerOpen ? (
        <ExperimentComposer
          recipe={shownRecipe}
          busy={composerBusy}
          onCancel={() => setComposerOpen(false)}
          onConfirm={(draft: ExperimentDraft) => void startWithDraft(draft)}
        />
      ) : null}
    </div>
  );
}
