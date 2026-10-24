'use client';

/**
 * The KPI ribbon: the six numbers that say what the bench is doing right now.
 *
 * Composition is deliberate — dissolved oxygen is the primary hero and gets the widest card
 * and the only colour on its border, because it is the quantity the whole experiment is about.
 * Everything else is context: saturation, temperature, the aerator's output, and the run clock.
 *
 * Two readings are *not* computed here:
 *
 * * **Saturation** is the sensor's own `od_sat_pct`, not DO divided by an assumed solubility.
 *   The empirical `C*` (~7.15 mg/L) is shown as a labelled reference, never substituted for
 *   the measurement.
 * * **The timer** is the gateway's `t_rel_s`, which excludes paused spans. The browser does
 *   not count — a backgrounded tab would then be wrong by however long it slept.
 */

import { Droplets, Fan, Percent, Thermometer, Timer } from 'lucide-react';
import type { LiveSnapshot, Registry, RunStateResponse } from '@/lib/types';
import { readDoDetail, readMotor } from '@/lib/registry';
import { formatDurationClock, formatNumber } from '@/lib/format';
import { cn } from './ui';

/** The empirical saturation used as a display reference. Not a derived quantity — see the
 *  module note. If a future deployment re-measures it, this constant is the one place. */
const C_STAR_REFERENCE_MG_L = 7.15;

function Card({
  accent,
  icon,
  title,
  tag,
  value,
  unit,
  footer,
  className,
}: {
  accent: string;
  icon: React.ReactNode;
  title: string;
  tag?: React.ReactNode;
  value: React.ReactNode;
  unit?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('bg-scada-well p-3 rounded-xl border border-scada-border flex flex-col justify-between', className)}>
      <div className="flex items-center justify-between text-xs">
        <span className={cn('font-bold uppercase tracking-wider flex items-center gap-1', accent)}>
          {icon}
          {title}
        </span>
        {tag}
      </div>
      <div className="my-1.5 flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-0.5 min-w-0">
          <span className="text-2xl lg:text-3xl font-extrabold font-mono text-white tracking-tight truncate">
            {value}
          </span>
          {unit}
        </div>
      </div>
      {footer ? <div className="text-[10px] text-slate-400 font-mono truncate">{footer}</div> : null}
    </div>
  );
}

