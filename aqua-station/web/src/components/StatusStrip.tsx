'use client';

/**
 * Per-node presence and stream health.
 *
 * This panel exists because the previous dashboard had no way to answer "is the number I am
 * looking at current?" — it showed values with no indication of when they arrived. Two
 * things make that answerable now:
 *
 *   * MQTT Last Will & Testament, so a disconnect is reported by the broker rather than
 *     inferred later from a stale timestamp;
 *   * the envelope's `t_src`, so a node whose clock was reconstructed from an uptime
 *     counter is visibly distinguished from one that knows the time.
 *
 * The schema-version mismatch check is a migration aid: a node still running v1 firmware
 * publishes field names the registry does not define, and that is far easier to spot here
 * than by noticing a column of dashes.
 */

import { Activity, Radio, Signal, Wifi, WifiOff } from 'lucide-react';
import type { LiveSnapshot, Registry, TimeSource } from '@/lib/types';
import { TIME_SOURCE_TRUST } from '@/lib/types';
import { nodeStale, roleDisplays, formatHz } from '@/lib/registry';
import { describeClockOffset } from '@/lib/dateUtils';
import { describeRssi, formatAge, formatDuration } from '@/lib/format';
import { Badge, Panel, TONE, type Tone, cn } from './ui';
import type { StreamState } from '@/hooks/useLive';

const STREAM_PRESENTATION: Record<StreamState, { tone: Tone; label: string; hint: string }> = {
  connecting: { tone: 'idle', label: 'conectando', hint: 'abriendo el flujo de eventos' },
  live: { tone: 'ok', label: 'en vivo', hint: 'eventos enviados por el servidor, 1 Hz' },
  stale: { tone: 'warn', label: 'obsoleto', hint: 'flujo abierto pero sin tramas' },
  polling: { tone: 'warn', label: 'sondeo', hint: 'se recurrió a GET /api/live' },
  offline: { tone: 'bad', label: 'sin conexión', hint: 'no hay gateway en este host' },
};

const TIME_SOURCE_HINT: Record<TimeSource, string> = {
  ntp: 'hora de red',
  espnow: 'recibida del registrador OD por ESP-NOW',
  rtc: 'DS3231 local',
  monotonic: 'reconstruida desde uptime — las marcas están ordenadas pero no ancladas',
};

