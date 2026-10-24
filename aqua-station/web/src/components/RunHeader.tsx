'use client';

/**
 * The bench header: identity, node health, the master clock, and the run controls.
 *
 * This is the operator's first surface, so it carries the one control that must never be
 * fumbled — the emergency stop — and the four readings that say whether the bench is alive.
 *
 * Two things are deliberate:
 *
 * * **E-STOP is not a run transition.** It issues `emergency_stop` through the outbox, which
 *   bypasses the database, and it is visually separate from Start/Stop. A stop that waits on a
 *   write, or that sits in a dropdown beside a setpoint, is the failure this avoids.
 * * **The node matrix shows only real fields.** Where a mockup would print a plausible number
 *   the registry does not carry, this prints an em dash. A fabricated health reading is worse
 *   than a blank one.
 */

import { useEffect, useMemo, useState } from 'react';
import { Clock, Activity, HardDrive, OctagonAlert, Pause, Play, Square } from 'lucide-react';
import type { LiveSnapshot, Registry, RunState, RunStateResponse } from '@/lib/types';
import { readField } from '@/lib/registry';
import { formatNumber } from '@/lib/format';
import { PROJECT_TZ_LABEL, formatRtcClock, formatTime } from '@/lib/dateUtils';
import { cn } from './ui';
import { PresenceSummary, SessionAge } from './StatusStrip';
import { getODriveHumanStatus } from '@/lib/odriveStatus';

/** A live RTC clock for the header, HH:MM:SS.mmm in the project's timezone, ticking every
 *  120 ms so the milliseconds stay honest without burning a frame budget.
 *
 *  Renders an em dash on the server and the first client paint: the time is different on every
 *  render, so emitting it during hydration would mismatch the server HTML (React error #425).
 *  `mounted` gates that first paint. */
function useRtcClock(): string {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 120);
    return () => clearInterval(id);
  }, []);
  return now === null ? '--:--:--.---' : formatRtcClock(now);
}

/** One cell of the node matrix, mirroring the mockup: a presence dot, a name, and one
 *  compact metric line in the node's own accent. */
function NodeCell({
  label,
  online,
  line,
  accent,
  title,
}: {
  label: string;
  online: boolean;
  line: string;
  accent: string;
  title: string;
}) {
  return (
    <div className="flex items-center gap-2 pr-3 border-r border-scada-border last:border-r-0 last:pr-0" title={title}>
      <span
        className={cn(
          'w-2 h-2 rounded-full shrink-0',
          online ? 'bg-emerald-400' : 'bg-rose-500/70',
        )}
      />
      <div className="min-w-0">
        <div className="font-semibold text-slate-200 text-[11px] truncate">{label}</div>
        <div className={cn('text-[9px] font-mono truncate', online ? accent : 'text-slate-500')}>
          {line}
        </div>
      </div>
    </div>
  );
}

