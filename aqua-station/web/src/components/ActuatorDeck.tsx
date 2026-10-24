'use client';

/**
 * The actuator deck: direct control of the three physical effectors.
 *
 * Every control here maps to a verb the node's own handler branches on — `set_speed`,
 * `start_mixer`, `dose` — never a generic one. That is the lesson from the previous console:
 * a single global action list offered `start` to a mixer that understands `start_mixer`, and
 * the obvious button was silently rejected.
 *
 * Nothing here is a control loop. The aerator's PID and the mixer's PI run on the nodes at
 * their own rates; these are setpoints, and the node decides how to reach them. A slider sets
 * a target, not a duty cycle.
 *
 * The pump is the one place a mistake injects chemicals into the tank, so its action is a
 * deliberate button + volume, never a slider that can be dragged past by accident.
 */

import { useState } from 'react';
import { AlertTriangle, Fan, Play, RotateCw, Syringe, Wrench } from 'lucide-react';
import { postCommand } from '@/lib/api';
import type { LiveSnapshot, Registry } from '@/lib/types';
import { readField } from '@/lib/registry';
import { formatNumber } from '@/lib/format';
import { useOptimistic } from '@/hooks/useOptimistic';
import { getODriveHumanStatus } from '@/lib/odriveStatus';
import { cn } from './ui';

/** Send a command, ignoring a refusal — the live view reconciles the screen either way, and a
 *  per-click error toast for a superseded setpoint would be noise. */
async function send(role: string, action: string, target: Record<string, unknown>): Promise<void> {
  try {
    await postCommand({ role, action, target, deadline_s: 120, requested_by: 'operator:web' });
  } catch {
    /* the next live frame shows the truth */
  }
}

function Panel({
  icon,
  title,
  subtitle,
  accent,
  badge,
  power,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  accent: string;
  /** Optional right-aligned status badge (e.g. "PI CLOSED-LOOP"). */
  badge?: React.ReactNode;
  /** Optional power switch, as the mockup's per-actuator toggle. */
  power?: { on: boolean; accent: string; onToggle: () => void; pending?: boolean };
  children: React.ReactNode;
}) {
  return (
    <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <div className={cn('p-1 rounded border shrink-0', accent)}>{icon}</div>
          <div className="min-w-0">
            <span className="text-xs font-bold text-white block truncate">{title}</span>
            <span className="text-[9px] text-slate-400 font-mono truncate block">{subtitle}</span>
          </div>
        </div>
        {power ? (
          <Switch
            on={power.on}
            accent={power.accent}
            onToggle={power.onToggle}
            pending={power.pending}
          />
        ) : (
          badge
        )}
      </div>
      {children}
    </div>
  );
}

/** The mockup's power switch: a small pill that fills with the actuator's colour when on.
 *
 *  `pending` marks an issued command the node has not yet confirmed: the pill fills at once
 *  (the operator's click must have a visible effect) but pulses until the measured value
 *  agrees, so "I asked for this" and "the node is doing this" stay distinguishable. */
function Switch({
  on,
  accent,
  onToggle,
  pending = false,
}: {
  on: boolean;
  accent: string;
  onToggle: () => void;
  pending?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-busy={pending}
      onClick={onToggle}
      title={pending ? 'Comando enviado — esperando confirmación del nodo' : undefined}
      className={cn(
        'relative inline-flex h-4 w-8 shrink-0 items-center rounded-full transition-colors',
        on ? accent : 'bg-slate-800',
        pending && 'animate-pulse',
      )}
    >
      <span
        className={cn(
          'inline-block h-3 w-3 transform rounded-full bg-white transition-transform',
          on ? 'translate-x-4' : 'translate-x-0.5',
        )}
      />
    </button>
  );
}

