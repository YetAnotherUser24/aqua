'use client';

/**
 * The unified event stream.
 *
 * One table for all producers — firmware, gateway and UI — because that was the whole point
 * of the redesign: the previous system had motor events here, pump events there and alerts
 * somewhere else, and reconstructing "what happened at 14:32" meant reading three schemas.
 *
 * Two columns carry information the old dashboard did not have:
 *
 *   * **source** — who observed it. A `do_low_critical` derived by the gateway from a
 *     threshold is a different claim from the probe reporting a fault, and merging them
 *     without provenance makes the two indistinguishable.
 *   * **scope** — whether the event is about the pond, a node, an experiment or an operator
 *     action. It is the difference between "the DO dropped" and "someone pressurised the
 *     sensor".
 */

import { useMemo, useState } from 'react';
import { History, RefreshCw } from 'lucide-react';
import { getEventTypes, getEvents, type EventQuery } from '@/lib/api';
import { formatDateTime } from '@/lib/dateUtils';
import { useAsync, useInterval } from '@/hooks/useAsync';
import type { AquaEvent, Severity } from '@/lib/types';
import { Badge, EmptyState, ErrorState, Loading, Panel, TONE, cn, toneFromSeverity } from './ui';

const SEVERITIES: Severity[] = ['critical', 'warning', 'info'];

/** Severity tokens are the API's vocabulary; these are what an operator reads. */
const SEVERITY_LABEL: Record<Severity, string> = {
  critical: 'crítico',
  warning: 'advertencia',
  info: 'info',
};

/** Severity filtering is the common case, so it gets a row of buttons rather than a select. */
const SEVERITY_ORDER: Record<Severity, number> = { critical: 0, warning: 1, info: 2 };

function detailSummary(detail: Record<string, unknown>): string {
  const entries = Object.entries(detail ?? {});
  if (entries.length === 0) return '—';
  return entries
    .slice(0, 4)
    .map(([key, value]) => {
      const text =
        value === null || value === undefined
          ? '—'
          : typeof value === 'object'
            ? Array.isArray(value)
              ? `[${value.length}]`
              : '{…}'
            : String(value);
      return `${key}=${text}`;
    })
    .join('  ');
}

function EventRow({ event }: { event: AquaEvent }) {
  const tone = toneFromSeverity(event.severity);
  const summary = detailSummary(event.detail);

  return (
    <tr className="hover:bg-scada-elevated/40 transition-colors align-top">
      <td className="py-2 px-3 whitespace-nowrap text-slate-300 font-mono text-[11px]">
        {formatDateTime(event.ts, true)}
      </td>
      <td className="py-2 px-3">
        <Badge tone={tone}>{SEVERITY_LABEL[event.severity] ?? event.severity}</Badge>
      </td>
      <td className="py-2 px-3">
        <span className="font-medium text-slate-200">{event.event_type}</span>
        {event.action ? (
          <span className="block text-[10px] text-slate-500">{event.action}</span>
        ) : null}
      </td>
      <td className="py-2 px-3 whitespace-nowrap text-slate-400 text-[11px]">
        {event.scope}
        <span className="block text-[10px] text-slate-500">{event.source}</span>
      </td>
      <td className="py-2 px-3 whitespace-nowrap text-slate-400 text-[11px]">
        {event.node_id ? (
          <span className="font-mono" title={event.node_id}>
            {event.node_id.slice(0, 8)}
          </span>
        ) : (
          '—'
        )}
      </td>
      <td className="py-2 px-3 text-slate-400 max-w-md">
        <span className="font-mono text-[10px] break-all" title={JSON.stringify(event.detail)}>
          {summary}
        </span>
      </td>
    </tr>
  );
}

