'use client';

/**
 * The free multivariable inspector.
 *
 * The observatory answers one question well; this answers "and what else was happening". Any
 * registry-backed metric can be placed on the left axis or the right, so a power/current pair,
 * a motor-speed pair, or DO-and-temperature can be read together without a second panel.
 *
 * Two axes, not one, because the quantities do not share a scale — plotting 24 V beside 1.6 A
 * on a single axis flattens the current into the baseline. Which axis a series belongs on is
 * fixed by the preset, not chosen per render, so the same variable keeps the same axis and the
 * chart stays comparable between looks.
 *
 * Visibility is a hide, not an unmount: a nulled series would break the axis-title computation
 * and make the domain jump, which is exactly the "phantom axis" the previous chart suffered.
 */

import { useMemo, useState } from 'react';
import '@/lib/chartSetup';
import { Line } from 'react-chartjs-2';
import { Layers, SlidersHorizontal } from 'lucide-react';
import { getSamples } from '@/lib/api';
import { useAsync } from '@/hooks/useAsync';
import { formatNumber } from '@/lib/format';
import { formatTime } from '@/lib/dateUtils';
import type { Registry } from '@/lib/types';
import { EmptyState, ErrorState, Loading, Panel, cn } from './ui';

const RANGES = ['10m', '1h'] as const;
type Range = (typeof RANGES)[number];

type Side = 'left' | 'right';

interface VarDef {
  key: string;
  role: string;
  field: string;
  label: string;
  unit: string | null;
  precision: number | null;
  side: Side;
  color: string;
}

/** Derived traces: no role/field to fetch, built in `points` from a fetched sibling series. */
const DERIVED: Record<string, (row: Record<string, number | null>) => number | null> = {
  error: (row) => (typeof row.do === 'number' ? ERROR_SETPOINT_MG_L - row.do : null),
};

/** The catalogue, hard-keyed to fields the registry actually carries. `field` must exist on
 *  `role`; a missing one renders as a gap rather than an error, which is truthful. */
const VAR_DEFS: VarDef[] = [
  { key: 'power', role: 'aerator', field: 'drv_power_w', label: 'Potencia ODrive', unit: 'W', precision: 1, side: 'left', color: '#fb923c' },
  { key: 'voltage', role: 'aerator', field: 'drv_vbus_v', label: 'Tensión bus', unit: 'V', precision: 2, side: 'left', color: '#10b981' },
  { key: 'odriveRpm', role: 'aerator', field: 'drv_rpm', label: 'Velocidad ODrive', unit: 'rpm', precision: 0, side: 'left', color: '#f59e0b' },
  { key: 'do', role: 'od_sensor', field: 'od_do_mg_l', label: 'Oxígeno disuelto', unit: 'mg/L', precision: 3, side: 'left', color: '#38bdf8' },
  { key: 'current', role: 'aerator', field: 'drv_ibus_a', label: 'Corriente bus', unit: 'A', precision: 2, side: 'right', color: '#facc15' },
  { key: 'mixerRpm', role: 'mixer', field: 'drv_rpm', label: 'Mezclador PI', unit: 'rpm', precision: 0, side: 'right', color: '#14b8a6' },
  { key: 'temp', role: 'od_sensor', field: 'od_temp_c', label: 'Temp. agua', unit: '°C', precision: 2, side: 'right', color: '#60a5fa' },
  { key: 'pumpVol', role: 'pump', field: 'pump_dosed_ml', label: 'Bomba dosificada', unit: 'mL', precision: 2, side: 'right', color: '#a855f7' },
  { key: 'error', role: '', field: '', label: 'Error e(t) = SP − DO', unit: 'mg/L', precision: 3, side: 'right', color: '#f43f5e' },
  { key: 'dDoDt', role: '', field: '', label: 'Derivada d[DO]/dt', unit: 'mg/L/min', precision: 3, side: 'right', color: '#c084fc' },
  { key: 'drvStatus', role: 'aerator', field: 'drv_status', label: 'Falla ODrive (active_errors)', unit: null, precision: 0, side: 'right', color: '#ef4444' },
  { key: 'drvState', role: 'aerator', field: 'drv_state', label: 'Estado Eje ODrive (state)', unit: null, precision: 0, side: 'right', color: '#38bdf8' },
  { key: 'disarmReason', role: 'aerator', field: 'drv_disarm_reason', label: 'Causa Desarme ODrive', unit: null, precision: 0, side: 'right', color: '#f97316' },
];

