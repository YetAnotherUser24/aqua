'use client';

/**
 * Bitácora de hitos — the operator's documented annotations on the run, as the mockup shows it.
 *
 * Distinct from the raw event stream: a milestone is something the *operator* wrote down to
 * explain the curve ("sulfite dose at +00:45"), whereas the event table is what the gateway
 * observed. Keeping them in separate panels is the point — one is a lab notebook, the other a
 * machine log.
 */

import { History } from 'lucide-react';
import { getMilestones } from '@/lib/api';
import { useAsync } from '@/hooks/useAsync';
import { formatRelativeClock } from '@/lib/format';
import { formatTime } from '@/lib/dateUtils';
import { EmptyState, Loading, Panel, cn } from './ui';
import type { Milestone, MilestoneKind } from '@/lib/types';

/** Badge tone per milestone kind, matching the mockup: sulfite violet, step amber, rest sky. */
const KIND_STYLE: Record<MilestoneKind, string> = {
  sulfite: 'bg-purple-950/80 text-purple-300 border-purple-800',
  step: 'bg-amber-950/80 text-amber-300 border-amber-800',
  winkler: 'bg-sky-950/80 text-sky-300 border-sky-800',
  note: 'bg-sky-950/80 text-sky-300 border-sky-800',
};

export function BitacoraPanel({
  experimentId,
  version = 0,
}: {
  experimentId: string | null;
  /** Bumped by the page when a milestone is posted, so the list re-fetches immediately. */
  version?: number;
}) {
  const { data, error, loading } = useAsync(
    () => (experimentId ? getMilestones(experimentId) : Promise.resolve(null)),
    [experimentId, version],
  );

  const milestones: Milestone[] = data?.milestones ?? [];

  return (
    <Panel
      title="Bitácora de Hitos en el Ensayo"
      subtitle="Eventos documentados en la gráfica"
      icon={<History className="w-4 h-4 text-telemetry-do" />}
    >
      {error ? (
        <EmptyState message="No se pudo leer la bitácora." />
      ) : loading && milestones.length === 0 ? (
        <Loading label="Leyendo hitos" />
      ) : milestones.length === 0 ? (
        <EmptyState
          message="Sin hitos documentados."
          hint="Añade un hito desde la gráfica para explicar un punto de la curva — una dosis, un escalón, una muestra Winkler."
        />
      ) : (
        <div className="space-y-2 max-h-40 overflow-y-auto pr-1">
          {milestones
            .slice()
            .sort((a, b) => (b.detail?.t_rel_s ?? 0) - (a.detail?.t_rel_s ?? 0))
            .map((m) => {
              const kind = (m.detail?.kind ?? 'note') as MilestoneKind;
              const rel = m.detail?.t_rel_s ?? null;
              return (
                <div
                  key={m.id}
                  className="p-2 rounded bg-scada-well border border-scada-border flex items-center justify-between gap-2 text-xs font-mono"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span
                      className={cn(
                        'px-1.5 py-0.5 rounded border text-[10px] shrink-0',
                        KIND_STYLE[kind],
                      )}
                    >
                      {rel !== null ? `t=${formatRelativeClock(rel).replace('+', '')}` : 't=—'}
                    </span>
                    <span className="text-slate-300 truncate">
                      {m.detail?.label ?? 'Hito'}
                      {m.detail?.detail ? ` — ${m.detail.detail}` : ''}
                    </span>
                  </div>
                  <span className="text-[10px] text-slate-500 shrink-0">RTC {formatTime(m.ts, true)}</span>
                </div>
              );
            })}
        </div>
      )}
    </Panel>
  );
}