export function KpiRibbon({
  registry,
  snapshot,
  run,
}: {
  registry: Registry | null;
  snapshot: LiveSnapshot | null;
  run: RunStateResponse | null;
}) {
  const doDetail = readDoDetail(registry, snapshot);
  const motor = readMotor(registry, snapshot);

  const doValue = doDetail.value;
  const doPrecision = doDetail.field?.precision ?? null;
  const doUnit = doDetail.field?.unit ?? 'mg/L';
  const sat = doDetail.saturationPct.value;
  const temp = doDetail.temperature.value;
  const tempUnit = doDetail.temperature.field?.unit ?? '°C';

  // Oxygen status, ordered so a stale reading outranks a healthy one: a two-minute-old
  // number is not evidence about right now.
  let doTag = { text: 'EN RANGO', cls: 'bg-emerald-950/80 text-emerald-400 border-emerald-800' };
  if (doDetail.missing) {
    doTag = { text: 'SIN DATOS', cls: 'bg-slate-800 text-slate-400 border-slate-700' };
  } else if (doDetail.stale) {
    doTag = { text: 'DESACTUALIZADO', cls: 'bg-amber-950/80 text-amber-400 border-amber-800' };
  } else if (doDetail.value === null) {
    doTag = { text: 'SENSOR MUDO', cls: 'bg-amber-950/80 text-amber-400 border-amber-800' };
  }

  const motorRunning = motor.running === true && !motor.stale;
  const timer = run?.state === 'ACTIVE_EXPERIMENT' ? run.t_rel_s : null;

  return (
    <section className="border-b border-scada-border bg-scada-surface/40 px-4 lg:px-6 py-2.5">
      <div className="w-full grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
        {/* HERO: dissolved oxygen — spans two columns on the widest layout so its extra
            size reads as importance rather than as an accident of the grid. */}
        <Card
          accent="text-telemetry-do"
          icon={<Droplets className="w-3.5 h-3.5" />}
          title="Oxígeno (DO)"
          tag={
            <span className={cn('text-[10px] font-mono px-1.5 py-0.5 rounded border', doTag.cls)}>
              {doTag.text}
            </span>
          }
          value={formatNumber(doValue, doPrecision)}
          unit={<span className="text-xs font-semibold text-telemetry-do ml-0.5">{doUnit}</span>}
          footer={
            <span>
              {doDetail.source === 'peer'
                ? `eco de ${doDetail.role}`
                : doDetail.source === 'none'
                  ? 'sin lectura autoritativa'
                  : `fuente: ${doDetail.role}`}
            </span>
          }
          className={cn(
            'md:col-span-2 xl:col-span-2 border',
            doDetail.value !== null && doDetail.value < 3
              ? 'border-rose-500/50'
              : doDetail.value !== null && doDetail.value < 5
                ? 'border-amber-500/50'
                : 'border-telemetry-do/30',
          )}
        />

        {/* Saturation — the sensor's own field, not derived */}
        <Card
          accent="text-telemetry-mixer"
          icon={<Percent className="w-3.5 h-3.5" />}
          title="Saturación"
          tag={<span className="text-[9px] text-slate-500 font-mono">OPTOD</span>}
          value={formatNumber(sat, doDetail.saturationPct.field?.precision ?? null)}
          unit={<span className="text-xs text-slate-400 ml-0.5">% Sat</span>}
          footer={<span>Ref. C*: {C_STAR_REFERENCE_MG_L} mg/L</span>}
        />

        {/* Temperature */}
        <Card
          accent="text-telemetry-temp"
          icon={<Thermometer className="w-3.5 h-3.5" />}
          title="Temp. Agua"
          tag={<span className="text-[9px] text-slate-500 font-mono">OPTOD</span>}
          value={formatNumber(temp, doDetail.temperature.field?.precision ?? null)}
          unit={<span className="text-xs text-slate-400 ml-0.5">{tempUnit}</span>}
          footer={<span>Vbat sensor: {formatNumber(doDetail.batteryV.value, 2)} V</span>}
        />

        {/* Aerator */}
        <Card
          accent="text-telemetry-motor"
          icon={<Fan className="w-3.5 h-3.5" />}
          title="Aireador ODrive"
          tag={
            <span
              className={cn(
                'text-[10px] font-mono px-1.5 py-0.5 rounded border',
                motor.running === null
                  ? 'bg-slate-800 text-slate-400 border-slate-700'
                  : motorRunning
                    ? 'bg-amber-950/80 text-amber-400 border-amber-800'
                    : 'bg-slate-800 text-slate-400 border-slate-700',
              )}
            >
              {motor.running === null ? 'SIN DATOS' : motorRunning ? 'EN MARCHA' : 'DETENIDO'}
            </span>
          }
          value={formatNumber(motor.rpm.value, motor.rpm.field?.precision ?? null)}
          unit={<span className="text-xs text-telemetry-motor ml-0.5">RPM</span>}
          footer={
            <span>
              {formatNumber(motor.powerW.value, motor.powerW.field?.precision ?? null)} W ·{' '}
              {formatNumber(motor.currentA.value, motor.currentA.field?.precision ?? null)} A
            </span>
          }
        />

        {/* Run timer */}
        <Card
          accent="text-telemetry-do"
          icon={<Timer className="w-3.5 h-3.5" />}
          title="Tiempo (t)"
          tag={
            <span
              className={cn(
                'text-[9px] font-mono',
                run?.paused ? 'text-amber-400' : run?.state === 'ACTIVE_EXPERIMENT' ? 'text-emerald-400' : 'text-slate-500',
              )}
            >
              {run?.paused
                ? '● PAUSADO'
                : run?.state === 'ACTIVE_EXPERIMENT'
                  ? '● EXP ACTIVO'
                  : '○ REPOSO'}
            </span>
          }
          value={formatDurationClock(timer)}
          footer={
            <span className="truncate">
              {run?.recipe_id ? `fase: ${run.phase_num ?? '—'} / ${run.phases.length}` : 'sin ensayo activo'}
            </span>
          }
        />
      </div>
    </section>
  );
}