const PRESETS: { id: string; label: string; keys: string[] }[] = [
  { id: 'power_current', label: '⚡ Potencia & Corriente', keys: ['power', 'current'] },
  { id: 'voltage_current', label: '🔋 Tensión (Izq) vs Corriente (Der)', keys: ['voltage', 'current'] },
  { id: 'motors', label: '🔄 Motores (ODrive + Mezclador)', keys: ['odriveRpm', 'mixerRpm'] },
  { id: 'water_quality', label: '💧 DO (mg/L) & Temp (°C)', keys: ['do', 'temp'] },
  { id: 'do_derivative', label: '📈 DO & Derivada d[DO]/dt', keys: ['do', 'dDoDt'] },
  { id: 'aerator_diagnostics', label: '🔍 Diagnóstico Aireador', keys: ['odriveRpm', 'voltage', 'current', 'drvStatus', 'drvState'] },
  { id: 'error_control', label: '🎯 Error Control e(t)', keys: ['do', 'error'] },
];

/** The control setpoint the error trace is measured against, mg/L. A constant for now, because
 *  the running recipe's setpoint is not exposed on the live payload; when it is, this reads it. */
const ERROR_SETPOINT_MG_L = 6.0;

const ODRIVE_ERROR_MAP: Record<number, string> = {
  0x0001: 'INITIALIZING',
  0x0002: 'SYSTEM_LEVEL',
  0x0004: 'TIMING_ERROR',
  0x0008: 'MISSING_ESTIMATE',
  0x0010: 'BAD_CONFIG',
  0x0020: 'DRV_FAULT',
  0x0040: 'MISSING_INPUT',
  0x0080: 'DC_BUS_OVER_VOLTAGE',
  0x0100: 'DC_BUS_UNDER_VOLTAGE',
  0x0200: 'DC_BUS_OVER_CURRENT',
  0x0400: 'DC_BUS_OVER_REGEN',
  0x0800: 'CURRENT_LIMIT',
  0x1000: 'MOTOR_OVER_TEMP',
  0x2000: 'INVERTER_OVER_TEMP',
  0x4000: 'VELOCITY_LIMIT',
  0x8000: 'POSITION_LIMIT',
  0x10000: 'WATCHDOG_EXPIRED',
  0x20000: 'ESTOP_REQUESTED',
  0x40000: 'SPINOUT_DETECTED',
  0x80000: 'BRAKE_RESISTOR',
  0x100000: 'THERMISTOR_DISCONNECTED',
  0x200000: 'CALIBRATION_ERROR',
};

function formatOdriveError(code: number | null): string {
  if (code === null || code === undefined) return '--';
  if (code === 0) return '0 (OK)';
  const errs: string[] = [];
  for (const [maskStr, name] of Object.entries(ODRIVE_ERROR_MAP)) {
    const mask = Number(maskStr);
    if ((code & mask) !== 0) errs.push(name);
  }
  return errs.length > 0 ? `0x${code.toString(16).toUpperCase()} (${errs.join(', ')})` : `0x${code.toString(16).toUpperCase()}`;
}