export function EventsTable({ experimentId }: { experimentId: string | null }) {
  const [severity, setSeverity] = useState<Severity | 'all'>('all');
  const [scope, setScope] = useState<string>('all');
  const [eventType, setEventType] = useState<string>('all');
  const [limit, setLimit] = useState(100);

  const catalogue = useAsync(() => getEventTypes(), []);

  const query: EventQuery = useMemo(
    () => ({
      severity: severity === 'all' ? undefined : severity,
      scope: scope === 'all' ? undefined : scope,
      event_type: eventType === 'all' ? undefined : eventType,
      experiment_id: experimentId ?? undefined,
      limit,
    }),
    [severity, scope, eventType, experimentId, limit],
  );

  const { data, error, loading, reload } = useAsync(() => getEvents(query), [
    query.severity,
    query.scope,
    query.event_type,
    query.experiment_id,
    query.limit,
  ]);

  // Events are written by the gateway as they happen, so a slow poll is enough. This replaces
  // the old 4-15 s poll of a dedicated endpoint; the *live* values come over SSE instead.
  useInterval(reload, 15_000);

  const events = data?.events ?? [];
  const counts = useMemo(() => {
    const base: Record<Severity, number> = { critical: 0, warning: 0, info: 0 };
    for (const event of events) base[event.severity] = (base[event.severity] ?? 0) + 1;
    return base;
  }, [events]);

  const sortedTypes = useMemo(
    () => (catalogue.data?.types ?? []).slice().sort((a, b) => a.localeCompare(b)),
    [catalogue.data],
  );
  const scopes = useMemo(() => catalogue.data?.scopes ?? [], [catalogue.data]);

  const worst: Severity | null = events.length
    ? ([...events].sort(
        (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
      )[0].severity ?? null)
    : null;

  return (
    <Panel
      title="Flujo de eventos"
      subtitle="Acciones de firmware, gateway y operador en una sola tabla"
      icon={<History className="w-4 h-4 text-sky-400" />}
      actions={
        <button
          type="button"
          onClick={reload}
          className="inline-flex items-center gap-1 text-[11px] font-semibold text-slate-400 hover:text-sky-300 transition-colors"
        >
          <RefreshCw className={cn('w-3 h-3', loading && 'animate-spin')} />
          actualizar
        </button>
      }
    >
      <div className="flex flex-wrap items-center gap-2 -mt-1">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setSeverity('all')}
            className={cn(
              'px-2 py-0.5 rounded-full text-[11px] font-semibold border transition-colors',
              severity === 'all'
                ? 'bg-scada-elevated text-slate-200 border-scada-borderHover'
                : 'bg-scada-well text-slate-500 border-scada-border hover:text-slate-300',
            )}
          >
            todos
          </button>
          {SEVERITIES.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setSeverity(s)}
              className={cn(
                'px-2 py-0.5 rounded-full text-[11px] font-semibold border transition-colors',
                severity === s
                  ? cn(TONE[toneFromSeverity(s)].bg, TONE[toneFromSeverity(s)].text, TONE[toneFromSeverity(s)].border)
                  : 'bg-scada-well text-slate-500 border-scada-border hover:text-slate-300',
              )}
            >
              {SEVERITY_LABEL[s]}
              {severity === 'all' && counts[s] > 0 ? ` ${counts[s]}` : ''}
            </button>
          ))}
        </div>

        <select
          value={scope}
          onChange={(e) => setScope(e.target.value)}
          className="rounded-lg border border-scada-border bg-scada-well px-2 py-1 text-[11px] text-slate-200 focus:border-telemetry-do focus:outline-none"
        >
          <option value="any">cualquier alcance</option>
          {scopes.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        <select
          value={eventType}
          onChange={(e) => setEventType(e.target.value)}
          className="rounded-lg border border-scada-border bg-scada-well px-2 py-1 text-[11px] text-slate-200 focus:border-telemetry-do focus:outline-none max-w-[14rem]"
        >
          <option value="any">cualquier tipo</option>
          {sortedTypes.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>

        <select
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          className="rounded-lg border border-scada-border bg-scada-well px-2 py-1 text-[11px] text-slate-200 focus:border-telemetry-do focus:outline-none"
        >
          {[50, 100, 250, 500].map((n) => (
            <option key={n} value={n}>
              últimos {n}
            </option>
          ))}
        </select>

        {experimentId ? (
          <Badge tone="info">acotado al ensayo</Badge>
        ) : null}
        {worst ? <Badge tone={toneFromSeverity(worst)}>{events.length} mostrados</Badge> : null}
      </div>

      {catalogue.error ? (
        <p className={cn('text-[11px]', TONE.warn.text)}>
          Catálogo de tipos de evento no disponible — el filtrado por tipo es limitado.
        </p>
      ) : null}

      <div className="overflow-x-auto max-h-[26rem]">
        {error ? (
          <ErrorState error={error} onRetry={reload} compact />
        ) : loading && events.length === 0 ? (
          <Loading label="Leyendo eventos" />
        ) : events.length === 0 ? (
          <EmptyState
            message="Ningún evento coincide con estos filtros."
            hint="El gateway escribe los eventos a medida que ocurren. Una tabla vacía aquí significa que no se registró ninguno, no que no se hayan consultado."
          />
        ) : (
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="bg-scada-well uppercase text-[10px] text-slate-500 font-semibold sticky top-0 z-10">
              <tr>
                <th className="py-2 px-3">Cuándo (GMT-5)</th>
                <th className="py-2 px-3">Severidad</th>
                <th className="py-2 px-3">Tipo</th>
                <th className="py-2 px-3">Alcance / origen</th>
                <th className="py-2 px-3">Nodo</th>
                <th className="py-2 px-3">Detalle</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-scada-border/60">
              {events.map((event) => (
                <EventRow key={event.id} event={event} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Panel>
  );
}
