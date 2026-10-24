/**
 * Small design system.
 *
 * Every tone is a full literal class string rather than a composed one, because Tailwind
 * scans source text for class names — `` `border-${tone}-500` `` would produce a border
 * that is simply missing at runtime, with no error anywhere.
 */

import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import type { ReactNode } from 'react';
import { AlertOctagon, AlertTriangle, CheckCircle2, Info, Loader2, RefreshCw } from 'lucide-react';
import { describeError, isUnavailable, isUnreachable } from '@/lib/api';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export type Tone = 'ok' | 'warn' | 'bad' | 'idle' | 'info';

export interface ToneStyle {
  text: string;
  bg: string;
  border: string;
  ring: string;
  dot: string;
}

export const TONE: Record<Tone, ToneStyle> = {
  ok: {
    text: 'text-emerald-400',
    bg: 'bg-emerald-950/70',
    border: 'border-emerald-800',
    ring: 'shadow-[0_0_12px_rgba(16,185,129,0.25)]',
    dot: 'bg-emerald-400',
  },
  warn: {
    text: 'text-amber-400',
    bg: 'bg-amber-950/70',
    border: 'border-amber-800',
    ring: 'shadow-[0_0_12px_rgba(245,158,11,0.25)]',
    dot: 'bg-amber-400',
  },
  bad: {
    text: 'text-rose-400',
    bg: 'bg-rose-950/80',
    border: 'border-rose-800',
    ring: 'shadow-[0_0_12px_rgba(244,63,94,0.25)]',
    dot: 'bg-rose-400',
  },
  idle: {
    text: 'text-slate-400',
    bg: 'bg-scada-elevated',
    border: 'border-scada-border',
    ring: '',
    dot: 'bg-slate-500',
  },
  info: {
    text: 'text-sky-400',
    bg: 'bg-sky-950/70',
    border: 'border-sky-800',
    ring: 'shadow-[0_0_12px_rgba(56,189,248,0.25)]',
    dot: 'bg-sky-400',
  },
};

export function toneFromSeverity(severity: string): Tone {
  if (severity === 'critical') return 'bad';
  if (severity === 'warning') return 'warn';
  return 'idle';
}