export function MultivariableInspector({ registry }: { registry: Registry | null }) {
  const [range, setRange] = useState<Range>('10m');
  const [active, setActive] = useState<Set<string>>(new Set(['power', 'current']));
  const [axisOpen, setAxisOpen] = useState(false);
  const [emaAerator, setEmaAerator] = useState(true);

  // Axis domains. Auto is the default; a manual override pins min/max for one side. Kept as
  // explicit state (not derived) because the operator sets it deliberately and expects it to
  // survive a variable toggle — a manual scale that reset whenever a checkbox changed would
  // defeat the point of setting it.
  const [leftAuto, setLeftAuto] = useState(true);
  const [rightAuto, setRightAuto] = useState(true);
  const [leftMin, setLeftMin] = useState(0);
  const [leftMax, setLeftMax] = useState(600);
  const [rightMin, setRightMin] = useState(0);
  const [rightMax, setRightMax] = useState(15);

  // One fetch per active variable. Cheap at these spans (raw archive tail), and it keeps each
  // series on its own node's clock rather than resampling them onto a shared one.
  const series = useMemo(() => {
    const pending: Record<string, { role: string; field: string }> = {};
    for (const def of VAR_DEFS) {
      // `error` and `dDoDt` are derived from the DO series, not fetched on their own.
      if (active.has(def.key) && def.role) pending[def.key] = { role: def.role, field: def.field };
    }
    if ((active.has('error') || active.has('dDoDt')) && !pending.do) {
      pending.do = { role: 'od_sensor', field: 'od_do_mg_l' };
    }
    return pending;
  }, [active]);

  const data = useAsync(
    async () => {
      const entries = await Promise.all(
        Object.entries(series).map(async ([key, s]) => {
          const res = await getSamples(s.role, s.field, range, 3000);
          return [key, res.data] as const;
        }),
      );
      return Object.fromEntries(entries) as Record<string, { bucket: string; value: number | null }[]>;
    },
    [series, range],
  );

  const points = useMemo(() => {
    const byMs = new Map<number, Record<string, number | null> & { ms: number }>();
    for (const [key, rows] of Object.entries(data.data ?? {})) {
      for (const row of rows) {
        const ms = new Date(row.bucket).getTime();
        const entry = byMs.get(ms) ?? { ms };
        let val = row.value;
        if (key === 'odriveRpm' && val !== null) {
          val = val > 650 ? null : Math.min(600, Math.max(0, val));
        }
        entry[key] = val;
        byMs.set(ms, entry);
      }
    }
    const merged = Array.from(byMs.values()).sort((a, b) => a.ms - b.ms);

    // Continuous Zero-Order Hold across all active telemetry series:
    // Ensures all selected curves have synchronized values at every point in time so
    // the multivariable inspector tooltip always presents all variables together.
    for (const def of VAR_DEFS) {
      if (!active.has(def.key)) continue;
      let lastVal: number | null = null;
      for (const row of merged) {
        if (row[def.key] !== undefined && row[def.key] !== null) {
          lastVal = row[def.key];
        } else if (lastVal !== null) {
          row[def.key] = lastVal;
        }
      }
      let nextVal: number | null = null;
      for (let i = merged.length - 1; i >= 0; i--) {
        if (merged[i][def.key] !== undefined && merged[i][def.key] !== null) {
          nextVal = merged[i][def.key];
        } else if (nextVal !== null) {
          merged[i][def.key] = nextVal;
        }
      }
    }
    for (const def of VAR_DEFS) {
      const derive = DERIVED[def.key];
      if (!active.has(def.key) || !derive) continue;
      for (const row of merged) row[def.key] = derive(row);
    }

    // Compute d[DO]/dt derivative (rate of change in mg/L/min) via OLS linear regression
    if (active.has('dDoDt')) {
      const doRows = merged.filter((r) => typeof r.do === 'number' && Number.isFinite(r.do));
      const WINDOW_MS = 30_000; // 30-second window gives smooth rate per minute
      for (let i = 0; i < merged.length; i++) {
        const curMs = merged[i].ms;
        const windowPts = doRows.filter((r) => r.ms <= curMs && r.ms >= curMs - WINDOW_MS);
        if (windowPts.length >= 3) {
          const t0 = windowPts[0].ms / 1000;
          let sumT = 0;
          let sumY = 0;
          let sumT2 = 0;
          let sumTY = 0;
          const n = windowPts.length;
          for (const pt of windowPts) {
            const t = pt.ms / 1000 - t0;
            const y = pt.do as number;
            sumT += t;
            sumY += y;
            sumT2 += t * t;
            sumTY += t * y;
          }
          const denom = n * sumT2 - sumT * sumT;
          if (denom > 1e-6) {
            const slopePerSec = (n * sumTY - sumT * sumY) / denom;
            const ratePerMin = slopePerSec * 60;
            merged[i].dDoDt = Math.round(ratePerMin * 1000) / 1000;
          } else {
            merged[i].dDoDt = null;
          }
        } else {
          merged[i].dDoDt = null;
        }
      }
    }

    // Apply EMA filter to aerator variables to filter 5 Hz ODrive jitter
    if (emaAerator) {
      const alpha = 0.2;
      for (const def of VAR_DEFS) {
        if (def.role === 'aerator' && active.has(def.key)) {
          let prev: number | null = null;
          for (const row of merged) {
            const val = row[def.key];
            if (val !== undefined && val !== null) {
              if (prev === null) {
                prev = val;
              } else {
                prev = alpha * val + (1 - alpha) * prev;
              }
              row[def.key] = Math.round(prev * 100) / 100;
            }
          }
        }
      }
    }

    let finalPoints = merged;
    if (finalPoints.length > 2000) {
      const step = Math.ceil(finalPoints.length / 2000);
      finalPoints = finalPoints.filter((_, idx) => idx % step === 0 || idx === finalPoints.length - 1);
    }

    // Ensure strictly increasing x-axis coordinates to prevent Recharts monotone spline division by zero (dx = 0 -> NaN)
    const strictlyIncreasing: typeof finalPoints = [];
    let lastX = -Infinity;
    for (const pt of finalPoints) {
      if (pt.ms !== null && Number.isFinite(pt.ms) && pt.ms > lastX) {
        strictlyIncreasing.push(pt);
        lastX = pt.ms;
      }
    }

    return strictlyIncreasing;
  }, [data.data, active, emaAerator]);

  const visible = VAR_DEFS.filter((d) => (active.has(d.key) && (series[d.key] || DERIVED[d.key])));
  const leftVars = visible.filter((v) => v.side === 'left');
  const rightVars = visible.filter((v) => v.side === 'right');

  const toggle = (key: string) => {
    setActive((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const applyPreset = (keys: string[]) => setActive(new Set(keys));

  const axisTitle = (vars: VarDef[]) =>
    vars.length === 0 ? '' : vars.map((v) => (v.unit ? `${v.label} (${v.unit})` : v.label)).join(' • ');

  const chartData = useMemo(() => {
    return {
      datasets: visible.map((v) => ({
        label: v.label,
        yAxisID: v.side,
        data: points.map((p) => ({ x: p.ms, y: p[v.key] ?? null })),
        borderColor: v.color,
        backgroundColor: 'transparent',
        borderWidth: 1.8,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.2,
        spanGaps: true,
      })),
    };
  }, [visible, points]);

  const chartOptions = useMemo(() => {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false as const,
      interaction: {
        mode: 'index' as const,
        intersect: false,
      },
      scales: {
        x: {
          type: 'linear' as const,
          grid: { color: '#1c2436' },
          ticks: {
            color: '#94a3b8',
            font: { size: 10, family: 'monospace' },
            callback: (v: number | string) => formatTime(Number(v), true),
            maxRotation: 0,
          },
        },
        left: {
          type: 'linear' as const,
          position: 'left' as const,
          display: leftVars.length > 0,
          min: leftAuto ? undefined : leftMin,
          max: leftAuto ? undefined : leftMax,
          grid: { color: '#1c2436' },
          ticks: { color: '#cbd5e1', font: { size: 10, family: 'monospace' } },
          title: {
            display: leftVars.length > 0,
            text: axisTitle(leftVars),
            color: '#cbd5e1',
            font: { size: 9, family: 'monospace' },
          },
        },
        right: {
          type: 'linear' as const,
          position: 'right' as const,
          display: rightVars.length > 0,
          min: rightAuto ? undefined : rightMin,
          max: rightAuto ? undefined : rightMax,
          grid: { drawOnChartArea: false },
          ticks: { color: '#facc15', font: { size: 10, family: 'monospace' } },
          title: {
            display: rightVars.length > 0,
            text: axisTitle(rightVars),
            color: '#facc15',
            font: { size: 9, family: 'monospace' },
          },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          position: 'cursor' as any,
          mode: 'index' as const,
          intersect: false,
          backgroundColor: 'rgba(11, 15, 25, 0.97)',
          borderColor: 'rgba(56, 189, 248, 0.5)',
          borderWidth: 1.5,
          cornerRadius: 8,
          padding: { top: 12, bottom: 12, left: 16, right: 16 },
          itemSpacing: 8,
          caretSize: 6,
          caretPadding: 8,
          usePointStyle: true,
          boxWidth: 12,
          boxHeight: 12,
          boxPadding: 8,
          titleColor: '#ffffff',
          titleFont: { size: 14, family: 'Inter, system-ui, sans-serif', weight: 'bold' as const },
          titleSpacing: 8,
          bodyColor: '#f1f5f9',
          bodyFont: { size: 13.5, family: 'Inter, system-ui, sans-serif', weight: '500' as const },
          callbacks: {
            title: (items: any[]) => (items.length ? `🕒 ${formatTime(items[0].parsed.x, true)}` : ''),
            label: (item: any) => {
              const v = visible.find((d) => d.label === item.dataset.label);
              const val = item.parsed.y;
              if (!v) return `${item.dataset.label}: ${val}`;
              const formatted = v.key === 'drvStatus' ? formatOdriveError(val) : formatNumber(val, v.precision);
              return `${v.label}: ${formatted}${v.unit && v.key !== 'drvStatus' ? ` ${v.unit}` : ''}`;
            },
          },
        },
      },
    };
  }, [leftVars, rightVars, leftAuto, leftMin, leftMax, rightAuto, rightMin, rightMax, visible]);

  return (
    <Panel
      title="Inspector multivariable"
      subtitle="Ejes izquierdo y derecho independientes · series crudas del archivo"
      icon={<Layers className="w-4 h-4 text-emerald-400" />}
      actions={
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setEmaAerator((v) => !v)}
            className={cn(
              'px-2 py-1 rounded text-[10px] font-mono transition border',
              emaAerator
                ? 'bg-scada-elevated text-emerald-400 border-emerald-600/80 font-bold'
                : 'bg-scada-well text-slate-500 border-scada-border',
            )}
            title="Filtro EMA (Media Móvil Exponencial) para variables del aerador (ODrive)"
          >
            EMA Aerador: {emaAerator ? 'ON' : 'OFF'}
          </button>
          <button
            type="button"
            onClick={() => setAxisOpen((v) => !v)}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-scada-elevated hover:bg-slate-700 text-telemetry-do border border-telemetry-do/40 transition text-[10px] font-mono"
            title="Ajustar límites de escala y asignación de ejes"
          >
            <SlidersHorizontal className="w-3.5 h-3.5" />
            Configurar Ejes
          </button>
          <div className="flex items-center gap-1 bg-scada-well rounded-lg p-0.5 border border-scada-border text-xs font-mono">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setRange(r)}
                className={cn(
                  'px-2 py-0.5 rounded transition',
                  range === r ? 'bg-scada-elevated text-emerald-400 font-bold border border-scada-border' : 'text-slate-400 hover:text-white',
                )}
              >
                {r}
              </button>
            ))}
          </div>
        </div>
      }
    >
      {/* Axis configuration panel */}
      {axisOpen ? (
        <div className="p-3 bg-scada-well rounded-xl border border-telemetry-do/40 text-xs font-mono space-y-3">
          <div className="flex items-center justify-between border-b border-scada-border pb-1.5">
            <span className="font-bold text-white flex items-center gap-1.5 text-xs">
              <SlidersHorizontal className="w-3.5 h-3.5 text-telemetry-do" /> Calibración & escalas
              de ejes Y (izquierdo / derecho)
            </span>
            <button
              type="button"
              onClick={() => setAxisOpen(false)}
              className="text-slate-400 hover:text-white text-xs px-1.5 py-0.5 rounded bg-scada-elevated border border-scada-border"
            >
              ✕ Cerrar
            </button>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-[11px]">
            <div className="p-2.5 rounded bg-scada-surface border border-scada-border space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-bold text-telemetry-do">Eje Y izquierdo</span>
                <label className="flex items-center gap-1 text-[10px] text-slate-400 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={leftAuto}
                    onChange={(e) => setLeftAuto(e.target.checked)}
                    className="accent-current text-telemetry-do"
                  />
                  <span>Auto-escala</span>
                </label>
              </div>
              {!leftAuto ? (
                <div className="flex gap-2">
                  <div>
                    <span className="text-[9px] text-slate-500 block">Mín:</span>
                    <input
                      type="number"
                      value={leftMin}
                      onChange={(e) => setLeftMin(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-[10px] border border-scada-border rounded px-1.5 py-0.5 font-mono"
                    />
                  </div>
                  <div>
                    <span className="text-[9px] text-slate-500 block">Máx:</span>
                    <input
                      type="number"
                      value={leftMax}
                      onChange={(e) => setLeftMax(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-[10px] border border-scada-border rounded px-1.5 py-0.5 font-mono"
                    />
                  </div>
                </div>
              ) : (
                <div className="text-[9px] text-slate-500 font-mono">
                  {axisTitle(leftVars) || 'sin variables'}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-1 pt-1 border-t border-scada-border/60 text-[9px]">
                <span className="text-slate-500">Presets:</span>
                <button
                  type="button"
                  onClick={() => setLeftAuto(true)}
                  className={cn('px-1.5 py-0.5 rounded border', leftAuto ? 'bg-sky-950/60 border-sky-500 text-sky-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  Auto
                </button>
                <button
                  type="button"
                  onClick={() => { setLeftAuto(false); setLeftMin(0); setLeftMax(600); }}
                  className={cn('px-1.5 py-0.5 rounded border', !leftAuto && leftMin === 0 && leftMax === 600 ? 'bg-sky-950/60 border-sky-500 text-sky-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  0–600 (ODrive)
                </button>
                <button
                  type="button"
                  onClick={() => { setLeftAuto(false); setLeftMin(0); setLeftMax(10); }}
                  className={cn('px-1.5 py-0.5 rounded border', !leftAuto && leftMin === 0 && leftMax === 10 ? 'bg-sky-950/60 border-sky-500 text-sky-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  0–10 (OD mg/L)
                </button>
                <button
                  type="button"
                  onClick={() => { setLeftAuto(false); setLeftMin(0); setLeftMax(30); }}
                  className={cn('px-1.5 py-0.5 rounded border', !leftAuto && leftMin === 0 && leftMax === 30 ? 'bg-sky-950/60 border-sky-500 text-sky-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  0–30 (V bus)
                </button>
              </div>
            </div>

            <div className="p-2.5 rounded bg-scada-surface border border-scada-border space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-bold text-amber-300">Eje Y derecho</span>
                <label className="flex items-center gap-1 text-[10px] text-slate-400 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={rightAuto}
                    onChange={(e) => setRightAuto(e.target.checked)}
                    className="accent-current text-amber-400"
                  />
                  <span>Auto-escala</span>
                </label>
              </div>
              {!rightAuto ? (
                <div className="flex gap-2">
                  <div>
                    <span className="text-[9px] text-slate-500 block">Mín:</span>
                    <input
                      type="number"
                      value={rightMin}
                      onChange={(e) => setRightMin(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-[10px] border border-scada-border rounded px-1.5 py-0.5 font-mono"
                    />
                  </div>
                  <div>
                    <span className="text-[9px] text-slate-500 block">Máx:</span>
                    <input
                      type="number"
                      value={rightMax}
                      onChange={(e) => setRightMax(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-[10px] border border-scada-border rounded px-1.5 py-0.5 font-mono"
                    />
                  </div>
                </div>
              ) : (
                <div className="text-[9px] text-slate-500 font-mono">
                  {axisTitle(rightVars) || 'sin variables'}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-1 pt-1 border-t border-scada-border/60 text-[9px]">
                <span className="text-slate-500">Presets:</span>
                <button
                  type="button"
                  onClick={() => setRightAuto(true)}
                  className={cn('px-1.5 py-0.5 rounded border', rightAuto ? 'bg-amber-950/60 border-amber-500 text-amber-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  Auto
                </button>
                <button
                  type="button"
                  onClick={() => { setRightAuto(false); setRightMin(0); setRightMax(15); }}
                  className={cn('px-1.5 py-0.5 rounded border', !rightAuto && rightMin === 0 && rightMax === 15 ? 'bg-amber-950/60 border-amber-500 text-amber-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  0–15 (A bus)
                </button>
                <button
                  type="button"
                  onClick={() => { setRightAuto(false); setRightMin(0); setRightMax(1500); }}
                  className={cn('px-1.5 py-0.5 rounded border', !rightAuto && rightMin === 0 && rightMax === 1500 ? 'bg-amber-950/60 border-amber-500 text-amber-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  0–1500 (Mezclador)
                </button>
                <button
                  type="button"
                  onClick={() => { setRightAuto(false); setRightMin(15); setRightMax(35); }}
                  className={cn('px-1.5 py-0.5 rounded border', !rightAuto && rightMin === 15 && rightMax === 35 ? 'bg-amber-950/60 border-amber-500 text-amber-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  15–35 (°C)
                </button>
                <button
                  type="button"
                  onClick={() => { setRightAuto(false); setRightMin(-1); setRightMax(1); }}
                  className={cn('px-1.5 py-0.5 rounded border', !rightAuto && rightMin === -1 && rightMax === 1 ? 'bg-amber-950/60 border-amber-500 text-amber-300 font-bold' : 'bg-scada-well border-scada-border text-slate-400')}
                >
                  ±1 (d[DO]/dt)
                </button>
              </div>
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-1 border-t border-scada-border">
            <button
              type="button"
              onClick={() => setAxisOpen(false)}
              className="px-3 py-1 bg-sky-600 hover:bg-sky-500 text-white rounded font-bold text-xs transition"
            >
              Aplicar escalas
            </button>
          </div>
        </div>
      ) : null}

      {/* Presets */}
      <div className="flex flex-wrap items-center gap-2 font-mono text-[10px]">
        <span className="text-slate-500">Presets:</span>
        {PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => applyPreset(p.keys)}
            className="px-2 py-1 rounded bg-scada-elevated hover:bg-slate-700 text-slate-300 border border-scada-border transition"
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Variable pills — the authoritative legend */}
      <div className="flex flex-wrap items-center gap-2 text-xs font-mono bg-scada-well p-2.5 rounded-xl border border-scada-border">
        <span className="text-[10px] text-slate-400 font-bold uppercase mr-1">Variables:</span>
        {VAR_DEFS.map((def) => {
          // A variable whose role is not in the registry cannot be shown at all. Derived traces
          // have no role and are always available.
          const known = registry?.roles?.[def.role];
          const disabled = registry !== null && !DERIVED[def.key] && !known;
          return (
            <label
              key={def.key}
              title={disabled ? `rol ${def.role} no declarado` : def.label}
              className={cn(
                'inline-flex items-center gap-1.5 px-2 py-1 rounded border cursor-pointer text-[11px] transition',
                active.has(def.key)
                  ? 'bg-scada-elevated text-slate-100 border-scada-border'
                  : 'bg-scada-well text-slate-500 border-scada-border/60 hover:text-slate-300',
                disabled && 'opacity-40 cursor-not-allowed',
              )}
            >
              <input
                type="checkbox"
                checked={active.has(def.key)}
                disabled={disabled}
                onChange={() => toggle(def.key)}
                className="accent-current"
                style={{ accentColor: def.color }}
              />
              <span
                className="w-2 h-2 rounded-full"
                style={{ backgroundColor: def.color }}
              />
              {def.label}
              <span className="text-[9px] text-slate-500">[{def.side === 'left' ? 'Izq' : 'Der'}]</span>
            </label>
          );
        })}
      </div>

      {data.loading && points.length === 0 ? <Loading label="Leyendo series" /> : null}
      {data.error ? <ErrorState error={data.error} onRetry={data.reload} compact /> : null}

      {!data.loading && !data.error && points.length === 0 ? (
        <EmptyState message="Sin datos para las variables activas." hint="Activa una variable o amplía el rango." />
      ) : null}

      {points.length > 0 ? (
        <div className="h-[260px] relative w-full bg-scada-well rounded-xl p-2 border border-scada-border">
          <Line data={chartData} options={chartOptions as any} />
        </div>
      ) : null}
    </Panel>
  );
}