export function StatusStrip({
  registry,
  snapshot,
  streamState,
  frames,
  reconnects,
}: {
  registry: Registry | null;
  snapshot: LiveSnapshot | null;
  streamState: StreamState;
  frames: number;
  reconnects: number;
}) {
  const presentation = STREAM_PRESENTATION[streamState];
  const displays = roleDisplays(registry);

  return (
    <Panel
      title="Presencia de nodos"
      subtitle="Estado informado por el broker, no un latido inferido"
      icon={<Radio className="w-4 h-4 text-sky-400" />}
      actions={
        <div className="flex items-center gap-2">
          <Badge tone={presentation.tone} pulse={streamState === 'stale'}>
            {streamState === 'live' ? (
              <Wifi className="w-3 h-3" />
            ) : streamState === 'offline' ? (
              <WifiOff className="w-3 h-3" />
            ) : (
              <Activity className="w-3 h-3" />
            )}
            {presentation.label}
          </Badge>
          <span className="text-[11px] text-slate-500 whitespace-nowrap">
            {frames.toLocaleString()} frames
            {reconnects > 0 ? ` · ${reconnects} reconnects` : ''}
          </span>
        </div>
      }
    >
      <p className="text-[11px] text-slate-500 -mt-2">{presentation.hint}</p>

      {displays.length === 0 ? (
        <p className="text-sm text-slate-400 py-4 text-center">Registro no cargado.</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
          {displays.map((display) => {
            const node = snapshot?.nodes?.[display.role] ?? null;
            const def = registry?.roles?.[display.role];
            const stale = nodeStale(node, def);
            const tone: Tone = !node ? 'idle' : !node.online ? 'bad' : stale ? 'warn' : 'ok';
            const offset = describeClockOffset(node?.clock_offset_ms);
            const signal = describeRssi(node?.rssi);
            // Only meaningful while the node is talking; a stale rate is not a fact about
            // the present.
            const observed = node?.online ? node?.observed_hz ?? null : null;
            const trust = node ? TIME_SOURCE_TRUST[node.t_src] ?? 0 : null;
            // Null means the node has never reported an envelope, so there is nothing to
            // compare. Reading that as "v0 firmware" made a node that had simply never
            // connected look like one running un-migrated firmware.
            const schemaMismatch =
              node && registry && node.schema_version !== null
                ? node.schema_version !== registry.schema_version
                : false;

            return (
              <div
                key={display.role}
                className={cn(
                  'rounded-xl border px-3.5 py-3 flex flex-col gap-2',
                  TONE[tone].bg,
                  TONE[tone].border,
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={cn('w-2 h-2 rounded-full shrink-0', TONE[tone].dot)} />
                    <span className="text-xs font-bold text-white truncate">{display.role}</span>
                  </div>
                  <span className={cn('text-[11px] font-semibold', TONE[tone].text)}>
                    {!node ? 'sin datos' : !node.online ? 'sin conexión' : stale ? 'en silencio' : 'en línea'}
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                  <span className="text-slate-500">última escucha</span>
                  <span className="text-slate-300 text-right font-mono">
                    {formatAge(node?.age_s ?? null)}
                  </span>

                  <span className="text-slate-500">clock</span>
                  <span
                    className={cn(
                      'text-right font-mono',
                      trust === null ? 'text-slate-500' : trust === 0 ? 'text-amber-400' : 'text-slate-300',
                    )}
                    title={node ? TIME_SOURCE_HINT[node.t_src] : undefined}
                  >
                    {node?.t_src ?? '—'}
                  </span>

                  <span className="text-slate-500">offset</span>
                  <span
                    className={cn(
                      'text-right font-mono',
                      offset.severity === 'ok'
                        ? 'text-slate-300'
                        : offset.severity === 'warn'
                          ? 'text-amber-400'
                          : 'text-rose-400',
                    )}
                  >
                    {offset.label}
                  </span>

                  <span className="text-slate-500">signal</span>
                  <span
                    className={cn(
                      'text-right font-mono inline-flex items-center justify-end gap-1',
                      signal.severity === 'ok'
                        ? 'text-slate-300'
                        : signal.severity === 'warn'
                          ? 'text-amber-400'
                          : 'text-rose-400',
                    )}
                  >
                    <Signal className="w-3 h-3" />
                    {signal.label}
                  </span>
                </div>

                {(trust === 0 || schemaMismatch) && (
                  <div className="flex flex-col gap-1 pt-1 border-t border-scada-border/80">
                    {trust === 0 ? (
                      <p className="text-[10px] text-amber-400/90">
                        Clock reconstructed from uptime — samples will be flagged in exports.
                      </p>
                    ) : null}
                    {schemaMismatch ? (
                      <p className="text-[10px] text-amber-400/90">
                        Publishing schema v{node?.schema_version} but the gateway expects v
                        {registry?.schema_version}. Firmware not yet migrated.
                      </p>
                    ) : null}
                  </div>
                )}

                <p
                  className="text-[10px] text-slate-500 truncate"
                  title={`${display.label} — declared cadence ${display.cadenceLabel}`}
                >
                  {observed !== null
                    ? `receiving ${formatHz(observed)} · declared ${display.cadenceLabel}`
                    : `declared ${display.cadenceLabel}`}
                </p>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

/** Compact one-line summary used in the page header. */
export function PresenceSummary({
  snapshot,
  registry,
}: {
  snapshot: LiveSnapshot | null;
  registry: Registry | null;
}) {
  const displays = roleDisplays(registry);
  const online = displays.filter((d) => {
    const node = snapshot?.nodes?.[d.role] ?? null;
    return node?.online && !nodeStale(node, registry?.roles?.[d.role]);
  }).length;

  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
      <span
        className={cn(
          'w-2 h-2 rounded-full',
          online === 0 ? 'bg-rose-400' : online === displays.length ? 'bg-emerald-400' : 'bg-amber-400',
        )}
      />
      {online}/{displays.length} nodos en línea
    </span>
  );
}

/** Uptime of the ingest session, phrased for an operator rather than a developer. */
export function SessionAge({
  lastFrameAt,
  snapshot,
}: {
  lastFrameAt: number | null;
  snapshot?: LiveSnapshot | null;
}) {
  if (lastFrameAt === null) return <span className="text-xs text-slate-500">sin conexión con gateway</span>;
  const seconds = (Date.now() - lastFrameAt) / 1000;
  const anyNodeOnline = Object.values(snapshot?.nodes ?? {}).some((n) => n.online);

  return (
    <span className="text-xs text-slate-500 inline-flex items-center gap-1.5">
      <span>stream gateway hace {formatDuration(seconds)}</span>
      {snapshot && !anyNodeOnline ? (
        <span className="text-amber-400/90 font-medium">(sin telemetría de sensores)</span>
      ) : null}
    </span>
  );
}