export function RunHeader({
  registry,
  snapshot,
  run,
  busy,
  onStart,
  onStop,
  onPause,
  onResume,
  onEStop,
  estopLatched,
  onClearEStop,
  sdLogging,
  onToggleSd,
  onSetState,
  schemaVersion,
  lastFrameAt,
}: {
  registry: Registry | null;
  snapshot: LiveSnapshot | null;
  run: RunStateResponse | null;
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
  onPause: () => void;
  onResume: () => void;
  onEStop: () => void;
  estopLatched: boolean;
  onClearEStop: () => void;
  sdLogging: boolean;
  onToggleSd: () => void;
  /** Global orchestrator state transition, from the header's Modo selector. */
  onSetState?: (state: RunState) => void;
  schemaVersion: number | null;
  lastFrameAt: number | null;
}) {
  // The matrix is built from the registry, so a fifth node appears here with no code change.
  // Each cell mirrors the mockup: an online dot, the node name, and one compact metric line
  // in the node's own accent. Only fields the registry actually carries are shown — the rest
  // render as an em dash rather than a plausible invented value.
  const cells = useMemo(() => {
    const byRole = (role: string) => snapshot?.nodes?.[role] ?? null;
    const f = (role: string, field: string) => {
      const r = readField(registry, snapshot, role, field);
      return r.value === null ? '—' : formatNumber(r.value, r.field?.precision ?? null);
    };

    const out: {
      label: string;
      online: boolean;
      line: string;
      accent: string;
      title: string;
    }[] = [];

    if (registry?.roles?.['od_sensor']) {
      const sensor = byRole('od_sensor');
      const hz = sensor?.observed_hz;
      out.push({
        label: 'Sensor OD',
        online: Boolean(sensor?.online),
        line: `${hz == null ? '—' : hz.toFixed(1)}s • ${f('od_sensor', 'pwr_battery_v')}V`,
        accent: 'text-slate-400',
        title: 'Aqualabo OPTOD Modbus RS485 • ESP32',
      });
    }

    if (registry?.roles?.['aerator']) {
      const aero = byRole('aerator');
      const aeroState = typeof aero?.metrics?.['drv_state'] === 'number' ? Number(aero.metrics['drv_state']) : null;
      const aeroStatus = typeof aero?.metrics?.['drv_status'] === 'number' ? Number(aero.metrics['drv_status']) : null;
      const aeroDisarmReason = typeof aero?.metrics?.['drv_disarm_reason'] === 'number' ? Number(aero.metrics['drv_disarm_reason']) : null;
      const aeroRunning = Boolean(aero?.metrics?.['drv_running']);
      const aeroFailsafe = Boolean(aero?.metrics?.['ctl_failsafe']);
      const aeroVbus = typeof aero?.metrics?.['drv_vbus_v'] === 'number' ? Number(aero.metrics['drv_vbus_v']) : null;

      const human = getODriveHumanStatus({
        online: Boolean(aero?.online),
        running: aeroRunning,
        state: aeroState,
        statusCode: aeroStatus,
        disarmReason: aeroDisarmReason,
        failsafe: aeroFailsafe,
        vbus: aeroVbus,
      });

      const lineText = !aero?.online
        ? 'DESCONECTADO'
        : human.hasErrors
        ? `FALLA: ${human.errors[0]?.name ?? 'ERROR'}`
        : `${human.badgeText} • ${f('aerator', 'drv_vbus_v')}V • ${f('aerator', 'drv_rpm')} RPM`;

      out.push({
        label: 'Aireador ODrive',
        online: Boolean(aero?.online),
        line: lineText,
        accent: human.hasErrors ? 'text-rose-400 font-bold' : (aeroRunning ? 'text-emerald-400 font-bold' : 'text-slate-400'),
        title: `Motor M8325s + ODrive S1: ${human.summaryText}`,
      });
    }

    if (registry?.roles?.['mixer']) {
      const mixer = byRole('mixer');
      const running = mixer?.metrics?.['drv_running'];
      const on = running === true || running === 1 || running === 'true';
      out.push({
        label: 'Mezclador T200 (PI)',
        online: Boolean(mixer?.online),
        line: on ? `PI ACTIVO • ${f('mixer', 'drv_rpm')} RPM` : `PI EN REPOSO • ${f('mixer', 'drv_rpm')} RPM`,
        accent: 'text-telemetry-mixer',
        title: 'ESC T-200 Lazo Cerrado PI (100 Hz Tacómetro) • ESP32-S3',
      });
    }

    if (registry?.roles?.['pump']) {
      const pump = byRole('pump');
      out.push({
        label: 'Bomba Sulfito',
        online: Boolean(pump?.online),
        line: pump?.online ? `Listo • ${f('pump', 'pump_dosed_ml')} mL` : 'Desconectada',
        accent: 'text-telemetry-pump',
        title: 'Bomba Peristáltica 12V Na₂SO₃ + AS5600 12-bit • ESP32',
      });
    }

    return out;
  }, [registry, snapshot]);

  const active = run?.state === 'ACTIVE_EXPERIMENT';
  const paused = Boolean(run?.paused);
  const rtcClock = useRtcClock();

  const badge =
    run?.state === 'ACTIVE_EXPERIMENT'
      ? paused
        ? { text: 'PAUSADO', cls: 'bg-amber-950/80 text-amber-400 border-amber-800' }
        : { text: 'EXP ACTIVO', cls: 'bg-emerald-950/80 text-emerald-400 border-emerald-800' }
      : run?.state === 'MANUAL_OVERRIDE'
        ? { text: 'ANULACIÓN MANUAL', cls: 'bg-sky-950/80 text-sky-400 border-sky-800' }
        : { text: 'REPOSO', cls: 'bg-slate-800 text-slate-300 border-slate-700' };

  return (
    <>
      <header className="sticky top-0 z-50 bg-scada-surface/95 backdrop-blur border-b border-scada-border px-4 lg:px-6 py-2">
        <div className="w-full flex flex-wrap items-center justify-between gap-3">
          {/* Brand */}
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-scada-elevated border border-scada-border flex items-center justify-center text-telemetry-do">
              <Activity className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-extrabold tracking-tight text-white text-sm">
                  AquaSCADA<span className="text-telemetry-do">Lab</span>
                </span>
                <span
                  className={cn(
                    'text-[10px] font-bold uppercase px-2 py-0.5 rounded border flex items-center gap-1.5 font-mono',
                    badge.cls,
                  )}
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-current" />
                  {badge.text}
                </span>
              </div>
              <p className="text-[11px] text-slate-400">
                Lazo cerrado de aireación &amp; ensayos de transferencia • Acuicultura
              </p>
            </div>
          </div>

          {/* Node matrix */}
          <div className="hidden xl:flex items-center gap-2 bg-scada-well px-3 py-1.5 rounded-xl border border-scada-border text-xs">
            {cells.length === 0 ? (
              <span className="text-[11px] text-slate-500">registro no cargado</span>
            ) : (
              cells.map((c) => (
                <NodeCell key={c.label} label={c.label} online={c.online} line={c.line} accent={c.accent} title={c.title} />
              ))
            )}
          </div>

          {/* Controls */}
          <div className="flex items-center gap-2.5 flex-wrap">
            <div className="hidden sm:flex flex-col text-right pr-2">
              <div className="flex items-center justify-end gap-1.5 text-xs text-slate-300 font-mono font-medium">
                <Clock className="w-3.5 h-3.5 text-telemetry-do" />
                <span>{rtcClock}</span>
              </div>
              <span className="text-[9px] text-slate-500 font-mono">
                {PROJECT_TZ_LABEL} • RTC SYNC
              </span>
            </div>

            {/* Start / pause master button */}
            {active && !paused ? (
              <button
                type="button"
                onClick={onPause}
                disabled={busy}
                className="flex items-center gap-1.5 bg-amber-600 hover:bg-amber-500 text-white font-bold text-xs px-3 py-1.5 rounded-xl border border-amber-500 transition-all active:scale-95 disabled:opacity-50"
                title="Pausa el ensayo (mantiene los actuadores en marcha)"
              >
                <Pause className="w-3.5 h-3.5 fill-white" />
                <span>Pausar</span>
              </button>
            ) : (
              <button
                type="button"
                onClick={paused ? onResume : onStart}
                disabled={busy}
                className="flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs px-3 py-1.5 rounded-xl border border-emerald-500 transition-all active:scale-95 disabled:opacity-50"
                title="Inicia o reanuda el ensayo experimental"
              >
                <Play className="w-3.5 h-3.5 fill-white" />
                <span>{paused ? 'Reanudar' : 'Iniciar Ensayo'}</span>
              </button>
            )}

            {/* Stop */}
            <button
              type="button"
              onClick={onStop}
              disabled={busy || !active}
              className={cn(
                'flex items-center gap-1.5 font-bold text-xs px-3 py-1.5 rounded-xl border transition-all active:scale-95',
                active
                  ? 'bg-rose-950/80 hover:bg-rose-900 text-rose-300 border-rose-800'
                  : 'bg-slate-800 text-slate-500 border-slate-700 cursor-not-allowed opacity-50',
              )}
              title="Detiene y finaliza el ensayo (desarma los actuadores a 0)"
            >
              <Square className="w-3.5 h-3.5 fill-current" />
              <span>Detener</span>
            </button>

            {/* SD logging — disabled: no firmware verb yet. It used to send
                `set_config{sd_logging}`, which the sensor rejects as `unsupported_action`, so the
                button flipped a colour while nothing recorded. Honest state is a greyed button
                with the reason, the same call the `calibrate` button makes. */}
            <button
              type="button"
              disabled
              className="flex items-center gap-1.5 font-mono text-xs px-2.5 py-1.5 rounded-xl border transition-all bg-slate-800 text-slate-500 border-slate-700 opacity-50 cursor-not-allowed"
              title="El firmware del nodo aún no expone un verbo de grabación en MicroSD; el botón se habilitará cuando lo haga."
            >
              <HardDrive className="w-3.5 h-3.5" />
              <span>SD: N/D</span>
            </button>

            {/* Modo — the global state selector, as the mockup's `stateSelect`. The engine is the
                single writer, so this reflects the last read and sends a transition on change. */}
            <select
              value={run?.state ?? 'IDLE'}
              onChange={(e) => onSetState?.(e.target.value as RunState)}
              disabled={busy}
              className="bg-scada-well text-slate-200 text-xs border border-scada-border rounded-lg px-2.5 py-1.5 font-medium focus:outline-none focus:border-telemetry-do cursor-pointer disabled:opacity-50"
              title="Estado global del orquestador"
            >
              <option value="ACTIVE_EXPERIMENT">Modo: Experimento Activo (1s / 0.2s)</option>
              <option value="IDLE">Modo: Reposo (IDLE 20s)</option>
              <option value="MANUAL_OVERRIDE">Modo: Anulación Manual (Override)</option>
            </select>

            {/* E-STOP */}
            <button
              type="button"
              onClick={onEStop}
              disabled={estopLatched}
              className={cn(
                'flex items-center gap-2 text-white font-extrabold text-xs px-3.5 py-2 rounded-xl border tracking-wide transition-all active:scale-95',
                estopLatched
                  ? 'bg-rose-900 border-rose-400 cursor-not-allowed'
                  : 'bg-rose-600 hover:bg-rose-500 border-rose-500',
              )}
              title="Desarma inmediatamente todos los motores y la bomba"
            >
              <OctagonAlert className="w-4 h-4 fill-white" />
              <span>PARADA DE EMERGENCIA</span>
            </button>
          </div>
        </div>

        {/* Presence + session, kept from the previous header so the stream health stays visible */}
        <div className="w-full flex items-center gap-3 mt-1.5">
          <PresenceSummary snapshot={snapshot} registry={registry} />
          <SessionAge lastFrameAt={lastFrameAt} snapshot={snapshot} />
        </div>
      </header>

      {/* Latched E-STOP banner */}
      {estopLatched ? (
        <div className="bg-rose-950/90 border-b border-rose-500 px-4 py-2.5 text-xs text-rose-200">
          <div className="flex items-center gap-2 font-bold w-full justify-between">
            <div className="flex items-center gap-2">
              <OctagonAlert className="w-5 h-5 text-rose-400" />
              <span>
                ¡ENCLAVAMIENTO DE SEGURIDAD ACTIVADO! Todos los actuadores desarmados. Bus
                desacoplado.
              </span>
            </div>
            <button
              type="button"
              onClick={onClearEStop}
              className="bg-rose-800 hover:bg-rose-700 text-white px-3 py-1 rounded font-mono uppercase text-[11px] border border-rose-400"
            >
              Rearmar Sistema (Clear Latch)
            </button>
          </div>
        </div>
      ) : null}
    </>
  );
}
