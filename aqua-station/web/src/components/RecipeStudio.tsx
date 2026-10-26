'use client';

/**
 * The recipe studio — the mockup's 4-tab control deck.
 *
 * Tabs: **Ensayo** (the phased protocol), **Actuadores** (manual node control), **Sensor**
 * (acquisition + calibration), **Diagnóstico** (bus/RTC registers). The protocol tab is the
 * spine: phase cards with a description, per-phase parameters, an exit condition, and a mode
 * that decides how the run advances.
 *
 * The mode footers are the point, and they differ by more than a colour. AUTO (autonomous),
 * HÍBRIDO (supervised — a confirm button appears once the condition is met), and MANUAL (the
 * operator fires the action and forces the advance). Rendering all three and revealing one is
 * what makes "which mode am I in" answerable at a glance, which is the whole reason the mockup
 * puts them there.
 */

import { useEffect, useState } from 'react';
import {
  Activity,
  ChevronDown,
  FlaskConical,
  Hand,
  HardDrive,
  Play,
  SlidersHorizontal,
  Zap,
} from 'lucide-react';
import { getRecipes } from '@/lib/api';
import { useAsync } from '@/hooks/useAsync';
import { getODriveHumanStatus } from '@/lib/odriveStatus';
import { postCommand } from '@/lib/api';
import type { LiveSnapshot, PhaseMode, PhaseProgress, RecipeDef, Registry, RunStateResponse } from '@/lib/types';
import { readField } from '@/lib/registry';
import { formatNumber } from '@/lib/format';
import { ActuatorDeck } from '@/components/ActuatorDeck';
import { ExperimentActions } from '@/components/ExperimentActions';
import { PhaseForm } from '@/components/PhaseForm';
import { cn } from './ui';

const MODE_STYLE: Record<PhaseMode, { cls: string; label: string }> = {
  auto: { cls: 'bg-emerald-950/80 text-emerald-400 border-emerald-700', label: 'AUTO' },
  hibrido: { cls: 'bg-sky-950/80 text-sky-400 border-sky-700', label: 'HÍBRIDO' },
  manual: { cls: 'bg-amber-950/80 text-amber-400 border-amber-700', label: 'MANUAL' },
};

const STATUS_STYLE: Record<string, { cls: string; label: string }> = {
  active: { cls: 'bg-emerald-950/80 text-emerald-400 border border-emerald-800', label: 'ACTIVA' },
  awaiting_confirm: { cls: 'bg-sky-950/80 text-sky-400 border border-sky-800', label: 'ESPERA OK' },
  completed: { cls: 'bg-sky-950/80 text-sky-400 border border-sky-800', label: 'COMPLETADA' },
  pending: { cls: 'bg-slate-800 text-slate-400 border border-slate-700', label: 'EN ESPERA' },
  skipped: { cls: 'bg-slate-800 text-slate-400 border border-slate-700', label: 'SALTADA' },
};

const TAB_DEFS = [
  { id: 'ensayo', label: 'Ensayo' },
  { id: 'actuadores', label: 'Actuadores' },
  { id: 'sensor', label: 'Sensor' },
  { id: 'diag', label: 'Diagnóstico' },
] as const;
type TabId = (typeof TAB_DEFS)[number]['id'];

function ModeButtons({
  phaseId,
  current,
  disabled,
  disabledReason,
  onSelect,
}: {
  phaseId: string;
  current: PhaseMode;
  disabled: boolean;
  /**
   * Why the buttons are inert, when they are.
   *
   * A greyed control with no explanation reads as a bug — which is exactly how this was once
   * reported ("no podía editar un experimento antes de lanzarlo"). The button is only inert
   * during an in-flight transition now: before launch the operator *can* set each phase's mode,
   * and the choice is carried into the experiment's phase rows so the run starts with it.
   */
  disabledReason?: string;
  onSelect: (mode: PhaseMode) => void;
}) {
  return (
    <div className="flex gap-1.5">
      {(['auto', 'hibrido', 'manual'] as PhaseMode[]).map((mode) => (
        <button
          key={mode}
          type="button"
          disabled={disabled}
          onClick={() => onSelect(mode)}
          title={disabled ? disabledReason : `Fijar esta fase en modo ${MODE_STYLE[mode].label}`}
          className={cn(
            'flex-1 py-1 text-[10px] font-bold rounded border font-mono transition-all',
            current === mode
              ? cn(MODE_STYLE[mode].cls, 'ring-1 ring-current')
              : 'bg-scada-elevated text-slate-400 border-scada-border hover:text-slate-200',
            disabled && 'opacity-50 cursor-not-allowed',
          )}
        >
          {MODE_STYLE[mode].label}
        </button>
      ))}
    </div>
  );
}

