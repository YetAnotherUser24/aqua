'use client';

/**
 * The observatory: dissolved oxygen against the aerator's speed, with the run's milestones
 * overlaid.
 *
 * Migrated from Recharts to Chart.js (HTML5 Canvas).
 * Milestones are rendered as vertical dotted lines with header badges.
 * Telemetry is resampled to 1.0 Hz with tension: 0 (linear) for smooth 60 FPS rendering.
 * Dual-axis Delta Cursors:
 *   - X-Axis: 2 vertical cursors (X1 in bright cyan, X2 in bright amber) for measuring Delta t
 *   - Y-Axis: 2 horizontal cursors (Y1 in bright cyan, Y2 in bright amber) for measuring Delta OD
 *   - Manipulable via direct canvas click, real-time sliders, and fine-step stepper buttons.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import '@/lib/chartSetup';
import { Line } from 'react-chartjs-2';
import type { Chart as ChartJSInstance } from 'chart.js';
import {
  BarChart3,
  BookmarkPlus,
  Calendar,
  Camera,
  ChevronLeft,
  ChevronRight,
  Crosshair,
  Eye,
  EyeOff,
  MoveHorizontal,
  MoveVertical,
  Pause,
  Play,
  RotateCcw,
  SlidersHorizontal,
} from 'lucide-react';
import { getHistory, getMilestones, getSamples } from '@/lib/api';
import { useAsync, useInterval } from '@/hooks/useAsync';
import { formatRelativeClock, formatNumber } from '@/lib/format';
import { formatTime, formatDateTime } from '@/lib/dateUtils';
import { resampleTo1Hz } from '@/lib/chartUtils';
import type { Milestone, Registry, RunStateResponse } from '@/lib/types';
import { EmptyState, ErrorState, Loading, Panel, cn } from './ui';

const RANGES = ['10m', '1h', '24h', '7d'] as const;
type Range = (typeof RANGES)[number];

const RAW_RANGES: readonly Range[] = ['10m', '1h'];
const isRawRange = (r: Range) => RAW_RANGES.includes(r);

type AxisMode = 'relative' | 'clock';

interface Point {
  ms: number;
  tRel: number | null;
  do: number | null;
  rpm: number | null;
  mixerRpm: number | null;
}

interface Mark {
  ms: number;
  tRel: number | null;
  do: number;
  label: string;
  detail: string;
  kind: string;
}

export function ObservatoryChart({
  registry,
  run,
  milestoneVersion = 0,
  onAddMilestone,
}: {
  registry: Registry | null;
  run: RunStateResponse | null;
  milestoneVersion?: number;
  onAddMilestone: () => void;
}) {
  const chartRef = useRef<ChartJSInstance<'line'> | null>(null);

  const [range, setRange] = useState<string>('10m');
  const [customRange, setCustomRange] = useState<string | null>(null);
  const [customOpen, setCustomOpen] = useState(false);
  const [axisMode, setAxisMode] = useState<AxisMode>('relative');
  const [showRpm, setShowRpm] = useState(true);
  const [showMixerRpm, setShowMixerRpm] = useState(true);
  const [showDo, setShowDo] = useState(true);
  const [emaFilter, setEmaFilter] = useState(true);
  const [axisConfigOpen, setAxisConfigOpen] = useState(false);
  const [doAuto, setDoAuto] = useState(true);
  const [doMin, setDoMin] = useState(0);
  const [doMax, setDoMax] = useState(8);
  const [rpmAuto, setRpmAuto] = useState(true);
  const [rpmMin, setRpmMin] = useState(0);
  const [rpmMax, setRpmMax] = useState(600);

  // SCADA specific features:
  const [isPaused, setIsPaused] = useState(false);
  const [isZoomed, setIsZoomed] = useState(false);
  const [zoomSpan, setZoomSpan] = useState<number | null>(null);
  const [fixedZoomDomain, setFixedZoomDomain] = useState<{ min: number; max: number } | null>(null);
  const [concreteInterval, setConcreteInterval] = useState<{ start: number; end: number; label: string } | null>(null);

  // Dual-Axis Delta Cursors (2 for X-axis, 2 for Y-axis)
  const [showDeltaCursors, setShowDeltaCursors] = useState(false);
  const [cursorAxes, setCursorAxes] = useState<'x' | 'y' | 'both'>('both');
  const [activeCursorTarget, setActiveCursorTarget] = useState<'x1' | 'x2' | 'y1' | 'y2'>('x1');
  const [snapCursors, setSnapCursors] = useState(true);
  const [cursor1X, setCursor1X] = useState<number | null>(null);
  const [cursor2X, setCursor2X] = useState<number | null>(null);
  const [cursor1Y, setCursor1Y] = useState<number | null>(null);
  const [cursor2Y, setCursor2Y] = useState<number | null>(null);

  const raw = useMemo(() => {
    if ((RANGES as readonly string[]).includes(range)) {
      return isRawRange(range as Range);
    }
    if (range.endsWith('m')) {
      const m = parseInt(range, 10);
      return !isNaN(m) && m <= 120;
    }
    if (range.endsWith('h')) {
      const h = parseInt(range, 10);
      return !isNaN(h) && h <= 6;
    }
    return false;
  }, [range]);

  const experimentId = run?.experiment_id ?? null;
  const t0ms = run?.started_at ? new Date(run.started_at).getTime() : null;

  const sampleLimit = useMemo(() => {
    if (range.endsWith('h')) {
      const hours = parseInt(range, 10) || 1;
      return Math.min(60_000, Math.max(15_000, hours * 15_000));
    }
    if (range.endsWith('d')) {
      return 60_000;
    }
    if (range.endsWith('m')) {
      const mins = parseInt(range, 10) || 10;
      return Math.min(40_000, Math.max(5_000, mins * 60 * 6));
    }
    return 20_000;
  }, [range]);

  interface SeriesPayload {
    data: Array<{ bucket: string; value: number | null }>;
  }

  // When a concrete interval is active, query '3650d' (full run) from archive so data for that interval exists
  const effectiveRange = concreteInterval ? '3650d' : range;

  const doSample = useAsync<SeriesPayload>(
    () => (raw || concreteInterval ? getSamples('od_sensor', 'od_do_mg_l', effectiveRange, sampleLimit) : getHistory('od_sensor', 'od_do_mg_l', effectiveRange)),
    [raw, effectiveRange, sampleLimit, concreteInterval],
  );
  const rpmSample = useAsync<SeriesPayload>(
    () => (raw || concreteInterval ? getSamples('aerator', 'drv_rpm', effectiveRange, sampleLimit) : getHistory('aerator', 'drv_rpm', effectiveRange)),
    [raw, effectiveRange, sampleLimit, concreteInterval],
  );
  const mixerRpmSample = useAsync<SeriesPayload>(
    () => (raw || concreteInterval ? getSamples('mixer', 'drv_rpm', effectiveRange, sampleLimit) : getHistory('mixer', 'drv_rpm', effectiveRange)),
    [raw, effectiveRange, sampleLimit, concreteInterval],
  );

  // Strict 1.0 Hz resampling across all series to prevent canvas lag
  const parsedDo = useMemo(
    () => resampleTo1Hz(doSample.data?.data ?? []).map((p) => ({ ms: p.ms, val: p.val })),
    [doSample.data],
  );

  const parsedOdrive = useMemo(
    () =>
      resampleTo1Hz(rpmSample.data?.data ?? [], 650).map((p) => ({
        ms: p.ms,
        val: Math.min(600, Math.max(0, p.val)),
      })),
    [rpmSample.data],
  );

  const parsedMixer = useMemo(
    () => resampleTo1Hz(mixerRpmSample.data?.data ?? []).map((p) => ({ ms: p.ms, val: p.val })),
    [mixerRpmSample.data],
  );

  const milestones = useAsync(
    () => (experimentId ? getMilestones(experimentId) : Promise.resolve(null)),
    [experimentId, milestoneVersion],
  );
  useInterval(milestones.reload, 15_000);

  // Polling with pause capability (1.0 Hz real-time SCADA cadence)
  useInterval(() => {
    if (!isPaused && !concreteInterval) {
      doSample.reload();
      rpmSample.reload();
      mixerRpmSample.reload();
    }
  }, 1_000);

  const points: Point[] = useMemo(() => {
    const byMs = new Map<number, Point>();
    const toRel = (ms: number) => (t0ms === null ? null : Number(((ms - t0ms) / 1000).toFixed(2)));

    for (const p of parsedDo) {
      const entry = byMs.get(p.ms) ?? { ms: p.ms, tRel: toRel(p.ms), do: null, rpm: null, mixerRpm: null };
      entry.do = p.val;
      byMs.set(p.ms, entry);
    }
    for (const p of parsedOdrive) {
      const entry = byMs.get(p.ms) ?? { ms: p.ms, tRel: toRel(p.ms), do: null, rpm: null, mixerRpm: null };
      entry.rpm = p.val;
      byMs.set(p.ms, entry);
    }
    for (const p of parsedMixer) {
      const entry = byMs.get(p.ms) ?? { ms: p.ms, tRel: toRel(p.ms), do: null, rpm: null, mixerRpm: null };
      entry.mixerRpm = p.val;
      byMs.set(p.ms, entry);
    }

    let sorted = Array.from(byMs.values()).sort((a, b) => a.ms - b.ms);

    // Continuous Zero-Order Hold (Sample-and-Hold):
    // Real hardware nodes sample at differing cadences (OPTOD RS485 at 1 Hz, ODrive UART at 5-20 Hz).
    // Without continuous alignment, points where only one node sampled have null for the other,
    // causing Chart.js to show only one variable unless they coincide at the exact same millisecond.
    // By holding the last physical measurement across the timeline, both DO and RPM are continuously
    // present at every point, guaranteeing that the inspection box always displays all variables together.
    let lastDo: number | null = null;
    let lastRpm: number | null = null;
    let lastMixerRpm: number | null = null;

    for (const pt of sorted) {
      if (pt.do !== null) lastDo = pt.do;
      else if (lastDo !== null) pt.do = lastDo;

      if (pt.rpm !== null) lastRpm = pt.rpm;
      else if (lastRpm !== null) pt.rpm = lastRpm;

      if (pt.mixerRpm !== null) lastMixerRpm = pt.mixerRpm;
      else if (lastMixerRpm !== null) pt.mixerRpm = lastMixerRpm;
    }

    let nextDo: number | null = null;
    let nextRpm: number | null = null;
    let nextMixerRpm: number | null = null;

    for (let i = sorted.length - 1; i >= 0; i--) {
      const pt = sorted[i];
      if (pt.do !== null) nextDo = pt.do;
      else if (nextDo !== null) pt.do = nextDo;

      if (pt.rpm !== null) nextRpm = pt.rpm;
      else if (nextRpm !== null) pt.rpm = nextRpm;

      if (pt.mixerRpm !== null) nextMixerRpm = pt.mixerRpm;
      else if (nextMixerRpm !== null) pt.mixerRpm = nextMixerRpm;
    }

    if (emaFilter) {
      const alpha = 0.2;
      let prevEma: number | null = null;
      for (const pt of sorted) {
        if (pt.rpm !== null) {
          if (prevEma === null) {
            prevEma = pt.rpm;
          } else {
            prevEma = alpha * pt.rpm + (1 - alpha) * prevEma;
          }
          pt.rpm = Math.abs(prevEma) < 0.4 ? 0 : Math.round(prevEma * 10) / 10;
        }
      }
    }

    // Performance optimization: decimate if more than 1,200 points to keep 60 FPS
    // Decimation is strictly anchored to wall-clock time buckets to prevent phase/aliasing artifacts as points slide
    if (sorted.length > 1200) {
      const step = Math.ceil(sorted.length / 1200);
      const stepMs = step * 1000;
      let lastBucket = -1;
      sorted = sorted.filter((pt, idx) => {
        if (idx === sorted.length - 1) return true;
        const bucket = Math.floor(pt.ms / stepMs);
        if (bucket !== lastBucket) {
          lastBucket = bucket;
          return true;
        }
        return false;
      });
    }

    return sorted;
  }, [parsedDo, parsedOdrive, parsedMixer, t0ms, emaFilter]);

  const marks: Mark[] = useMemo(() => {
    const list: Mark[] = [];
    for (const m of milestones.data?.milestones ?? []) {
      const ts = new Date(m.ts).getTime();
      const relDetail = m.detail?.t_rel_s;
      list.push({
        ms: ts,
        tRel: typeof relDetail === 'number' ? relDetail : t0ms === null ? null : Number(((ts - t0ms) / 1000).toFixed(2)),
        do: typeof m.detail?.do_mg_l === 'number' ? m.detail.do_mg_l : 0,
        label: m.detail?.label ?? 'Hito',
        detail: m.detail?.detail ?? '',
        kind: m.detail?.kind ?? 'note',
      });
    }
    return list;
  }, [milestones.data, t0ms]);

  const effectiveAxisMode: AxisMode = t0ms !== null ? axisMode : 'clock';
  const xKey = effectiveAxisMode === 'relative' ? 'tRel' : 'ms';
  const relActive = effectiveAxisMode === 'relative';

  // Compute natural data boundaries from the active telemetry series
  const { dataMin, dataMax } = useMemo(() => {
    if (points.length === 0) return { dataMin: undefined, dataMax: undefined };
    const first = xKey === 'tRel' ? (points[0].tRel ?? undefined) : points[0].ms;
    const last = xKey === 'tRel' ? (points[points.length - 1].tRel ?? undefined) : points[points.length - 1].ms;
    return { dataMin: first, dataMax: last };
  }, [points, xKey]);

  // Current effective timeline boundaries (concrete interval or natural data window)
  const currentMin = concreteInterval ? concreteInterval.start : dataMin;
  const currentMax = concreteInterval ? concreteInterval.end : dataMax;

  // Filter marks so only milestones strictly falling within the visible timeline are annotated
  const placedMarks = useMemo(() => {
    return marks.filter((m) => {
      const xVal = xKey === 'tRel' ? m.tRel : m.ms;
      if (xVal === null || !Number.isFinite(xVal)) return false;
      if (currentMin !== undefined && xVal < currentMin) return false;
      if (currentMax !== undefined && xVal > currentMax) return false;
      return true;
    });
  }, [marks, xKey, currentMin, currentMax]);

  const loading = doSample.loading && points.length === 0;
  const error = doSample.error ?? rpmSample.error ?? mixerRpmSample.error;

  // Min and Max domains for interactive slider cursors (bound strictly to visible interval)
  const xDomain = useMemo(() => {
    const minVal = currentMin ?? 0;
    const maxVal = currentMax ?? 100;
    return { min: minVal, max: maxVal };
  }, [currentMin, currentMax]);

  const yDomain = useMemo(() => {
    if (points.length === 0) return { min: 0, max: 8 };
    const doVals = points.map((p) => p.do).filter((v): v is number => v !== null);
    const minD = doVals.length ? Math.min(...doVals) : 0;
    const maxD = doVals.length ? Math.max(...doVals) : 8;
    return {
      min: doAuto ? Math.max(0, Math.floor(minD - 0.5)) : doMin,
      max: doAuto ? Math.ceil(maxD + 0.5) : doMax,
    };
  }, [points, doAuto, doMin, doMax]);

  // Center/initialize cursors visibly within the current domain
  const centerCursors = () => {
    const xSpan = Math.max(1, xDomain.max - xDomain.min);
    setCursor1X(Number((xDomain.min + xSpan * 0.25).toFixed(1)));
    setCursor2X(Number((xDomain.min + xSpan * 0.75).toFixed(1)));

    const ySpan = Math.max(0.1, yDomain.max - yDomain.min);
    setCursor1Y(Number((yDomain.min + ySpan * 0.3).toFixed(2)));
    setCursor2Y(Number((yDomain.min + ySpan * 0.7).toFixed(2)));
  };

  // Keep cursors within visible bounds when user switches range or intervals
  useEffect(() => {
    if (!showDeltaCursors) return;
    if (cursor1X !== null && (cursor1X < xDomain.min || cursor1X > xDomain.max)) {
      centerCursors();
    }
  }, [xDomain.min, xDomain.max, showDeltaCursors]);

  // Quick PNG export
  const handleCapture = () => {
    const chart = chartRef.current;
    if (!chart) return;
    const url = chart.toBase64Image('image/png', 1);
    const link = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    link.download = `aquacontrol_observatorio_${stamp}.png`;
    link.href = url;
    link.click();
  };

  // Save and preserve interactive zoom boundaries, allowing live streaming to continue with the zoomed span
  const handleZoomChange = (context?: any) => {
    const chart = context?.chart || chartRef.current;
    if (chart && chart.scales && chart.scales.x) {
      const xMin = chart.scales.x.min;
      const xMax = chart.scales.x.max;
      if (typeof xMin === 'number' && typeof xMax === 'number' && !isNaN(xMin) && !isNaN(xMax) && xMax > xMin) {
        const span = Math.max(5, xMax - xMin);
        if (!concreteInterval && !isPaused) {
          // Chart is live: keep streaming live with the zoomed window span!
          setZoomSpan(span);
          setFixedZoomDomain(null);
          setIsZoomed(true);
        } else {
          // Paused or viewing concrete historical interval: hold fixed slice
          setFixedZoomDomain({ min: xMin, max: xMax });
          setZoomSpan(null);
          setIsZoomed(true);
        }
      }
    }
  };

  // Reset interactive zoom
  const handleResetZoom = () => {
    const chart = chartRef.current;
    if (chart) {
      if (typeof (chart as any).resetZoom === 'function') {
        (chart as any).resetZoom();
      }
      if (chart.scales && chart.scales.x && chart.scales.x.options) {
        chart.scales.x.options.min = currentMin;
        chart.scales.x.options.max = currentMax;
      }
      chart.update('none');
    }
    setZoomSpan(null);
    setFixedZoomDomain(null);
    setIsZoomed(false);
  };

  // Effective X-axis domain (advances live with zoomed span or uses fixed historical slice)
  const effectiveXMin = useMemo(() => {
    if (!isZoomed) return currentMin;
    if (fixedZoomDomain) return fixedZoomDomain.min;
    if (zoomSpan !== null && currentMax !== undefined) {
      return Number((currentMax - zoomSpan).toFixed(2));
    }
    return currentMin;
  }, [isZoomed, fixedZoomDomain, zoomSpan, currentMax, currentMin]);

  const effectiveXMax = useMemo(() => {
    if (!isZoomed) return currentMax;
    if (fixedZoomDomain) return fixedZoomDomain.max;
    if (zoomSpan !== null && currentMax !== undefined) {
      return currentMax;
    }
    return currentMax;
  }, [isZoomed, fixedZoomDomain, zoomSpan, currentMax]);

  // Sync chart scale options when effectiveXMin / effectiveXMax change (live streaming during zoom)
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !chart.scales || !chart.scales.x) return;
    if (isZoomed && zoomSpan !== null && !isPaused && !concreteInterval) {
      if (chart.scales.x.options) {
        chart.scales.x.options.min = effectiveXMin;
        chart.scales.x.options.max = effectiveXMax;
      }
      chart.update('none');
    }
  }, [effectiveXMin, effectiveXMax, isZoomed, zoomSpan, isPaused, concreteInterval]);

  // Setup annotations: Critical line, Milestones (vertical dotted lines), and Dual Cursors
  const annotations = useMemo(() => {
    const ann: Record<string, any> = {};

    // 1. Critical Hypoxia Line (DO = 3.0 mg/L)
    if (showDo) {
      ann['critical_do'] = {
        type: 'line',
        scaleID: 'do',
        yMin: 3.0,
        yMax: 3.0,
        borderColor: '#ef4444',
        borderWidth: 1.5,
        borderDash: [5, 5],
        adjustScaleRange: false,
        label: {
          display: true,
          content: 'Crítico 3.0 mg/L',
          position: 'start',
          backgroundColor: 'rgba(15, 23, 42, 0.85)',
          color: '#f87171',
          font: { size: 9, family: 'monospace' },
          padding: 2,
        },
      };
    }

    // 2. Milestones / Events (Clean vertical dotted lines only, without text badge box)
    placedMarks.forEach((m, idx) => {
      const xVal = xKey === 'tRel' ? m.tRel : m.ms;
      if (xVal === null || !Number.isFinite(xVal)) return;

      ann[`milestone_${idx}`] = {
        type: 'line',
        scaleID: 'x',
        value: xVal,
        xMin: xVal,
        xMax: xVal,
        borderColor: '#c084fc',
        borderWidth: 1.5,
        borderDash: [4, 4],
        adjustScaleRange: false,
      };
    });

    // 3. Dual-Axis Delta Cursors
    if (showDeltaCursors) {
      // X-Axis Cursors (Vertical)
      if (cursorAxes === 'x' || cursorAxes === 'both') {
        if (cursor1X !== null) {
          ann['cursor_x1'] = {
            type: 'line',
            scaleID: 'x',
            value: cursor1X,
            xMin: cursor1X,
            xMax: cursor1X,
            borderColor: '#00f0ff',
            borderWidth: 2.5,
            borderDash: [6, 3],
            adjustScaleRange: false,
            label: {
              display: true,
              content: `X1: ${relActive ? formatRelativeClock(cursor1X) : (range === '7d' || range === '3650d' ? formatDateTime(cursor1X) : formatTime(cursor1X))}`,
              position: 'start',
              backgroundColor: '#0284c7',
              color: '#ffffff',
              font: { size: 10, weight: 'bold', family: 'monospace' },
              padding: { top: 2, bottom: 2, left: 6, right: 6 },
              borderRadius: 4,
            },
          };
        }
        if (cursor2X !== null) {
          ann['cursor_x2'] = {
            type: 'line',
            scaleID: 'x',
            value: cursor2X,
            xMin: cursor2X,
            xMax: cursor2X,
            borderColor: '#ffd600',
            borderWidth: 2.5,
            borderDash: [6, 3],
            adjustScaleRange: false,
            label: {
              display: true,
              content: `X2: ${relActive ? formatRelativeClock(cursor2X) : (range === '7d' || range === '3650d' ? formatDateTime(cursor2X) : formatTime(cursor2X))}`,
              position: 'start',
              backgroundColor: '#d97706',
              color: '#ffffff',
              font: { size: 10, weight: 'bold', family: 'monospace' },
              padding: { top: 2, bottom: 2, left: 6, right: 6 },
              borderRadius: 4,
              yAdjust: 18,
            },
          };
        }
      }

      // Y-Axis Cursors (Horizontal)
      if (cursorAxes === 'y' || cursorAxes === 'both') {
        if (cursor1Y !== null) {
          ann['cursor_y1'] = {
            type: 'line',
            scaleID: 'do',
            value: cursor1Y,
            yMin: cursor1Y,
            yMax: cursor1Y,
            borderColor: '#00f0ff',
            borderWidth: 2.5,
            borderDash: [6, 3],
            adjustScaleRange: false,
            label: {
              display: true,
              content: `Y1: ${cursor1Y.toFixed(2)} mg/L`,
              position: 'start',
              backgroundColor: '#0284c7',
              color: '#ffffff',
              font: { size: 10, weight: 'bold', family: 'monospace' },
              padding: { top: 2, bottom: 2, left: 6, right: 6 },
              borderRadius: 4,
            },
          };
        }
        if (cursor2Y !== null) {
          ann['cursor_y2'] = {
            type: 'line',
            scaleID: 'do',
            value: cursor2Y,
            yMin: cursor2Y,
            yMax: cursor2Y,
            borderColor: '#ffd600',
            borderWidth: 2.5,
            borderDash: [6, 3],
            adjustScaleRange: false,
            label: {
              display: true,
              content: `Y2: ${cursor2Y.toFixed(2)} mg/L`,
              position: 'end',
              backgroundColor: '#d97706',
              color: '#ffffff',
              font: { size: 10, weight: 'bold', family: 'monospace' },
              padding: { top: 2, bottom: 2, left: 6, right: 6 },
              borderRadius: 4,
            },
          };
        }
      }
    }

    return ann;
  }, [showDo, placedMarks, xKey, showDeltaCursors, cursorAxes, cursor1X, cursor2X, cursor1Y, cursor2Y, relActive]);

  // Chart datasets with linear tension: 0 for 60 FPS performance
  const chartData = useMemo(() => {
    const sets = [];

    if (showDo) {
      sets.push({
        label: 'Oxígeno Disuelto (mg/L)',
        yAxisID: 'do',
        data: points.map((p) => ({
          x: (xKey === 'tRel' ? p.tRel : p.ms) as number,
          y: p.do,
        })),
        borderColor: '#38bdf8',
        backgroundColor: 'rgba(56, 189, 248, 0.08)',
        fill: true,
        borderWidth: 2,
        pointStyle: 'circle' as const,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.2, // Spline cúbico suave para una curva senoidal perfectamente continua y lisa
        normalized: true,
        spanGaps: true,
      });
    }

    if (showRpm) {
      sets.push({
        label: 'ODrive Aireador (RPM)',
        yAxisID: 'rpm',
        data: points.map((p) => ({
          x: (xKey === 'tRel' ? p.tRel : p.ms) as number,
          y: p.rpm,
        })),
        borderColor: '#f59e0b',
        backgroundColor: 'transparent',
        fill: false,
        borderWidth: 2,
        pointStyle: 'triangle' as const,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0, // Linear
        normalized: true,
        spanGaps: true,
      });
    }

    if (showMixerRpm) {
      sets.push({
        label: 'Mezclador T-200 (RPM)',
        yAxisID: 'rpm',
        data: points.map((p) => ({
          x: (xKey === 'tRel' ? p.tRel : p.ms) as number,
          y: p.mixerRpm,
        })),
        borderColor: '#14b8a6',
        backgroundColor: 'transparent',
        fill: false,
        borderWidth: 2,
        pointStyle: 'rectRounded' as const,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0, // Linear
        normalized: true,
        spanGaps: true,
      });
    }

    return { datasets: sets };
  }, [points, showDo, showRpm, showMixerRpm, xKey]);

  // Handle Chart Click for Delta Cursors
  const handleChartClick = (event: any) => {
    if (!showDeltaCursors || !chartRef.current) return;
    const chart = chartRef.current;
    const rect = chart.canvas.getBoundingClientRect();
    const clickX = event.clientX - rect.left;
    const clickY = event.clientY - rect.top;

    const rawValX = chart.scales.x ? chart.scales.x.getValueForPixel(clickX) : undefined;
    const rawValY = chart.scales.do ? chart.scales.do.getValueForPixel(clickY) : undefined;

    if (activeCursorTarget === 'x1' || activeCursorTarget === 'x2') {
      if (rawValX === undefined || isNaN(rawValX)) return;
      let targetX = rawValX;
      if (snapCursors && points.length > 0) {
        let closestPt = points[0];
        let minDiff = Infinity;
        for (const p of points) {
          const px = xKey === 'tRel' ? p.tRel : p.ms;
          if (px !== null && Number.isFinite(px)) {
            const diff = Math.abs(px - rawValX);
            if (diff < minDiff) {
              minDiff = diff;
              closestPt = p;
            }
          }
        }
        const snapped = xKey === 'tRel' ? closestPt.tRel : closestPt.ms;
        if (snapped !== null) targetX = snapped;
      }
      if (activeCursorTarget === 'x1') {
        setCursor1X(targetX);
        setActiveCursorTarget('x2');
      } else {
        setCursor2X(targetX);
        setActiveCursorTarget('x1');
      }
    } else if (activeCursorTarget === 'y1' || activeCursorTarget === 'y2') {
      if (rawValY === undefined || isNaN(rawValY)) return;
      const roundedY = Number(rawValY.toFixed(2));
      if (activeCursorTarget === 'y1') {
        setCursor1Y(roundedY);
        setActiveCursorTarget('y2');
      } else {
        setCursor2Y(roundedY);
        setActiveCursorTarget('y1');
      }
    }
  };

  // Delta calculations
  const deltaMetrics = useMemo(() => {
    if (!showDeltaCursors) return null;

    let dtSeconds: number | null = null;
    let p1: Point | undefined;
    let p2: Point | undefined;

    if (cursor1X !== null && cursor2X !== null) {
      const x1 = Math.min(cursor1X, cursor2X);
      const x2 = Math.max(cursor1X, cursor2X);
      dtSeconds = xKey === 'tRel' ? x2 - x1 : (x2 - x1) / 1000;
      p1 = points.find((p) => Math.abs((xKey === 'tRel' ? (p.tRel ?? 0) : p.ms) - cursor1X) < (xKey === 'tRel' ? 1.5 : 1500));
      p2 = points.find((p) => Math.abs((xKey === 'tRel' ? (p.tRel ?? 0) : p.ms) - cursor2X) < (xKey === 'tRel' ? 1.5 : 1500));
    }

    const deltaY = cursor1Y !== null && cursor2Y !== null ? Math.abs(cursor2Y - cursor1Y) : null;
    const signedDeltaY = cursor1Y !== null && cursor2Y !== null ? cursor2Y - cursor1Y : null;

    let rateDoPerMin: number | null = null;
    if (signedDeltaY !== null && dtSeconds !== null && dtSeconds > 0) {
      rateDoPerMin = (signedDeltaY / dtSeconds) * 60;
    } else if (p1 && p2 && p1.do !== null && p2.do !== null && dtSeconds !== null && dtSeconds > 0) {
      const autoDeltaDo = p2.do - p1.do;
      rateDoPerMin = (autoDeltaDo / dtSeconds) * 60;
    }

    return {
      dtSeconds,
      deltaY,
      rateDoPerMin,
      cursor1Y,
      cursor2Y,
      cursor1X,
      cursor2X,
      p1Do: p1?.do ?? null,
      p2Do: p2?.do ?? null,
      rpm1: p1?.rpm ?? null,
      rpm2: p2?.rpm ?? null,
    };
  }, [showDeltaCursors, cursor1X, cursor2X, cursor1Y, cursor2Y, xKey, points]);

  // Chart options optimized for speed
  const chartOptions = useMemo(() => {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false as const,
      onClick: handleChartClick,
      interaction: {
        mode: 'index' as const,
        axis: 'x' as const,
        intersect: false,
      },
      scales: {
        x: {
          type: 'linear' as const,
          bounds: 'data' as const,
          min: effectiveXMin,
          max: effectiveXMax,
          grid: {
            color: '#1c2436',
          },
          ticks: {
            color: '#94a3b8',
            font: { size: 10, family: 'monospace' },
            callback: (v: number | string) => {
              const num = Number(v);
              if (relActive) return formatRelativeClock(num);
              if (range === '7d' || range === '3650d') return formatDateTime(num);
              return formatTime(num);
            },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 12,
          },
          afterBuildTicks: (scale: any) => {
            const min = scale.min;
            const max = scale.max;
            if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return;
            const span = max - min;

            if (relActive) {
              // Relative timeline (in seconds)
              let stepS: number;
              if (span <= 600) stepS = 60; // 1 min
              else if (span <= 3600) stepS = 300; // 5 min
              else if (span <= 14400) stepS = 1800; // 30 min
              else if (span <= 86400) stepS = 3600 * 3; // 3 hours
              else stepS = 86400; // 1 day
              const firstTick = Math.ceil(min / stepS) * stepS;
              const ticks = [];
              for (let t = firstTick; t <= max; t += stepS) {
                ticks.push({ value: t });
              }
              if (ticks.length >= 3) {
                scale.ticks = ticks;
              }
              return;
            }

            // Real wall-clock mode (in milliseconds)
            let stepMs = 3 * 3600 * 1000;
            if (span <= 3600 * 1000) {
              stepMs = 10 * 60 * 1000; // 10 min
            } else if (span <= 4 * 3600 * 1000) {
              stepMs = 30 * 60 * 1000; // 30 min
            } else if (span <= 26 * 3600 * 1000) {
              stepMs = 3 * 3600 * 1000; // 3 clean hours
            } else {
              stepMs = 24 * 3600 * 1000; // 1 day
            }
            const firstTick = Math.ceil(min / stepMs) * stepMs;
            const ticks = [];
            for (let t = firstTick; t <= max; t += stepMs) {
              ticks.push({ value: t });
            }
            if (ticks.length >= 3) {
              scale.ticks = ticks;
            }
          },
        },
        do: {
          type: 'linear' as const,
          position: 'left' as const,
          display: showDo,
          min: doAuto ? undefined : doMin,
          max: doAuto ? undefined : doMax,
          suggestedMin: 2,
          suggestedMax: 8,
          grid: {
            color: '#1c2436',
          },
          ticks: {
            color: '#38bdf8',
            font: { size: 10, family: 'monospace' },
            callback: (v: number | string) => Number(v).toFixed(1),
          },
          title: {
            display: showDo,
            text: 'OD (mg/L)',
            color: '#38bdf8',
            font: { size: 10, weight: 'bold' as const, family: 'monospace' },
          },
        },
        rpm: {
          type: 'linear' as const,
          position: 'right' as const,
          display: showRpm || showMixerRpm,
          min: rpmAuto ? undefined : rpmMin,
          max: rpmAuto ? undefined : rpmMax,
          suggestedMin: 0,
          suggestedMax: 600,
          grid: {
            drawOnChartArea: false,
          },
          ticks: {
            color: '#f59e0b',
            font: { size: 10, family: 'monospace' },
            callback: (v: number | string) => Math.round(Number(v)).toString(),
          },
          title: {
            display: showRpm || showMixerRpm,
            text: 'RPM',
            color: '#f59e0b',
            font: { size: 10, weight: 'bold' as const, family: 'monospace' },
          },
        },
      },
      plugins: {
        legend: {
          display: false,
        },
        annotation: {
          annotations,
        },
        zoom: {
          pan: {
            enabled: true,
            mode: 'x' as const,
            onPanComplete: handleZoomChange,
          },
          zoom: {
            wheel: {
              enabled: true,
              modifierKey: 'ctrl' as const, // Prevents intercepting page scroll
            },
            drag: {
              enabled: true,
              backgroundColor: 'rgba(56, 189, 248, 0.15)',
              borderColor: '#38bdf8',
              borderWidth: 1,
            },
            pinch: {
              enabled: true,
            },
            mode: 'x' as const,
            onZoomComplete: handleZoomChange,
          },
        },
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
          footerColor: '#94a3b8',
          footerFont: { size: 12, family: 'Inter, system-ui, sans-serif' },
          callbacks: {
            labelColor: (item: any) => {
              const color = (item.dataset.borderColor as string) || '#38bdf8';
              return {
                borderColor: color,
                backgroundColor: color,
                borderWidth: 1.5,
              };
            },
            labelPointStyle: (item: any) => {
              return {
                pointStyle: item.dataset.pointStyle || 'circle',
                rotation: 0,
              };
            },
            title: (items: any[]) => {
              if (!items.length) return '';
              const x = items[0].parsed.x;
              if (relActive) return `⏱️ ${formatRelativeClock(x)}`;
              if (range === '7d' || range === '3650d') return `📅 ${formatDateTime(x, true)}`;
              return `🕒 ${formatTime(x, true)}`;
            },
            label: (item: any) => {
              const rawLabel = item.dataset.label || '';
              const cleanLabel = rawLabel.replace(/\s*\([^)]*\)$/, '');
              const val = item.parsed.y;
              if (val === null || val === undefined) return `${cleanLabel}: Inactivo`;
              if (item.dataset.yAxisID === 'do') return `${cleanLabel}: ${Number(val).toFixed(3)} mg/L`;
              return `${cleanLabel}: ${Math.round(val)} RPM`;
            },
            afterBody: (items: any[]) => {
              if (!items.length) return [];
              const x = items[0].parsed.x;
              const targetMs = relActive && t0ms !== null ? t0ms + x * 1000 : x;
              const mark = placedMarks.find((m) => Math.abs(m.ms - targetMs) < 2000);
              if (mark) {
                return [`📍 Hito: ${mark.label}${mark.detail ? ` (${mark.detail})` : ''}`];
              }
              return [];
            },
          },
        },
      },
    };
  }, [
    concreteInterval,
    showDo,
    showRpm,
    showMixerRpm,
    doAuto,
    doMin,
    doMax,
    rpmAuto,
    rpmMin,
    rpmMax,
    relActive,
    annotations,
    placedMarks,
    t0ms,
    showDeltaCursors,
    snapCursors,
    activeCursorTarget,
  ]);

  const handleRangeClick = (r: string) => {
    setRange(r);
    setCustomRange(null);
    setConcreteInterval(null);
    if (r === '24h' || r === '7d' || r === '3650d') {
      // 24h, 7d and full run historical rollups are chronological: switch to wall-clock time mode
      setAxisMode('clock');
    } else if (r === '10m' || r === '1h') {
      if (t0ms !== null) {
        setAxisMode('relative');
      }
    }
    if (chartRef.current && typeof (chartRef.current as any).resetZoom === 'function') {
      (chartRef.current as any).resetZoom();
    }
    setZoomSpan(null);
    setFixedZoomDomain(null);
    setIsZoomed(false);
  };

  return (
    <>
      <Panel
        title="Dinámica de Oxigenación & Ajuste de Transferencia kLa"
        subtitle="Azul: Concentración DO (mg/L) • Ámbar: ODrive Aireador (RPM) • Verde azulado: Mezclador T-200 (RPM)"
        titleBadge={
          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-scada-well text-slate-300 border border-scada-border font-normal">
            {concreteInterval
              ? 'Intervalo Congelado'
              : range === '24h'
              ? 'Rollup 1-min (Histórico 24h)'
              : range === '7d'
              ? 'Rollup 1-hora (Histórico 7d)'
              : range === '3650d'
              ? 'Ensayo Completo'
              : '1.0 Hz Resample'}
          </span>
        }
        icon={<BarChart3 className="w-4 h-4 text-telemetry-do" />}
        actions={
          <div className="flex flex-wrap items-center justify-end gap-2">
            {/* Range Presets */}
            <div className="flex items-center bg-scada-well rounded-lg p-0.5 border border-scada-border text-xs font-mono">
              <span className="text-[10px] text-slate-500 px-1.5 uppercase font-semibold hidden sm:inline">
                Ventana:
              </span>
              {RANGES.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => handleRangeClick(r)}
                  className={cn(
                    'px-2 py-0.5 rounded transition',
                    range === r && !customRange && !concreteInterval
                      ? 'bg-scada-elevated text-telemetry-do font-bold border border-scada-border'
                      : 'text-slate-400 hover:text-white',
                  )}
                >
                  {r}
                </button>
              ))}
              <button
                type="button"
                onClick={() => handleRangeClick('3650d')}
                className={cn(
                  'px-2 py-0.5 rounded transition',
                  range === '3650d' && !concreteInterval
                    ? 'bg-scada-elevated text-telemetry-do font-bold border border-scada-border'
                    : 'text-slate-400 hover:text-white',
                )}
                title="Todo el ensayo"
              >
                Todo
              </button>
              <button
                type="button"
                onClick={() => setCustomOpen(true)}
                className={cn(
                  'px-1.5 py-0.5 rounded transition',
                  customRange || concreteInterval
                    ? 'bg-scada-elevated text-telemetry-do font-bold border border-scada-border'
                    : 'text-slate-400 hover:text-sky-300',
                )}
                title="Configurar intervalo o ventana concreta"
              >
                ⚙️ {concreteInterval ? concreteInterval.label : customRange ?? 'Intervalo'}
              </button>
            </div>

            {/* Axis Mode Toggle */}
            <div className="flex items-center bg-scada-well rounded-lg p-0.5 border border-scada-border text-xs font-mono">
              <button
                type="button"
                onClick={() => setAxisMode('relative')}
                disabled={t0ms === null}
                className={cn(
                  'px-2 py-1 rounded transition disabled:opacity-40',
                  relActive ? 'bg-scada-elevated text-telemetry-do font-bold border border-scada-border' : 'text-slate-400 hover:text-white',
                )}
                title={t0ms === null ? 'Sin ensayo activo: no hay t₀' : 'Tiempo transcurrido'}
              >
                t - t₀
              </button>
              <button
                type="button"
                onClick={() => setAxisMode('clock')}
                className={cn(
                  'px-2 py-1 rounded transition',
                  !relActive ? 'bg-scada-elevated text-telemetry-do font-bold border border-scada-border' : 'text-slate-400 hover:text-white',
                )}
                title="Hora real (GMT-5)"
              >
                Hora
              </button>
            </div>

            {/* EMA Toggle */}
            <button
              type="button"
              onClick={() => setEmaFilter((v) => !v)}
              className={cn(
                'px-2 py-1 rounded text-xs font-mono transition border',
                emaFilter
                  ? 'bg-scada-elevated text-emerald-400 border-emerald-600/80 font-bold'
                  : 'bg-scada-well text-slate-500 border-scada-border',
              )}
              title="Filtro EMA para atenuar ruido y oscilaciones a 5 Hz"
            >
              EMA: {emaFilter ? 'ON' : 'OFF'}
            </button>

            {/* SCADA Tool: Pause / Resume */}
            <button
              type="button"
              onClick={() => {
                setIsPaused((v) => {
                  const next = !v;
                  if (!next && isZoomed && fixedZoomDomain) {
                    const span = Math.max(5, fixedZoomDomain.max - fixedZoomDomain.min);
                    setZoomSpan(span);
                    setFixedZoomDomain(null);
                  }
                  return next;
                });
              }}
              className={cn(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono font-bold transition border',
                isPaused
                  ? 'bg-amber-950/70 text-amber-300 border-amber-500/80 animate-pulse'
                  : 'bg-scada-well text-slate-400 border-scada-border hover:text-white',
              )}
              title={isPaused ? 'Reanudar telemetría en vivo' : 'Pausar auto-refresco del gráfico para inspeccionar'}
            >
              {isPaused ? <Play className="w-3.5 h-3.5 text-amber-400" /> : <Pause className="w-3.5 h-3.5 text-slate-400" />}
              <span>{isPaused ? 'Pausado' : 'Pausa'}</span>
            </button>

            {/* SCADA Tool: Dual Delta Cursors Toggle */}
            <button
              type="button"
              onClick={() => {
                const next = !showDeltaCursors;
                setShowDeltaCursors(next);
                if (next && (cursor1X === null || cursor1X < xDomain.min || cursor1X > xDomain.max)) {
                  centerCursors();
                }
              }}
              className={cn(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono font-bold transition border',
                showDeltaCursors
                  ? 'bg-scada-elevated text-cyan-300 border-cyan-500/80'
                  : 'bg-scada-well text-slate-400 border-scada-border hover:text-white',
              )}
              title="Activar/Ocultar cursores delta (2 en eje X y 2 en eje Y)"
            >
              <Crosshair className="w-3.5 h-3.5 text-cyan-400" />
              <span>Delta</span>
            </button>

            {/* SCADA Tool: Quick PNG Capture */}
            <button
              type="button"
              onClick={handleCapture}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-mono font-bold bg-scada-well hover:bg-scada-elevated text-slate-300 border border-scada-border transition"
              title="Descargar captura rápida en PNG de la vista actual"
            >
              <Camera className="w-3.5 h-3.5 text-slate-300" />
            </button>

            {/* Y Axes Limits Config */}
            <button
              type="button"
              onClick={() => setAxisConfigOpen((v) => !v)}
              className={cn(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-bold transition border',
                axisConfigOpen || !doAuto || !rpmAuto
                  ? 'bg-scada-elevated text-cyan-400 border-cyan-500/80'
                  : 'bg-scada-well text-slate-400 border-scada-border hover:text-white',
              )}
              title="Configurar escalas mínimas y máximas de los ejes Y"
            >
              <SlidersHorizontal className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Ejes</span>
              {(!doAuto || !rpmAuto) && <span className="w-1.5 h-1.5 rounded-full bg-cyan-400" />}
            </button>

            {/* Milestone Button */}
            <button
              type="button"
              onClick={onAddMilestone}
              disabled={!experimentId}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-scada-elevated hover:bg-slate-700 text-telemetry-do border border-telemetry-do/40 transition disabled:opacity-40"
              title={experimentId ? 'Añade un hito documentado a la gráfica' : 'Inicia un ensayo para documentar hitos'}
            >
              <BookmarkPlus className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Hito</span>
            </button>
          </div>
        }
      >
        {/* Comprehensive Dual-Axis Delta HUD */}
        {showDeltaCursors && deltaMetrics ? (
          <div className="p-3 bg-scada-surface border border-cyan-500/50 rounded-xl space-y-2.5 text-xs font-mono shadow-xl">
            {/* HUD Header with axis selector and metrics */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-scada-border pb-2">
              <div className="flex flex-wrap items-center gap-4">
                <span className="font-bold text-cyan-300 flex items-center gap-1.5">
                  <Crosshair className="w-4 h-4 text-cyan-400" /> Medición Delta (2 por Eje):
                </span>

                {/* X Metric */}
                {(cursorAxes === 'x' || cursorAxes === 'both') && deltaMetrics.dtSeconds !== null ? (
                  <div className="px-2 py-0.5 rounded bg-scada-well border border-scada-border">
                    <span className="text-slate-400">Δt (Eje X): </span>
                    <strong className="text-cyan-300 font-mono">
                      {Math.round(deltaMetrics.dtSeconds)}s ({Math.floor(deltaMetrics.dtSeconds / 60)}m {Math.round(deltaMetrics.dtSeconds % 60)}s)
                    </strong>
                  </div>
                ) : null}

                {/* Y Metric */}
                {(cursorAxes === 'y' || cursorAxes === 'both') && deltaMetrics.deltaY !== null ? (
                  <div className="px-2 py-0.5 rounded bg-scada-well border border-scada-border">
                    <span className="text-slate-400">ΔOD (Eje Y): </span>
                    <strong className="text-amber-300 font-mono">
                      {deltaMetrics.deltaY.toFixed(2)} mg/L
                    </strong>
                  </div>
                ) : null}

                {/* Slope / Transfer Rate */}
                {deltaMetrics.rateDoPerMin !== null ? (
                  <div className="px-2 py-0.5 rounded bg-scada-well border border-scada-border">
                    <span className="text-slate-400">Pendiente: </span>
                    <strong className={deltaMetrics.rateDoPerMin >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                      {deltaMetrics.rateDoPerMin >= 0 ? '+' : ''}{deltaMetrics.rateDoPerMin.toFixed(3)} mg/L/min
                    </strong>
                  </div>
                ) : null}
              </div>

              {/* View options & Actions */}
              <div className="flex items-center gap-2">
                <div className="flex items-center bg-scada-well rounded border border-scada-border text-[11px] p-0.5">
                  <button
                    type="button"
                    onClick={() => setCursorAxes('x')}
                    className={cn('px-2 py-0.5 rounded transition flex items-center gap-1', cursorAxes === 'x' ? 'bg-scada-elevated text-cyan-300 font-bold' : 'text-slate-400')}
                    title="Mostrar solo 2 cursores verticales (Eje X)"
                  >
                    <MoveHorizontal className="w-3 h-3" /> Solo X
                  </button>
                  <button
                    type="button"
                    onClick={() => setCursorAxes('y')}
                    className={cn('px-2 py-0.5 rounded transition flex items-center gap-1', cursorAxes === 'y' ? 'bg-scada-elevated text-amber-300 font-bold' : 'text-slate-400')}
                    title="Mostrar solo 2 cursores horizontales (Eje Y)"
                  >
                    <MoveVertical className="w-3 h-3" /> Solo Y
                  </button>
                  <button
                    type="button"
                    onClick={() => setCursorAxes('both')}
                    className={cn('px-2 py-0.5 rounded transition', cursorAxes === 'both' ? 'bg-scada-elevated text-white font-bold' : 'text-slate-400')}
                    title="Mostrar 2 cursores en X y 2 en Y"
                  >
                    Ambos (X+Y)
                  </button>
                </div>

                <button
                  type="button"
                  onClick={centerCursors}
                  className="px-2 py-1 rounded bg-scada-well hover:bg-slate-700 text-slate-300 border border-scada-border text-[11px] transition"
                  title="Centrar los cursores en la vista actual"
                >
                  Centrar
                </button>

                <button
                  type="button"
                  onClick={() => setShowDeltaCursors(false)}
                  className="text-slate-400 hover:text-white text-xs px-2 py-1 rounded bg-scada-well border border-scada-border"
                  title="Cerrar cursores"
                >
                  ✕
                </button>
              </div>
            </div>

            {/* Active Target for Canvas Click & Interactive Range Sliders */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-[11px]">
              {/* X Cursors Manipulation Controls */}
              {(cursorAxes === 'x' || cursorAxes === 'both') ? (
                <div className="p-2 rounded-lg bg-scada-well border border-scada-border space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-cyan-400 flex items-center gap-1">
                      <MoveHorizontal className="w-3.5 h-3.5" /> Cursores Eje X (Tiempo)
                    </span>
                    <button
                      type="button"
                      onClick={() => setSnapCursors((v) => !v)}
                      className={cn(
                        'px-2 py-0.5 rounded text-[10px] border transition font-mono',
                        snapCursors ? 'bg-cyan-950/60 border-cyan-500 text-cyan-300 font-bold' : 'bg-scada-surface border-scada-border text-slate-400',
                      )}
                      title="Snap magnético a puntos reales de muestra"
                    >
                      Snap: {snapCursors ? 'ON' : 'OFF'}
                    </button>
                  </div>

                  {/* X1 Slider & Controls */}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setActiveCursorTarget('x1')}
                      className={cn(
                        'px-2 py-0.5 rounded text-[10px] font-bold border',
                        activeCursorTarget === 'x1' ? 'bg-cyan-500 text-slate-900 border-cyan-300' : 'bg-scada-surface text-cyan-300 border-cyan-500/40',
                      )}
                      title="Clic para seleccionar X1 como destino al hacer clic en el gráfico"
                    >
                      X1 (Clic)
                    </button>
                    <span className="w-16 font-mono text-[10px] text-cyan-200">
                      {cursor1X !== null ? (relActive ? formatRelativeClock(cursor1X) : (range === '7d' || range === '3650d' ? formatDateTime(cursor1X) : formatTime(cursor1X))) : '--'}
                    </span>
                    <input
                      type="range"
                      min={xDomain.min}
                      max={xDomain.max}
                      step={xKey === 'tRel' ? 1 : 1000}
                      value={cursor1X ?? xDomain.min}
                      onChange={(e) => setCursor1X(Number(e.target.value))}
                      className="flex-1 accent-cyan-400 cursor-pointer h-1.5 bg-scada-surface rounded"
                    />
                    <div className="flex items-center gap-0.5">
                      <button
                        type="button"
                        onClick={() => setCursor1X((v) => (v !== null ? v - (xKey === 'tRel' ? 5 : 5000) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Retroceder 5 segundos"
                      >
                        <ChevronLeft className="w-3 h-3" />
                      </button>
                      <button
                        type="button"
                        onClick={() => setCursor1X((v) => (v !== null ? v + (xKey === 'tRel' ? 5 : 5000) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Avanzar 5 segundos"
                      >
                        <ChevronRight className="w-3 h-3" />
                      </button>
                    </div>
                  </div>

                  {/* X2 Slider & Controls */}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setActiveCursorTarget('x2')}
                      className={cn(
                        'px-2 py-0.5 rounded text-[10px] font-bold border',
                        activeCursorTarget === 'x2' ? 'bg-amber-400 text-slate-900 border-amber-300' : 'bg-scada-surface text-amber-300 border-amber-500/40',
                      )}
                      title="Clic para seleccionar X2 como destino al hacer clic en el gráfico"
                    >
                      X2 (Clic)
                    </button>
                    <span className="w-16 font-mono text-[10px] text-amber-200">
                      {cursor2X !== null ? (relActive ? formatRelativeClock(cursor2X) : (range === '7d' || range === '3650d' ? formatDateTime(cursor2X) : formatTime(cursor2X))) : '--'}
                    </span>
                    <input
                      type="range"
                      min={xDomain.min}
                      max={xDomain.max}
                      step={xKey === 'tRel' ? 1 : 1000}
                      value={cursor2X ?? xDomain.max}
                      onChange={(e) => setCursor2X(Number(e.target.value))}
                      className="flex-1 accent-amber-400 cursor-pointer h-1.5 bg-scada-surface rounded"
                    />
                    <div className="flex items-center gap-0.5">
                      <button
                        type="button"
                        onClick={() => setCursor2X((v) => (v !== null ? v - (xKey === 'tRel' ? 5 : 5000) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Retroceder 5 segundos"
                      >
                        <ChevronLeft className="w-3 h-3" />
                      </button>
                      <button
                        type="button"
                        onClick={() => setCursor2X((v) => (v !== null ? v + (xKey === 'tRel' ? 5 : 5000) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Avanzar 5 segundos"
                      >
                        <ChevronRight className="w-3 h-3" />
                      </button>
                    </div>
                  </div>
                </div>
              ) : null}

              {/* Y Cursors Manipulation Controls */}
              {(cursorAxes === 'y' || cursorAxes === 'both') ? (
                <div className="p-2 rounded-lg bg-scada-well border border-scada-border space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-amber-300 flex items-center gap-1">
                      <MoveVertical className="w-3.5 h-3.5" /> Cursores Eje Y (OD mg/L)
                    </span>
                    <span className="text-[10px] text-slate-400 font-mono">
                      Rango: {yDomain.min} – {yDomain.max} mg/L
                    </span>
                  </div>

                  {/* Y1 Slider & Controls */}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setActiveCursorTarget('y1')}
                      className={cn(
                        'px-2 py-0.5 rounded text-[10px] font-bold border',
                        activeCursorTarget === 'y1' ? 'bg-cyan-500 text-slate-900 border-cyan-300' : 'bg-scada-surface text-cyan-300 border-cyan-500/40',
                      )}
                      title="Clic para seleccionar Y1 como destino al hacer clic en el gráfico"
                    >
                      Y1 (Clic)
                    </button>
                    <span className="w-16 font-mono text-[10px] text-cyan-200">
                      {cursor1Y !== null ? `${cursor1Y.toFixed(2)} mg/L` : '--'}
                    </span>
                    <input
                      type="range"
                      min={yDomain.min}
                      max={yDomain.max}
                      step={0.05}
                      value={cursor1Y ?? yDomain.min}
                      onChange={(e) => setCursor1Y(Number(Number(e.target.value).toFixed(2)))}
                      className="flex-1 accent-cyan-400 cursor-pointer h-1.5 bg-scada-surface rounded"
                    />
                    <div className="flex items-center gap-0.5">
                      <button
                        type="button"
                        onClick={() => setCursor1Y((v) => (v !== null ? Number((v - 0.1).toFixed(2)) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Bajar 0.1 mg/L"
                      >
                        <ChevronLeft className="w-3 h-3" />
                      </button>
                      <button
                        type="button"
                        onClick={() => setCursor1Y((v) => (v !== null ? Number((v + 0.1).toFixed(2)) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Subir 0.1 mg/L"
                      >
                        <ChevronRight className="w-3 h-3" />
                      </button>
                    </div>
                  </div>

                  {/* Y2 Slider & Controls */}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setActiveCursorTarget('y2')}
                      className={cn(
                        'px-2 py-0.5 rounded text-[10px] font-bold border',
                        activeCursorTarget === 'y2' ? 'bg-amber-400 text-slate-900 border-amber-300' : 'bg-scada-surface text-amber-300 border-amber-500/40',
                      )}
                      title="Clic para seleccionar Y2 como destino al hacer clic en el gráfico"
                    >
                      Y2 (Clic)
                    </button>
                    <span className="w-16 font-mono text-[10px] text-amber-200">
                      {cursor2Y !== null ? `${cursor2Y.toFixed(2)} mg/L` : '--'}
                    </span>
                    <input
                      type="range"
                      min={yDomain.min}
                      max={yDomain.max}
                      step={0.05}
                      value={cursor2Y ?? yDomain.max}
                      onChange={(e) => setCursor2Y(Number(Number(e.target.value).toFixed(2)))}
                      className="flex-1 accent-amber-400 cursor-pointer h-1.5 bg-scada-surface rounded"
                    />
                    <div className="flex items-center gap-0.5">
                      <button
                        type="button"
                        onClick={() => setCursor2Y((v) => (v !== null ? Number((v - 0.1).toFixed(2)) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Bajar 0.1 mg/L"
                      >
                        <ChevronLeft className="w-3 h-3" />
                      </button>
                      <button
                        type="button"
                        onClick={() => setCursor2Y((v) => (v !== null ? Number((v + 0.1).toFixed(2)) : null))}
                        className="p-1 rounded bg-scada-surface hover:bg-slate-700 text-slate-300"
                        title="Subir 0.1 mg/L"
                      >
                        <ChevronRight className="w-3 h-3" />
                      </button>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        {/* Concrete Interval Banner */}
        {concreteInterval ? (
          <div className="flex items-center justify-between p-2 rounded-lg bg-scada-well border border-amber-500/50 text-xs font-mono text-amber-300">
            <span className="flex items-center gap-1.5">
              <Calendar className="w-3.5 h-3.5" /> Intervalo congelado: <strong>{concreteInterval.label}</strong> (Telemetría en vivo pausada)
            </span>
            <button
              type="button"
              onClick={() => {
                setConcreteInterval(null);
                setIsPaused(false);
              }}
              className="text-slate-300 hover:text-white bg-scada-elevated px-2 py-0.5 rounded border border-scada-border text-[11px]"
            >
              Restablecer en vivo
            </button>
          </div>
        ) : null}

        {/* Axis configuration drawer */}
        {axisConfigOpen ? (
          <div className="p-3 bg-scada-well rounded-xl border border-cyan-500/40 text-xs font-mono space-y-3">
            <div className="flex items-center justify-between border-b border-scada-border pb-1.5">
              <span className="font-bold text-white flex items-center gap-1.5 text-xs">
                <SlidersHorizontal className="w-3.5 h-3.5 text-cyan-400" /> Configuración de Ejes Y (DO / RPM)
              </span>
              <button
                type="button"
                onClick={() => setAxisConfigOpen(false)}
                className="text-slate-400 hover:text-white text-xs px-2 py-0.5 rounded bg-scada-elevated border border-scada-border"
              >
                ✕ Cerrar
              </button>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-[11px]">
              {/* DO Axis */}
              <div className="p-2.5 rounded-lg bg-scada-surface border border-scada-border space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-bold text-telemetry-do">Eje Y Izquierdo: OD (mg/L)</span>
                  <label className="flex items-center gap-1 text-[10px] text-slate-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={doAuto}
                      onChange={(e) => setDoAuto(e.target.checked)}
                      className="accent-cyan-400"
                    />
                    <span>Auto-escala</span>
                  </label>
                </div>
                <div className="flex items-center gap-2">
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] text-slate-400">Mín:</span>
                    <input
                      type="number"
                      step="0.5"
                      disabled={doAuto}
                      value={doMin}
                      onChange={(e) => setDoMin(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-xs border border-scada-border rounded px-1.5 py-0.5 font-mono disabled:opacity-40"
                    />
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] text-slate-400">Máx:</span>
                    <input
                      type="number"
                      step="0.5"
                      disabled={doAuto}
                      value={doMax}
                      onChange={(e) => setDoMax(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-xs border border-scada-border rounded px-1.5 py-0.5 font-mono disabled:opacity-40"
                    />
                  </div>
                </div>
              </div>

              {/* RPM Axis */}
              <div className="p-2.5 rounded-lg bg-scada-surface border border-scada-border space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-bold text-amber-400">Eje Y Derecho: Motores (RPM)</span>
                  <label className="flex items-center gap-1 text-[10px] text-slate-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={rpmAuto}
                      onChange={(e) => setRpmAuto(e.target.checked)}
                      className="accent-amber-400"
                    />
                    <span>Auto-escala</span>
                  </label>
                </div>
                <div className="flex items-center gap-2">
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] text-slate-400">Mín:</span>
                    <input
                      type="number"
                      step="50"
                      disabled={rpmAuto}
                      value={rpmMin}
                      onChange={(e) => setRpmMin(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-xs border border-scada-border rounded px-1.5 py-0.5 font-mono disabled:opacity-40"
                    />
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] text-slate-400">Máx:</span>
                    <input
                      type="number"
                      step="50"
                      disabled={rpmAuto}
                      value={rpmMax}
                      onChange={(e) => setRpmMax(Number(e.target.value))}
                      className="w-16 bg-scada-well text-slate-100 text-xs border border-scada-border rounded px-1.5 py-0.5 font-mono disabled:opacity-40"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {/* Legend with Interactive Series Toggles */}
        <div className="flex flex-wrap items-center justify-center gap-3 text-[11px] text-slate-300">
          <button
            type="button"
            onClick={() => setShowDo((v) => !v)}
            className={cn(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-medium transition cursor-pointer',
              showDo
                ? 'bg-sky-950/40 border-sky-600/60 text-sky-300 hover:bg-sky-900/40'
                : 'bg-slate-800/40 border-slate-700 text-slate-500 line-through opacity-60 hover:opacity-100',
            )}
          >
            {showDo ? <Eye className="w-3.5 h-3.5 text-telemetry-do" /> : <EyeOff className="w-3.5 h-3.5 text-slate-500" />}
            <span className="w-2.5 h-2.5 rounded-full bg-telemetry-do shadow-[0_0_6px_rgba(56,189,248,0.7)]" />
            <span>Oxígeno Disuelto (mg/L){showDo ? '' : ' (oculto)'}</span>
          </button>

          <button
            type="button"
            onClick={() => setShowRpm((v) => !v)}
            className={cn(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-medium transition cursor-pointer',
              showRpm
                ? 'bg-amber-950/40 border-amber-600/60 text-amber-300 hover:bg-amber-900/40'
                : 'bg-slate-800/40 border-slate-700 text-slate-500 line-through opacity-60 hover:opacity-100',
            )}
          >
            {showRpm ? <Eye className="w-3.5 h-3.5 text-amber-400" /> : <EyeOff className="w-3.5 h-3.5 text-slate-500" />}
            <svg className="w-2.5 h-2.5 text-amber-400 fill-current drop-shadow-[0_0_4px_rgba(245,158,11,0.6)]" viewBox="0 0 24 24"><polygon points="12 3, 22 21, 2 21" /></svg>
            <span>Velocidad ODrive (RPM){showRpm ? '' : ' (oculto)'}</span>
          </button>

          <button
            type="button"
            onClick={() => setShowMixerRpm((v) => !v)}
            className={cn(
              'flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-medium transition cursor-pointer',
              showMixerRpm
                ? 'bg-teal-950/40 border-teal-600/60 text-teal-300 hover:bg-teal-900/40'
                : 'bg-slate-800/40 border-slate-700 text-slate-500 line-through opacity-60 hover:opacity-100',
            )}
          >
            {showMixerRpm ? <Eye className="w-3.5 h-3.5 text-teal-400" /> : <EyeOff className="w-3.5 h-3.5 text-slate-500" />}
            <span className="w-2.5 h-2.5 rounded-[3px] bg-teal-400 shadow-[0_0_6px_rgba(20,184,166,0.6)]" />
            <span>Mezclador T-200 (RPM){showMixerRpm ? '' : ' (oculto)'}</span>
          </button>

          <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-transparent">
            <span className="w-2 h-2 rotate-45 bg-purple-400 shadow-[0_0_4px_rgba(192,132,252,0.6)]" />
            <span className="w-3 h-0.5 border-t border-dashed border-purple-300" />
            Hitos del Ensayo
          </span>

          {isZoomed ? (
            <button
              type="button"
              onClick={handleResetZoom}
              className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-sky-950 border border-sky-500/80 text-sky-300 hover:bg-sky-900 text-xs font-mono font-bold transition shadow"
            >
              <RotateCcw className="w-3 h-3 text-sky-400" /> Restablecer Zoom
            </button>
          ) : null}
        </div>

        {loading ? <Loading label="Leyendo muestras del archivo" /> : null}
        {error ? <ErrorState error={error} onRetry={doSample.reload} compact /> : null}

        {!error && !loading && points.length === 0 ? (
          <EmptyState
            message="Sin muestras en este rango."
            hint="Si un ensayo está en marcha, prueba un rango mayor (1h/24h): el nodo sella cada muestra con su reloj (RTC), y un desfase frente al gateway deja los puntos fuera de la ventana corta."
          />
        ) : null}

        {points.length > 0 ? (
          <div className="h-[440px] md:h-[480px] 2xl:h-[560px] relative w-full bg-scada-well rounded-xl p-2 border border-scada-border">
            <Line ref={chartRef} data={chartData} options={chartOptions as any} />
          </div>
        ) : null}
      </Panel>

      {/* Modal: Custom window or concrete time interval */}
      {customOpen ? (
        <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 font-mono">
          <div className="bg-scada-surface border border-scada-border rounded-2xl max-w-md w-full p-5 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between border-b border-scada-border pb-2.5">
              <span className="text-white font-bold text-xs">Horizonte Temporal del Observatorio</span>
              <button type="button" onClick={() => setCustomOpen(false)} className="text-slate-400 hover:text-white text-xs">
                ✕
              </button>
            </div>
            <CustomWindowForm
              t0ms={t0ms}
              relActive={relActive}
              onApplyRelative={(spec) => {
                setConcreteInterval(null);
                setCustomRange(spec.label);
                setRange(spec.query);
                setZoomSpan(null);
                setFixedZoomDomain(null);
                setIsZoomed(false);
                setCustomOpen(false);
              }}
              onApplyConcrete={(spec) => {
                setConcreteInterval(spec);
                setIsPaused(true);
                setZoomSpan(null);
                setFixedZoomDomain(null);
                setIsZoomed(false);
                setCustomOpen(false);
              }}
              onCancel={() => setCustomOpen(false)}
            />
          </div>
        </div>
      ) : null}
    </>
  );
}

function CustomWindowForm({
  t0ms,
  relActive,
  onApplyRelative,
  onApplyConcrete,
  onCancel,
}: {
  t0ms: number | null;
  relActive: boolean;
  onApplyRelative: (spec: { query: string; label: string }) => void;
  onApplyConcrete: (spec: { start: number; end: number; label: string }) => void;
  onCancel: () => void;
}) {
  const [tab, setTab] = useState<'relative' | 'interval'>('relative');
  const [value, setValue] = useState('15');
  const [unit, setUnit] = useState<'s' | 'm' | 'h' | 'd'>('m');

  const [startMin, setStartMin] = useState('0');
  const [endMin, setEndMin] = useState('30');

  const applyRelative = () => {
    const amount = Math.max(1, Math.round(Number(value) || 1));
    onApplyRelative({ query: `${amount}${unit}`, label: `${amount}${unit}` });
  };

  const applyConcrete = () => {
    const sMin = Math.max(0, Number(startMin) || 0);
    const eMin = Math.max(sMin + 1, Number(endMin) || sMin + 10);

    if (relActive && t0ms !== null) {
      const startSec = sMin * 60;
      const endSec = eMin * 60;
      onApplyConcrete({
        start: startSec,
        end: endSec,
        label: `+${Math.floor(sMin)}m a +${Math.floor(eMin)}m`,
      });
    } else {
      const now = Date.now();
      const startMs = now - eMin * 60 * 1000;
      const endMs = now - sMin * 60 * 1000;
      onApplyConcrete({
        start: startMs,
        end: endMs,
        label: `-${Math.floor(eMin)}m a -${Math.floor(sMin)}m`,
      });
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex border-b border-scada-border pb-1 gap-2 text-xs">
        <button
          type="button"
          onClick={() => setTab('relative')}
          className={cn('px-2.5 py-1 rounded transition', tab === 'relative' ? 'bg-scada-elevated text-cyan-300 font-bold' : 'text-slate-400')}
        >
          Ventana móvil (últimos X)
        </button>
        <button
          type="button"
          onClick={() => setTab('interval')}
          className={cn('px-2.5 py-1 rounded transition', tab === 'interval' ? 'bg-scada-elevated text-cyan-300 font-bold' : 'text-slate-400')}
        >
          Intervalo fijo (Desde / Hasta)
        </button>
      </div>

      {tab === 'relative' ? (
        <div className="space-y-3">
          <p className="text-[11px] text-slate-400">
            Muestra la telemetría acumulada en los últimos minutos u horas desde el instante actual.
          </p>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className="w-24 bg-scada-well text-slate-100 text-xs border border-scada-border rounded p-2 text-right focus:outline-none focus:border-telemetry-do font-mono"
            />
            <select
              value={unit}
              onChange={(e) => setUnit(e.target.value as 's' | 'm' | 'h' | 'd')}
              className="bg-scada-well text-slate-100 text-xs border border-scada-border rounded p-2 focus:outline-none focus:border-telemetry-do font-mono"
            >
              <option value="s">Segundos (s)</option>
              <option value="m">Minutos (min)</option>
              <option value="h">Horas (h)</option>
              <option value="d">Días (días)</option>
            </select>
          </div>
          <div className="flex justify-end gap-2 pt-2 border-t border-scada-border">
            <button type="button" onClick={onCancel} className="px-3 py-1.5 rounded bg-scada-elevated text-slate-300 text-xs">
              Cancelar
            </button>
            <button
              type="button"
              onClick={applyRelative}
              className="px-4 py-1.5 rounded bg-telemetry-do hover:bg-sky-400 text-slate-900 font-bold text-xs transition"
            >
              Aplicar Ventana
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-[11px] text-slate-400">
            Fija el eje horizontal en un tramo concreto en minutos {relActive ? 'desde t₀' : 'atrás en el tiempo'}.
          </p>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <span className="text-[10px] text-slate-400 block mb-1">Minuto inicial:</span>
              <input
                type="number"
                min={0}
                value={startMin}
                onChange={(e) => setStartMin(e.target.value)}
                className="w-full bg-scada-well text-slate-100 border border-scada-border rounded p-2 font-mono"
              />
            </div>
            <div>
              <span className="text-[10px] text-slate-400 block mb-1">Minuto final:</span>
              <input
                type="number"
                min={1}
                value={endMin}
                onChange={(e) => setEndMin(e.target.value)}
                className="w-full bg-scada-well text-slate-100 border border-scada-border rounded p-2 font-mono"
              />
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-2 border-t border-scada-border">
            <button type="button" onClick={onCancel} className="px-3 py-1.5 rounded bg-scada-elevated text-slate-300 text-xs">
              Cancelar
            </button>
            <button
              type="button"
              onClick={applyConcrete}
              className="px-4 py-1.5 rounded bg-cyan-500 hover:bg-cyan-400 text-slate-900 font-bold text-xs transition"
            >
              Fijar Intervalo (Pausar)
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