export function toneFromLevel(level: string | undefined): Tone {
  if (level === 'CRITICAL') return 'bad';
  if (level === 'WARNING') return 'warn';
  if (level === 'OK') return 'ok';
  return 'idle';
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

interface PanelProps {
  title: string;
  subtitle?: string;
  /** A small badge rendered immediately after the title, as the mockup does for its sensor
   *  rate. Kept separate from `actions` so it hugs the title rather than the far edge. */
  titleBadge?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** `glow` is reserved for the primary reading so there is exactly one focal point. */
  variant?: 'default' | 'glow';
}

export function Panel({
  title,
  subtitle,
  titleBadge,
  icon,
  actions,
  children,
  className,
  variant = 'default',
}: PanelProps) {
  return (
    <section
      className={cn(
        // The mockup's structural card: neutral obsidian surface with a single subtle border.
        // No glassmorphism and no cyan tint — those were the previous dashboard's language and
        // they read as a different palette next to the flat mockup.
        'rounded-2xl p-4 flex flex-col gap-3 border shadow-sm',
        variant === 'glow'
          ? 'bg-scada-surface border-telemetry-do/40'
          : 'bg-scada-surface border-scada-border',
        className,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-scada-border pb-3">
        <div className="flex items-center gap-2.5 min-w-0">
          {icon ? (
            // Accent tile, as the mockup: elevated surface, subtle border, coloured glyph.
            <div className="p-2 rounded-lg bg-scada-elevated border border-scada-border shrink-0">
              {icon}
            </div>
          ) : null}
          <div className="min-w-0">
            <h3 className="text-sm font-bold text-white flex items-center gap-2 flex-wrap">
              {title}
              {titleBadge}
            </h3>
            {subtitle ? <p className="text-[11px] text-slate-400 mt-0.5">{subtitle}</p> : null}
          </div>
        </div>
        {actions ? (
          <div className="flex items-center gap-2 flex-wrap justify-end min-w-0">{actions}</div>
        ) : null}
      </header>
      {children}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  icon?: ReactNode;
  pulse?: boolean;
  className?: string;
}

export function Badge({ tone = 'idle', children, icon, pulse, className }: BadgeProps) {
  const style = TONE[tone];
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold border whitespace-nowrap',
        style.text,
        style.bg,
        style.border,
        pulse && 'animate-pulse',
        className,
      )}
    >
      {icon}
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

export function Loading({ label = 'Cargando' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-sm text-slate-400">
      <Loader2 className="w-4 h-4 animate-spin" />
      <span>{label}…</span>
    </div>
  );
}

/** Shown when a request failed. The message distinguishes the three cases that actually
 *  matter, because "database offline" and "no gateway" need different actions. */
export function ErrorState({
  error,
  onRetry,
  compact,
}: {
  error: unknown;
  onRetry?: () => void;
  compact?: boolean;
}) {
  const unavailable = isUnavailable(error);
  const unreachable = isUnreachable(error);
  const tone: Tone = unreachable ? 'bad' : unavailable ? 'warn' : 'bad';
  const headline = unreachable
    ? 'Gateway inalcanzable'
    : unavailable
      ? 'Gateway degradado'
      : 'Solicitud fallida';
  const hint = unreachable
    ? 'Caddy o aqua-gateway.service no responde en este host.'
    : unavailable
      ? 'El gateway está en marcha pero su base de datos está sin conexión, así que no puede responder. La telemetría se sigue archivando.'
      : 'Consulta el log del gateway para ver el error subyacente.';

  return (
    <div
      className={cn(
        'flex flex-col gap-2 rounded-xl border px-4',
        TONE[tone].bg,
        TONE[tone].border,
        compact ? 'py-3' : 'py-5',
      )}
    >
      <div className={cn('flex items-center gap-2 text-sm font-semibold', TONE[tone].text)}>
        <AlertOctagon className="w-4 h-4" />
        <span>{headline}</span>
      </div>
      <p className="text-xs text-slate-300">{describeError(error)}</p>
      {!compact ? <p className="text-[11px] text-slate-400">{hint}</p> : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="self-start mt-1 inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800/80 hover:bg-slate-700 text-slate-200 transition-colors"
        >
          <RefreshCw className="w-3 h-3" />
          Reintentar
        </button>
      ) : null}
    </div>
  );
}

/** Deliberately distinct from `ErrorState`: "nothing happened" and "we cannot tell you"
 *  must never look the same. */
export function EmptyState({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 py-8 text-center">
      <Info className="w-4 h-4 text-slate-500" />
      <p className="text-sm text-slate-400">{message}</p>
      {hint ? <p className="text-[11px] text-slate-500 max-w-md">{hint}</p> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sparkline — inline SVG rather than a chart library. 120 points, no axes, no tooltip:
// a full recharts instance per metric card would cost more than the whole panel.
// ---------------------------------------------------------------------------

export function Sparkline({
  values,
  tone = 'info',
  className,
  height = 28,
}: {
  values: number[] | undefined;
  tone?: Tone;
  className?: string;
  height?: number;
}) {
  const stroke: Record<Tone, string> = {
    ok: '#34d399',
    warn: '#fbbf24',
    bad: '#fb7185',
    idle: '#64748b',
    info: '#22d3ee',
  };

  if (!values || values.length < 2) {
    return <div className={cn('w-full', className)} style={{ height }} aria-hidden />;
  }

  const width = 100;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const step = width / (values.length - 1);

  // Inset by 2 units top and bottom so a flat series does not sit on the edge.
  const points = values
    .map((value, index) => {
      const x = index * step;
      const y = height - 2 - ((value - lo) / span) * (height - 4);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={cn('w-full', className)}
      style={{ height }}
      aria-hidden
    >
      <polyline
        points={points}
        fill="none"
        stroke={stroke[tone]}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Metric — one label/value/unit block, used by every card.
// ---------------------------------------------------------------------------

export function Metric({
  label,
  value,
  unit,
  hint,
  tone = 'idle',
  large,
}: {
  label: string;
  value: string;
  unit?: string | null;
  hint?: ReactNode;
  tone?: Tone;
  large?: boolean;
}) {
  return (
    <div className="flex flex-col justify-between">
      <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">{label}</span>
      <div className="mt-1">
        <div className="flex items-baseline gap-1.5">
          <span
            className={cn(
              'font-bold text-white tracking-tight',
              large ? 'text-4xl lg:text-5xl font-extrabold' : 'text-2xl lg:text-3xl',
            )}
          >
            {value}
          </span>
          {unit ? <span className={cn('text-sm font-medium', TONE[tone].text)}>{unit}</span> : null}
        </div>
        {hint ? <div className="text-[11px] text-slate-500 mt-0.5">{hint}</div> : null}
      </div>
    </div>
  );
}

/** Inline icon for a tone, so status is not conveyed by colour alone. */
export function ToneIcon({ tone, className }: { tone: Tone; className?: string }) {
  if (tone === 'bad') return <AlertOctagon className={className} />;
  if (tone === 'warn') return <AlertTriangle className={className} />;
  if (tone === 'ok') return <CheckCircle2 className={className} />;
  return <Info className={className} />;
}