/** Renders one phase with its three mode footers, only one of which is visible. */
function PhaseCard({
  phase,
  busy,
  onMode,
  onParam,
  onConfirm,
  onManualAction,
  modeDisabledReason,
}: {
  phase: PhaseProgress;
  busy: boolean;
  onMode: (mode: PhaseMode) => void;
  onParam: (key: string, value: number | string | boolean) => void;
  onConfirm: () => void;
  onManualAction: () => void;
  /** Shown as a tooltip on the inert mode buttons; empty when they are live. */
  modeDisabledReason?: string;
}) {
  const [open, setOpen] = useState(
    phase.status === 'active' || phase.status === 'awaiting_confirm',
  );
  const status = STATUS_STYLE[phase.status] ?? STATUS_STYLE.pending;
  const awaiting = phase.status === 'awaiting_confirm';

  return (
    <div
      className={cn(
        'border rounded-xl overflow-hidden bg-scada-canvas shadow-sm',
        awaiting ? 'border-sky-500/50' : 'border-scada-border',
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2 bg-scada-surface border-b border-scada-border hover:bg-scada-elevated/60 transition text-left"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-mono text-[10px] font-bold text-slate-500 bg-scada-well px-1.5 py-0.5 rounded border border-scada-border shrink-0">
            {phase.num}
          </span>
          <span
            className={cn(
              'w-2 h-2 rounded-full shrink-0',
              phase.status === 'active'
                ? 'bg-emerald-400'
                : phase.status === 'completed'
                  ? 'bg-sky-400'
                  : 'bg-slate-500',
            )}
          />
          <span className="text-xs font-bold text-slate-100">{phase.title}</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className={cn('text-[10px] font-mono px-1.5 py-0.5 rounded', status.cls)}>
            {status.label}
          </span>
          <ChevronDown className={cn('w-3.5 h-3.5 text-slate-500 transition-transform', open && 'rotate-180')} />
        </div>
      </button>

      {open ? (
        <div className="p-3 space-y-3">
          {/* Objective — the mockup leads each phase with what it is for. The recipe ships a
              little inline markup (`<strong>`), so it is rendered as HTML. */}
          {phase.desc ? (
            <div
              className="p-2 rounded bg-scada-well/80 border border-scada-border text-[11px] text-slate-400 leading-relaxed [&_strong]:text-slate-200"
              dangerouslySetInnerHTML={{ __html: phase.desc }}
            />
          ) : null}

          {/* Mode selector */}
          <div>
            <div className="text-[10px] text-slate-500 uppercase font-mono mb-1.5 tracking-wider">
              Modo de Operación de la Fase:
            </div>
            <ModeButtons
              phaseId={phase.phase_id}
              current={phase.mode}
              disabled={busy}
              disabledReason={modeDisabledReason}
              onSelect={onMode}
            />
          </div>

          {/* Per-phase parameters, as the mockup's bespoke control surface: sections, number
              fields, selects, switches and static badges. Driven by the declarative spec in
              `PhaseForm`, with edited values held by RecipeStudio. */}
          <PhaseForm phaseId={phase.phase_id} params={phase.params ?? {}} onParam={onParam} />

          {/* Exit condition */}
          <div className="border-t border-scada-border pt-2.5">
            <div className="text-[10px] text-slate-500 uppercase font-mono mb-1 tracking-wider">
              Condición para salir de esta fase:
            </div>
            <div className="text-[11px] text-slate-300 font-mono bg-scada-well rounded px-2.5 py-1.5 border border-scada-border">
              {phase.condition_text}
              <span className="text-slate-500">
                {/* A null timeout is a supervised phase waiting for its operator, not a
                    missing value, so it reads as a statement rather than "nulls". */}
                {phase.timeout_s === null
                  ? ' · sin timeout (espera al operador)'
                  : ` · timeout ${phase.timeout_s}s`}
              </span>
            </div>
          </div>

          {/* Footer 1: AUTO */}
          <div className={cn('space-y-1.5', phase.mode === 'auto' ? '' : 'hidden')}>
            <div className="p-2 rounded bg-emerald-950/40 border border-emerald-800/60 text-emerald-300 text-[10px] font-mono flex items-center justify-between">
              <span className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                <strong>Modo Auto:</strong> Orquestador aplica consigna y avanza de fase
                automáticamente.
              </span>
              <span className="text-[9px] bg-emerald-900/60 px-1.5 py-0.5 rounded border border-emerald-700">
                AUTÓNOMO
              </span>
            </div>
          </div>

          {/* Footer 2: HÍBRIDO */}
          <div className={cn('space-y-2', phase.mode === 'hibrido' ? '' : 'hidden')}>
            <div className="p-2 rounded bg-sky-950/40 border border-sky-800/60 text-sky-300 text-[10px] font-mono flex items-center justify-between">
              <span className="flex items-center gap-1.5">
                <SlidersHorizontal className="w-3.5 h-3.5 text-sky-400" />
                <strong>Modo Híbrido:</strong> Al cumplirse la condición, requiere visto bueno
                del operador.
              </span>
              <span className="text-[9px] bg-sky-900/60 px-1.5 py-0.5 rounded border border-sky-700">
                SUPERVISADO
              </span>
            </div>
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy || !awaiting}
              className={cn(
                'w-full py-2 rounded-lg font-bold text-xs flex items-center justify-center gap-1.5 transition',
                awaiting
                  ? 'bg-sky-600 hover:bg-sky-500 text-white active:scale-95 shadow-sm'
                  : 'bg-scada-elevated text-slate-500 border border-scada-border cursor-not-allowed',
              )}
            >
              <Play className="w-3.5 h-3.5" /> Visto Bueno / Confirmar y Pasar a Siguiente Fase
            </button>
          </div>

          {/* Footer 3: MANUAL */}
          <div className={cn('space-y-2', phase.mode === 'manual' ? '' : 'hidden')}>
            <div className="p-2 rounded bg-amber-950/40 border border-amber-800/60 text-amber-300 text-[10px] font-mono flex items-center justify-between">
              <span className="flex items-center gap-1.5">
                <Hand className="w-3.5 h-3.5 text-amber-400" />
                <strong>Modo Manual:</strong> Control directo. No avanza automáticamente por
                umbral.
              </span>
              <span className="text-[9px] bg-amber-900/60 px-1.5 py-0.5 rounded border border-amber-700">
                OPERADOR AL MANDO
              </span>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={onManualAction}
                className="py-2 bg-amber-600 hover:bg-amber-500 text-white rounded-lg font-bold text-xs flex items-center justify-center gap-1 transition active:scale-95 disabled:opacity-50"
              >
                <Zap className="w-3.5 h-3.5" /> Disparar Acción
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={onConfirm}
                className="py-2 bg-scada-elevated hover:bg-slate-700 text-amber-300 border border-amber-500/40 rounded-lg font-bold text-xs flex items-center justify-center gap-1 transition active:scale-95 disabled:opacity-50"
              >
                <ChevronDown className="w-3.5 h-3.5 rotate-[-90deg]" /> Forzar Avance
              </button>
            </div>
          </div>

          {/* Post-Deox extension: continue DO logging */}
          {phase.phase_id === 'p1_end' || phase.phase_id === 'pd_trans' ? (
            <div className="pt-2 border-t border-scada-border/80">
              <button
                type="button"
                disabled={busy}
                onClick={async () => {
                  try {
                    await postCommand({ role: 'od_sensor', action: 'start', target: { interval_sec: 5 } });
                    onMode('manual');
                  } catch {
                    /* ignored */
                  }
                }}
                className="w-full py-2 bg-teal-950/70 hover:bg-teal-900/80 text-teal-300 border border-teal-700/80 rounded-lg font-bold text-xs flex items-center justify-center gap-1.5 transition active:scale-95 shadow-sm"
                title="Mantiene activo el muestreo de alta fidelidad del sensor de OD y pone la etapa en modo manual para continuar el registro sin apagar el ensayo"
              >
                <Activity className="w-3.5 h-3.5 text-teal-400" />
                Continuar con Loggeo de OD (Extender Registro)
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function SensorTab({ registry, snapshot }: { registry: Registry | null; snapshot: LiveSnapshot | null }) {
  // 5 s is the bench default (the same value `AQUA_MQTT`/modbus cadence and the recipes'
  // `interval_sec` use). It was hard-coded to 1 here, so the tab opened on a rate the operator
  // never chose and the first click was always needed to correct it.
  const [rate, setRate] = useState(5);
  // The rate the node has actually acknowledged, so an issued change can be shown as "pending"
  // until `observed_hz` agrees rather than silently appearing to have taken effect. The node
  // reports Hz, not the interval, so the expected rate is 1/interval and the comparison is a
  // tolerance on that.
  const [pendingRate, setPendingRate] = useState<number | null>(null);
  const battery = readField(registry, snapshot, 'od_sensor', 'pwr_battery_v');
  const observed = snapshot?.nodes?.['od_sensor']?.observed_hz;

  // Clear the pending mark once the measured rate matches what was asked for, or after a
  // bounded wait so a refused command does not pulse forever.
  useEffect(() => {
    if (pendingRate === null) return;
    const expected = 1 / pendingRate;
    if (observed != null && Math.abs(observed - expected) <= expected * 0.25) {
      setPendingRate(null);
      return;
    }
    const timer = setTimeout(() => setPendingRate(null), 6_000);
    return () => clearTimeout(timer);
  }, [observed, pendingRate]);

  const setSampling = (sec: number) => {
    setRate(sec);
    setPendingRate(sec);
    void postCommand({
      role: 'od_sensor',
      action: 'set_sampling_rate',
      target: { interval_sec: sec },
      deadline_s: 120,
      requested_by: 'operator:web',
    }).catch(() => setPendingRate(null));
  };

  return (
    <div className="space-y-4">
      <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-bold text-white">Frecuencia de adquisición</span>
          <span className="text-[10px] font-mono text-emerald-400">
            {observed == null ? 'SD —' : `${observed.toFixed(2)} Hz real`}
          </span>
        </div>
        <div>
          <label className="text-[11px] text-slate-400 font-medium block mb-1">Intervalo Modbus RS485:</label>
          <div className="grid grid-cols-4 gap-1.5 font-mono text-xs">
            {[1, 2, 5, 15].map((sec) => (
              <button
                key={sec}
                type="button"
                onClick={() => setSampling(sec)}
                className={cn(
                  'py-1.5 rounded transition',
                  rate === sec
                    ? 'bg-scada-elevated text-telemetry-do border border-telemetry-do/40 font-bold'
                    : 'bg-slate-800 text-slate-400 border border-scada-border',
                  // Pulse while the node has not yet confirmed the change, so an issued
                  // command reads as "sending" rather than as instantly applied.
                  pendingRate === sec && 'animate-pulse ring-1 ring-telemetry-do/60',
                )}
                title={
                  pendingRate === sec
                    ? 'Comando enviado — esperando que el nodo confirme la nueva cadencia'
                    : undefined
                }
              >
                {sec}s
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-2">
        <span className="text-xs font-bold text-slate-200 block">Calibración de campo (2 puntos):</span>
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            disabled
            title="El firmware del nodo aún no expone un verbo de calibración; el botón se habilitará cuando lo haga."
            className="py-2 px-2 bg-scada-elevated text-slate-500 rounded border border-scada-border text-xs font-medium opacity-50 cursor-not-allowed"
          >
            1. Cero (0.0 mg/L)
          </button>
          <button
            type="button"
            disabled
            title="El firmware del nodo aún no expone un verbo de calibración; el botón se habilitará cuando lo haga."
            className="py-2 px-2 bg-scada-elevated text-slate-500 rounded border border-scada-border text-xs font-medium opacity-50 cursor-not-allowed"
          >
            2. 100% aire sat (7.15 mg/L)
          </button>
        </div>
        {/* ponytail: disabled until `calibrate` exists in nodo-sensor-od firmware (main.cpp
            handles start/stop/set_sampling_rate/manual_sample/set_state only). Sending it today
            would be silently dropped, which reads as success to the operator — worse than a
            greyed button. Wire up when the verb lands; the target shape is already right. */}
        <p className="text-[10px] text-slate-500">
          Pendiente de verbo <span className="font-mono">calibrate</span> en firmware.
        </p>
      </div>

      <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-2 font-mono text-xs">
        <span className="text-xs font-bold text-slate-200 block">Batería & RTC del sensor:</span>
        <div className="flex justify-between text-slate-400 py-1 border-b border-scada-border">
          <span>Voltaje batería:</span>
          <span className="text-emerald-400 font-bold">
            {formatNumber(battery.value, 2)} V
          </span>
        </div>
        <div className="flex justify-between text-slate-400 py-1">
          <span>Frecuencia real:</span>
          <span className="text-telemetry-do font-bold">
            {observed == null ? '—' : `${observed.toFixed(3)} Hz`}
          </span>
        </div>
      </div>
    </div>
  );
}

function DiagnosticsTab({ registry, snapshot }: { registry: Registry | null; snapshot: LiveSnapshot | null }) {
  const vbus = readField(registry, snapshot, 'aerator', 'drv_vbus_v');
  const ibus = readField(registry, snapshot, 'aerator', 'drv_ibus_a');
  const torque = readField(registry, snapshot, 'aerator', 'drv_torque_nm');
  const mode = snapshot?.nodes?.['aerator']?.metrics?.['ctl_mode'];
  const failsafe = snapshot?.nodes?.['aerator']?.metrics?.['ctl_failsafe'];
  const battery = readField(registry, snapshot, 'od_sensor', 'pwr_battery_v');
  const observed = snapshot?.nodes?.['od_sensor']?.observed_hz;

  const aero = snapshot?.nodes?.['aerator'];
  const aeroState = typeof aero?.metrics?.['drv_state'] === 'number' ? Number(aero.metrics['drv_state']) : null;
  const aeroStatus = typeof aero?.metrics?.['drv_status'] === 'number' ? Number(aero.metrics['drv_status']) : null;
  const aeroDisarmReason = typeof aero?.metrics?.['drv_disarm_reason'] === 'number' ? Number(aero.metrics['drv_disarm_reason']) : null;
  const aeroRunning = Boolean(aero?.metrics?.['drv_running']);

  const odriveHuman = getODriveHumanStatus({
    online: Boolean(aero?.online),
    running: aeroRunning,
    state: aeroState,
    statusCode: aeroStatus,
    disarmReason: aeroDisarmReason,
    failsafe: Boolean(failsafe),
    vbus: vbus.value,
  });

  const row = (label: string, value: string, cls = 'text-white') => (
    <div className="flex justify-between text-slate-400 py-1 border-b border-scada-border last:border-b-0">
      <span>{label}</span>
      <span className={cn('font-bold', cls)}>{value}</span>
    </div>
  );

  // The mockup shows a MOSFET temperature here. The registry carries no such field for the
  // aerator, so it is omitted rather than faked — an invented fault reading is worse than a
  // missing row. The rows below are every bus quantity the node actually publishes.
  const failsafeOn = failsafe === true || failsafe === 1 || failsafe === 'true';

  return (
    <div className="space-y-3 font-mono text-xs">
      <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-1">
        <span className="text-xs font-bold text-slate-200 block mb-1">Diagnóstico ODrive S1 (M8325s):</span>
        {row('Estado eje (axis0)', odriveHuman.stateLabel, odriveHuman.badgeClass)}
        {row(
          'Errores activos',
          odriveHuman.hasErrors
            ? odriveHuman.errors.map((e) => e.label).join(' | ')
            : 'Sin errores (0x00)',
          odriveHuman.hasErrors ? 'text-rose-400' : 'text-emerald-400',
        )}
        {row(
          'Causa de desarme',
          odriveHuman.disarmReason
            ? `${odriveHuman.disarmReason.label} (0x${odriveHuman.disarmReason.mask.toString(16).toUpperCase()})`
            : 'Ninguna (0x00)',
          odriveHuman.disarmReason ? 'text-amber-400' : 'text-slate-400',
        )}
        {row('Tensión bus DC', `${formatNumber(vbus.value, 2)} V`, 'text-white')}
        {row('Corriente medida', `${formatNumber(ibus.value, 2)} A`, 'text-white')}
        {row('Torque estimado', `${formatNumber(torque.value, 3)} N·m`, 'text-white')}
        {row(
          'Modo de control',
          typeof mode === 'string' ? mode.toUpperCase() : mode == null ? '—' : String(mode),
          'text-emerald-400',
        )}
        {row(
          'Failsafe',
          failsafe == null ? '—' : failsafeOn ? 'ACTUADO' : 'libre',
          failsafeOn ? 'text-rose-400' : 'text-emerald-400',
        )}
      </div>

      <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-1">
        <span className="text-xs font-bold text-slate-200 block mb-1">Batería & RTC del sensor:</span>
        {row('Voltaje batería', `${formatNumber(battery.value, 2)} V`, 'text-emerald-400')}
        {row('Frecuencia real', observed == null ? '—' : `${observed.toFixed(3)} Hz`, 'text-telemetry-do')}
      </div>

      <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-1">
        <span className="text-xs font-bold text-slate-200 block mb-1">Nodos registrados:</span>
        {(registry ? Object.entries(registry.roles) : []).map(([role, def]) =>
          row(
            def.display_name,
            snapshot?.nodes?.[role]?.online ? 'en línea' : 'sin conexión',
            snapshot?.nodes?.[role]?.online ? 'text-emerald-400' : 'text-rose-400',
          ),
        )}
      </div>
    </div>
  );
}

export function RecipeStudio({
  registry,
  snapshot,
  run,
  busy,
  onMode,
  onConfirm,
  onTriggerAction,
  sdLogging,
  onToggleSd,
  onStop,
  onToggleRun,
  onRecipeChange,
  onModeDraftChange,
  runTitle,
  runNotes,
}: {
  registry: Registry | null;
  snapshot: LiveSnapshot | null;
  run: RunStateResponse | null;
  busy: boolean;
  onMode: (phaseId: string, mode: PhaseMode) => void;
  onConfirm: () => void;
  /** Fire the active MANUAL phase's held entry commands ("Disparar Acción"). */
  onTriggerAction: (params?: Record<string, unknown>) => void;
  sdLogging: boolean;
  onToggleSd: () => void;
  onStop: () => void;
  /** Pause if running, resume if paused, start if idle — the single run-toggle button. */
  onToggleRun: () => void;
  /** Reports the recipe currently shown, so the page can create-and-start a run from it. */
  onRecipeChange?: (recipe: RecipeDef | null) => void;
  /** Reports the operator's pre-launch mode choices, so the page can create the experiment
   *  with them. Ignored once a run is active (the orchestrator owns modes then). */
  onModeDraftChange?: (modes: Record<string, PhaseMode>) => void;
  /** The name and description chosen in the pre-start dialog, echoed read-only in the footer. */
  runTitle?: string | null;
  runNotes?: string | null;
}) {
  const { data, error, loading } = useAsync(() => getRecipes(), []);
  const [tab, setTab] = useState<TabId>('ensayo');
  const [selected, setSelected] = useState<string | null>(null);
  // Operator edits to the phase plan, keyed by `phase_id` then param name. Held here rather
  // than pushed to the gateway because the recipe's params are the plan's declared defaults,
  // not live setpoints — the engine issues the `commands` on phase entry. Editing is the
  // mockup's behaviour; the values ride with the plan the operator is looking at.
  const [paramEdits, setParamEdits] = useState<Record<string, Record<string, unknown>>>({});
  // Operator mode choices made *before* launch, keyed by `phase_id`. During a run the mode
  // lives on the orchestrator and is applied immediately; before a run there is no experiment
  // to write to yet, so the choice is held here and handed to the page when it creates the
  // experiment. That is the pre-launch edit the UI used to disable outright.
  const [modeEdits, setModeEdits] = useState<Record<string, PhaseMode>>({});

  const recipes: RecipeDef[] = data?.recipes ?? [];
  const activeRecipeId = run?.recipe_id ?? null;
  const shown = activeRecipeId ?? selected ?? recipes[0]?.recipe_id ?? null;
  const recipe = recipes.find((r) => r.recipe_id === shown) ?? null;

  // Let the page know which recipe is on screen, so "Iniciar Ensayo" can create a run for it.
  // Depends on the id, not the object, so it fires only when the selection actually changes.
  const recipeId = recipe?.recipe_id ?? null;
  useEffect(() => {
    onRecipeChange?.(recipe);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recipeId]);

  // An active run supplies the phase list with live status; otherwise the recipe is a preview.
  // Either way the operator's param edits overlay the declared defaults.
  const livePhases = run?.phases ?? null;
  const basePhases: PhaseProgress[] =
    activeRecipeId && livePhases && livePhases.length > 0
      ? livePhases
      : (recipe?.phases.map((p) => ({ ...p, status: 'pending' as const })) ?? []);
  // Overlay the operator's edits: params always, and the mode only when there is no live run
  // (a live run's modes come back on `run.phases`, already reflecting `setMode`). This is what
  // makes a pre-launch mode choice visible on the card before the experiment exists.
  const phases: PhaseProgress[] = basePhases.map((p) =>
    paramEdits[p.phase_id] || modeEdits[p.phase_id]
      ? {
          ...p,
          params: { ...p.params, ...(paramEdits[p.phase_id] ?? {}) },
          mode: activeRecipeId ? p.mode : (modeEdits[p.phase_id] ?? p.mode),
        }
      : p,
  );

  const handleParam = (phaseId: string, key: string, value: number | string | boolean) => {
    setParamEdits((prev) => ({
      ...prev,
      [phaseId]: { ...(prev[phaseId] ?? {}), [key]: value },
    }));
  };

  const handleMode = (phaseId: string, mode: PhaseMode) => {
    // During a run the orchestrator is the writer: apply immediately. Before a run there is no
    // experiment to write to, so hold the choice as a draft and let the page carry it into the
    // experiment it creates on launch.
    if (activeRecipeId) {
      onMode(phaseId, mode);
      return;
    }
    const next = { ...modeEdits, [phaseId]: mode };
    setModeEdits(next);
    onModeDraftChange?.(next);
  };

  const setAllModes = (mode: PhaseMode) => {
    if (activeRecipeId) {
      for (const p of phases) {
        onMode(p.phase_id, mode);
      }
      return;
    }
    const next: Record<string, PhaseMode> = {};
    for (const p of phases) {
      next[p.phase_id] = mode;
    }
    setModeEdits(next);
    onModeDraftChange?.(next);
  };

  const triggerManual = (phaseId?: string) => {
    // "Disparar Acción" now fires the phase's *own* entry commands, held by the orchestrator
    // since the phase was entered. When the operator has edited the setpoint (e.g. 800 RPM
    // instead of 1000 RPM), the edited params are forwarded so the engine applies them.
    onTriggerAction(phaseId ? paramEdits[phaseId] : undefined);
  };

  return (
    <div className="bg-scada-surface rounded-2xl border border-scada-border p-4 shadow-sm flex flex-col gap-4">
      {/* Tabs */}
      <div className="grid grid-cols-4 gap-1 p-1 bg-scada-well rounded-xl border border-scada-border text-xs">
        {TAB_DEFS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={cn(
              'py-2 rounded-lg text-center transition',
              tab === t.id
                ? 'bg-scada-elevated text-white font-semibold border border-telemetry-do/50'
                : 'text-slate-400 hover:text-white',
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'ensayo' ? (
        <div className="space-y-3">
          {/* Protocol header + recipe selector */}
          <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-white flex items-center gap-1.5">
                <FlaskConical className="w-4 h-4 text-telemetry-do" /> Protocolo Experimental
              </span>
              <span className="text-[10px] font-mono uppercase px-2 py-0.5 rounded bg-scada-elevated text-slate-300 border border-scada-border">
                Capítulo V
              </span>
            </div>
            {/* "Caso de Ensayo" — always shown, as in the mockup. Locked while a run is
                active: the recipe is fixed for the run's lifetime, and silently swapping it
                under a live phase would misrepresent what the gateway is executing. */}
            {recipes.length > 0 ? (
              <>
                <div>
                  <label
                    htmlFor="recipe-select"
                    className="text-[11px] text-slate-400 font-medium block mb-1"
                  >
                    Caso de Ensayo:
                  </label>
                  <select
                    id="recipe-select"
                    value={shown ?? ''}
                    onChange={(e) => setSelected(e.target.value)}
                    disabled={Boolean(activeRecipeId)}
                    title={
                      activeRecipeId
                        ? 'El protocolo queda fijo mientras el ensayo está en marcha'
                        : undefined
                    }
                    className={cn(
                      'w-full bg-scada-elevated text-slate-100 text-xs border border-scada-border rounded-lg p-2 font-medium focus:outline-none focus:border-telemetry-do',
                      activeRecipeId && 'opacity-60 cursor-not-allowed',
                    )}
                  >
                    {recipes.map((r) => (
                      <option key={r.recipe_id} value={r.recipe_id}>
                        {r.title}
                      </option>
                    ))}
                  </select>
                </div>
                {recipe ? (
                  <p className="text-[10px] text-slate-500 font-mono -mt-1">
                    Archivo destino: {recipe.filename}
                  </p>
                ) : null}
              </>
            ) : (
              <p className="text-[11px] text-slate-400 font-mono">cargando protocolos…</p>
            )}
            {/* Mode legend */}
            <div className="p-2.5 rounded-lg bg-scada-canvas border border-scada-border space-y-1.5 text-[10px] font-mono">
              <div className="flex items-center gap-2">
                <span className="px-1.5 py-0.5 rounded bg-emerald-950/80 text-emerald-400 border border-emerald-800 font-bold">
                  AUTO
                </span>
                <span className="text-slate-300">100% autónomo:</span>
                <span className="text-slate-400">aplica consignas y avanza por umbrales.</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="px-1.5 py-0.5 rounded bg-sky-950/80 text-sky-400 border border-sky-800 font-bold">
                  HÍBRIDO
                </span>
                <span className="text-slate-300">Supervisado:</span>
                <span className="text-slate-400">requiere visto bueno del operador al cumplirse.</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="px-1.5 py-0.5 rounded bg-amber-950/80 text-amber-400 border border-amber-800 font-bold">
                  MANUAL
                </span>
                <span className="text-slate-300">Control directo:</span>
                <span className="text-slate-400">el operador dispara y decide el avance.</span>
              </div>
            </div>
          </div>

          {error ? (
            <p className="text-[11px] text-rose-400">No se pudieron cargar los protocolos.</p>
          ) : null}
          {loading && recipes.length === 0 ? (
            <p className="text-[11px] text-slate-500 text-center py-4">Leyendo protocolos…</p>
          ) : null}

          {phases.length > 0 ? (
            <div className="space-y-2.5 max-h-[780px] 2xl:max-h-[920px] overflow-y-auto pr-0.5">
              {/* Batch mode selector buttons */}
              <div className="flex flex-wrap items-center justify-between gap-2 p-2.5 rounded-xl bg-scada-well border border-scada-border">
                <span className="text-[11px] font-mono text-slate-300">Poner todas las etapas en:</span>
                <div className="flex gap-1.5 font-mono text-[10px]">
                  <button
                    type="button"
                    onClick={() => setAllModes('auto')}
                    disabled={busy}
                    className="px-2.5 py-1 rounded bg-emerald-950/70 hover:bg-emerald-900 border border-emerald-800 text-emerald-300 font-bold transition active:scale-95 disabled:opacity-50"
                    title="Establecer todas las etapas en modo AUTÓNOMO"
                  >
                    Todos AUTO
                  </button>
                  <button
                    type="button"
                    onClick={() => setAllModes('hibrido')}
                    disabled={busy}
                    className="px-2.5 py-1 rounded bg-sky-950/70 hover:bg-sky-900 border border-sky-800 text-sky-300 font-bold transition active:scale-95 disabled:opacity-50"
                    title="Establecer todas las etapas en modo HÍBRIDO (Supervisado)"
                  >
                    Todos HÍBRIDO
                  </button>
                  <button
                    type="button"
                    onClick={() => setAllModes('manual')}
                    disabled={busy}
                    className="px-2.5 py-1 rounded bg-amber-950/70 hover:bg-amber-900 border border-amber-800 text-amber-300 font-bold transition active:scale-95 disabled:opacity-50"
                    title="Establecer todas las etapas en modo MANUAL (Control directo)"
                  >
                    Todos MANUAL
                  </button>
                </div>
              </div>

              {!activeRecipeId ? (
                <p className="text-[11px] text-sky-300/90 bg-sky-950/30 border border-sky-800/50 rounded-lg px-3 py-2">
                  Puedes <strong>preparar los modos</strong> de cada fase antes de lanzar. Se
                  guardan con el ensayo y el orquestador arranca con ellos; durante el ensayo
                  también se pueden cambiar en vivo. Los parámetros de cada fase se preparan aquí.
                </p>
              ) : null}
              {phases.map((phase) => (
                <PhaseCard
                  key={phase.phase_id}
                  phase={phase}
                  busy={busy}
                  modeDisabledReason={
                    busy
                      ? 'Hay una transición en curso; espera a que termine.'
                      : undefined
                  }
                  onMode={(mode) => handleMode(phase.phase_id, mode)}
                  onParam={(key, value) => handleParam(phase.phase_id, key, value)}
                  onConfirm={onConfirm}
                  onManualAction={() => triggerManual(phase.phase_id)}
                />
              ))}
            </div>
          ) : null}

          {/* Global experiment actions: run toggle, stop, SD, download, and collapsible
              options — the mockup's Ensayo footer. */}
          <ExperimentActions
            run={run}
            busy={busy}
            sdLogging={sdLogging}
            filename={recipe?.filename ?? '—'}
            title={runTitle}
            notes={runNotes}
            onToggleRun={onToggleRun}
            onStop={onStop}
            onToggleSd={onToggleSd}
          />
        </div>
      ) : null}

      {tab === 'actuadores' ? <ActuatorDeck registry={registry} snapshot={snapshot} /> : null}
      {tab === 'sensor' ? <SensorTab registry={registry} snapshot={snapshot} /> : null}
      {tab === 'diag' ? <DiagnosticsTab registry={registry} snapshot={snapshot} /> : null}
    </div>
  );
}