function Slider({
  value,
  min,
  max,
  step,
  onChange,
  onCommit,
  accent,
}: {
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  /** Fired once the operator releases the handle. Used to send the setpoint on release so the
   *  slider is not merely cosmetic until a separate "Aplicar" button is pressed. */
  onCommit?: (v: number) => void;
  accent: string;
}) {
  return (
    <input
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      onPointerUp={onCommit ? (e) => onCommit(Number((e.target as HTMLInputElement).value)) : undefined}
      onKeyUp={onCommit ? (e) => onCommit(Number((e.target as HTMLInputElement).value)) : undefined}
      className={cn('w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer', accent)}
    />
  );
}

export function ActuatorDeck({
  registry,
  snapshot,
}: {
  registry: Registry | null;
  snapshot: LiveSnapshot | null;
}) {
  const [rpm, setRpm] = useState(210);
  const [mixerRpm, setMixerRpm] = useState(1200);
  const [doseMl, setDoseMl] = useState(5.0);
  const [busy, setBusy] = useState(false);

  const measuredRpm = readField(registry, snapshot, 'aerator', 'drv_rpm');
  const measuredMixer = readField(registry, snapshot, 'mixer', 'drv_rpm');
  const dosed = readField(registry, snapshot, 'pump', 'pump_dosed_ml');

  // Measured state, read off the node.
  //
  // It is derived from the *measured rpm*, not `drv_running`. `drv_running` is the control
  // loop's own view ("is the loop armed"), which stays `true` for a PID holding 0 rpm — so a
  // stopped motor read as "on" and the switch disagreed with the needle beside it. A power
  // switch means "is it turning", and the node's own tachometer is the honest answer. Below the
  // threshold it reads off; with no fresh telemetry it also reads off: a defined, safe default
  // that is a fact about what is known, not a guess.
  const aerator = snapshot?.nodes?.['aerator'];
  const mixer = snapshot?.nodes?.['mixer'];
  const RUNNING_RPM = 20; // fallback if drv_running metric is unset
  const aeratorArmed = aerator?.metrics?.['drv_running'] != null
    ? Boolean(aerator.metrics['drv_running'])
    : (measuredRpm.value ?? 0) > RUNNING_RPM;
  const measuredOdriveOn = Boolean(aerator?.online) && aeratorArmed;

  const aeratorState = typeof aerator?.metrics?.['drv_state'] === 'number' ? Number(aerator.metrics['drv_state']) : null;
  const aeratorStatus = typeof aerator?.metrics?.['drv_status'] === 'number' ? Number(aerator.metrics['drv_status']) : null;
  const aeratorDisarmReason = typeof aerator?.metrics?.['drv_disarm_reason'] === 'number' ? Number(aerator.metrics['drv_disarm_reason']) : null;
  const aeratorFailsafe = Boolean(aerator?.metrics?.['ctl_failsafe']);
  const aeratorVbus = typeof aerator?.metrics?.['drv_vbus_v'] === 'number' ? Number(aerator.metrics['drv_vbus_v']) : null;

  const odriveHuman = getODriveHumanStatus({
    online: Boolean(aerator?.online),
    running: aeratorArmed,
    state: aeratorState,
    statusCode: aeratorStatus,
    disarmReason: aeratorDisarmReason,
    failsafe: aeratorFailsafe,
    vbus: aeratorVbus,
  });

  const mixerArmed = mixer?.metrics?.['drv_running'] != null
    ? Boolean(mixer.metrics['drv_running'])
    : (measuredMixer.value ?? 0) > RUNNING_RPM;
  const measuredMixerOn = Boolean(mixer?.online) && mixerArmed;

  // The switch the operator *sees* is the measured state overlaid with the command they just
  // sent. Without this, a toggle could not move until the next tachometer sample arrived — and
  // for an actuator whose value cannot move (an aerator with no ODrive attached reports 0 rpm
  // forever) it could never move at all, which is exactly what was reported as "the toggle does
  // not update". The intent expires after a few seconds if the node never confirms it, so the
  // worst case is a brief lie followed by the truth, never a permanent one.
  const odriveSwitch = useOptimistic(measuredOdriveOn, (a, b) => a === b, 5_000);
  const mixerSwitch = useOptimistic(measuredMixerOn, (a, b) => a === b, 5_000);

  const run = async (role: string, action: string, target: Record<string, unknown>) => {
    setBusy(true);
    await send(role, action, target);
    setBusy(false);
  };

  const has = (role: string) => Boolean(registry?.roles?.[role]);
  const readyCount = [has('aerator'), has('mixer'), has('pump')].filter(Boolean).length;

  return (
    // No outer card: RecipeStudio already supplies the surface and padding, so wrapping here
    // would nest two identical panels and read as a redundant sub-chart.
    <div className="space-y-3">
      <div className="text-[11px] text-slate-400 px-1 flex items-center justify-between">
        <span>Control de los 3 Efectores Físicos del Banco:</span>
        <span className="font-mono text-emerald-400">{readyCount} Nodos Listos</span>
      </div>

      {/* ODrive aerator — speed is the knob, in RPM, reaching the node as a percent. The power
          switch arms and disarms it at the current setpoint. */}
      {has('aerator') ? (
        <Panel
          icon={<Fan className="w-3.5 h-3.5 text-telemetry-motor" />}
          title="Aireador ODrive S1 (M8325s)"
          subtitle="Planta 2 • 0 a 600 RPM"
          accent="bg-amber-950/60 border-amber-800 text-telemetry-motor"
          power={{
            on: odriveSwitch.value,
            accent: 'bg-amber-500',
            pending: odriveSwitch.pending,
            onToggle: () => {
              // Flip the switch now, then send. The switch reconciles to the node's tachometer
              // as soon as it agrees, or reverts if the command is never confirmed.
              const next = !odriveSwitch.value;
              odriveSwitch.set(next);
              void run(
                'aerator',
                next ? 'set_speed' : 'stop',
                next ? { speed_percent: Math.round((rpm / 600) * 100) } : {},
              );
            },
          }}
        >
          {/* Badge de Estado Humano ODrive S1 */}
          <div className="flex items-center justify-between gap-2 p-2 rounded-lg bg-scada-surface border border-scada-border">
            <div className="flex items-center gap-2 min-w-0">
              <span className={cn('w-2 h-2 rounded-full shrink-0', odriveHuman.badgeDotClass)} />
              <div className="truncate">
                <span className="text-[10px] text-slate-400 block font-mono">Estado ODrive S1:</span>
                <span className="text-xs font-bold text-slate-100">{odriveHuman.badgeText}</span>
              </div>
            </div>
            <span className={cn('text-[10px] px-2 py-0.5 rounded border font-mono shrink-0', odriveHuman.badgeClass)}>
              {odriveHuman.stateLabel}
            </span>
          </div>

          {/* Banner de diagnóstico / alerta de fallas */}
          {(odriveHuman.hasErrors || odriveHuman.isDisarmedByFault || (odriveHuman.disarmReason && !odriveHuman.hasErrors)) ? (
            <div className="p-2.5 rounded-lg bg-rose-950/40 border border-rose-800/80 text-xs space-y-1.5">
              <div className="flex items-start gap-2 text-rose-300">
                <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5 text-rose-400" />
                <div className="flex-1 min-w-0">
                  <strong className="block text-rose-200">{odriveHuman.summaryText}</strong>
                  {odriveHuman.errors.length > 0 && (
                    <ul className="list-disc list-inside text-[11px] text-rose-300/90 mt-1 space-y-0.5">
                      {odriveHuman.errors.map((e) => (
                        <li key={e.mask}>
                          <strong>{e.label}</strong>: {e.advice}
                        </li>
                      ))}
                    </ul>
                  )}
                  {odriveHuman.disarmReason && odriveHuman.errors.length === 0 && (
                    <p className="text-[11px] text-amber-300/90 mt-0.5">
                      <strong>Causa de desarme:</strong> {odriveHuman.disarmReason.label}. {odriveHuman.disarmReason.advice}
                    </p>
                  )}
                </div>
              </div>
              <div className="flex justify-end pt-1">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    void run('aerator', 'clear_estop', {});
                  }}
                  className="px-2.5 py-1 rounded bg-rose-900/80 hover:bg-rose-800 text-rose-100 text-[11px] font-bold border border-rose-700 transition flex items-center gap-1.5 shadow-sm"
                  title="Envía comando clear_estop y reintenta armar ODrive"
                >
                  <Wrench className="w-3 h-3" />
                  Despejar Errores ODrive
                </button>
              </div>
            </div>
          ) : null}

          <div className="flex justify-between text-[11px] font-mono text-slate-400">
            <span>
              Consigna: <strong className="text-telemetry-motor">{rpm} RPM</strong>
            </span>
            <span>
              Medido: <strong className="text-slate-200">{formatNumber(measuredRpm.value, 0)}</strong>
            </span>
          </div>
          <Slider
            value={rpm}
            min={0}
            max={600}
            step={10}
            onChange={setRpm}
            accent="accent-amber-500"
          />
          <div className="flex gap-1.5 font-mono text-[10px]">
            {[0, 150, 300, 450, 600].map((preset) => (
              <button
                key={preset}
                type="button"
                onClick={() => {
                  setRpm(preset);
                }}
                className={cn(
                  'flex-1 py-1 rounded bg-scada-elevated hover:bg-slate-700 border text-slate-300 transition',
                  rpm === preset ? 'border-amber-500 text-amber-300 font-bold' : 'border-scada-border',
                )}
              >
                {preset}
              </button>
            ))}
          </div>
          <div className="flex gap-2 pt-0.5">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                odriveSwitch.set(true);
                void run('aerator', 'set_speed', { speed_percent: Math.round((rpm / 600) * 100) });
              }}
              className="flex-1 py-1.5 rounded-lg text-white text-xs font-bold bg-amber-600 hover:bg-amber-500 disabled:opacity-50 transition shadow-sm"
            >
              Aplicar {rpm} RPM ({Math.round((rpm / 600) * 100)}%)
            </button>
            <button
              type="button"
              disabled={busy || !odriveSwitch.value}
              onClick={() => {
                odriveSwitch.set(false);
                void run('aerator', 'stop', {});
              }}
              className="px-3 py-1.5 rounded-lg text-xs font-bold bg-slate-800 hover:bg-rose-950/80 text-slate-300 hover:text-rose-300 border border-slate-700 hover:border-rose-800 disabled:opacity-40 transition"
              title="Desarma el motor y conmuta el nodo a telemetría IDLE (20 s)"
            >
              Detener
            </button>
          </div>
        </Panel>
      ) : null}

      {/* Mixer — closed-loop target in RPM, the only form its handler accepts. Same power switch
          as the aerator, so both motors arm and disarm the same way. */}
      {has('mixer') ? (
        <Panel
          icon={<RotateCw className="w-3.5 h-3.5 text-telemetry-mixer" />}
          title="Mezclador T-200 (Controlador PI)"
          subtitle="100 Hz Tacómetro FG • Kp=0.0027, Ki=0.0064"
          accent="bg-teal-950/60 border-teal-800 text-telemetry-mixer"
          power={{
            on: mixerSwitch.value,
            accent: 'bg-teal-500',
            pending: mixerSwitch.pending,
            onToggle: () => {
              const next = !mixerSwitch.value;
              mixerSwitch.set(next);
              void run(
                'mixer',
                next ? 'start_mixer' : 'stop_mixer',
                next ? { target_rpm: mixerRpm || 1200 } : {},
              );
            },
          }}
        >
          <div className="flex justify-between text-[11px] font-mono text-slate-400">
            <span>
              Consigna: <strong className="text-telemetry-mixer">{mixerRpm} RPM</strong>
            </span>
            <span>
              Medido: <strong className="text-slate-200">{formatNumber(measuredMixer.value, 0)} RPM</strong>
            </span>
          </div>
          <Slider
            value={mixerRpm}
            min={0}
            max={3000}
            step={50}
            onChange={setMixerRpm}
            accent="accent-teal-500"
          />
          <div className="flex gap-1.5 font-mono text-[10px]">
            {[0, 600, 1200, 1800, 2500].map((preset) => (
              <button
                key={preset}
                type="button"
                onClick={() => {
                  setMixerRpm(preset);
                }}
                className={cn(
                  'flex-1 py-1 rounded bg-scada-elevated hover:bg-slate-700 border text-slate-300 transition',
                  mixerRpm === preset ? 'border-teal-500 text-teal-300 font-bold' : 'border-scada-border',
                )}
              >
                {preset}
              </button>
            ))}
          </div>
          <div className="flex gap-2 pt-0.5">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                mixerSwitch.set(true);
                void run('mixer', 'start_mixer', { target_rpm: mixerRpm });
              }}
              className="flex-1 py-1.5 rounded-lg text-white text-xs font-bold bg-teal-600 hover:bg-teal-500 disabled:opacity-50 transition shadow-sm"
            >
              Aplicar {mixerRpm} RPM
            </button>
            <button
              type="button"
              disabled={busy || !mixerSwitch.value}
              onClick={() => {
                mixerSwitch.set(false);
                void run('mixer', 'stop_mixer', {});
              }}
              className="px-3 py-1.5 rounded-lg text-xs font-bold bg-slate-800 hover:bg-rose-950/80 text-slate-300 hover:text-rose-300 border border-slate-700 hover:border-rose-800 disabled:opacity-40 transition"
              title="Detiene el mezclador y conmuta el nodo a telemetría IDLE (20 s)"
            >
              Detener
            </button>
          </div>
        </Panel>
      ) : null}

      {/* Pump — a dose is deliberate: volume + a button, never a drag. */}
      {has('pump') ? (
        <Panel
          icon={<Syringe className="w-3.5 h-3.5 text-telemetry-pump" />}
          title="Bomba Dosificadora Na₂SO₃"
          subtitle="Planta 1 • AS5600 12-bit"
          accent="bg-purple-950/60 border-purple-800 text-telemetry-pump"
          badge={
            <span className="text-[9px] font-mono text-purple-300 px-1.5 py-0.5 rounded bg-purple-950 border border-purple-800">
              Calibrado
            </span>
          }
        >
          <div className="flex justify-between text-[11px] font-mono text-slate-400">
            <span>
              Volumen dosificado:{' '}
              <strong className="text-slate-200">{formatNumber(dosed.value, 2)} mL</strong>
            </span>
          </div>
          <div className="flex gap-2 items-end">
            <div className="flex-1">
              <label className="text-[10px] text-slate-400 font-mono block mb-1">
                Volumen a Inyectar (mL):
              </label>
              <input
                type="number"
                min={0.5}
                max={50}
                step={0.5}
                value={doseMl}
                onChange={(e) => setDoseMl(Number(e.target.value))}
                className="w-full bg-scada-elevated text-slate-100 text-xs border border-scada-border rounded-lg p-2 font-mono focus:outline-none focus:border-telemetry-pump"
              />
            </div>
            <button
              type="button"
              disabled={busy}
              onClick={() => run('pump', 'dose', { target_ml: doseMl })}
              className="py-2 px-3 bg-purple-600 hover:bg-purple-500 text-white rounded-lg font-bold text-xs flex items-center gap-1 transition disabled:opacity-50"
            >
              <Play className="w-3.5 h-3.5 fill-white" /> Dosificar
            </button>
          </div>
          <div className="flex justify-between items-center text-[10px] text-slate-400 pt-1 border-t border-scada-border">
            <span>Purgado de tubería química:</span>
            <button
              type="button"
              onClick={() => void run('pump', 'force_off', {})}
              className="text-sky-400 hover:underline"
              title="Detiene y purga la línea de dosificación"
            >
              Purgar Tubería
            </button>
          </div>
        </Panel>
      ) : null}

      {!has('aerator') && !has('mixer') && !has('pump') ? (
        <p className="text-xs text-slate-400 text-center py-4">Registro no cargado.</p>
      ) : null}
    </div>
  );
}
