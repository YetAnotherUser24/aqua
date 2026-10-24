'use client';

/**
 * Gateway health, framed around the three numbers that actually predict trouble.
 *
 * From `docs/RUNBOOK.md`, the numbers worth watching on this deployment are:
 *
 *   1. **flush backlog** — age of the oldest archive file not yet pushed to object storage.
 *      With a 14-day local retention window this is the only warning before loss becomes
 *      permanent. Everything else here is context for it.
 *   2. **block writes and bytes per block** — the SD-card canary. A healthy stream writes in
 *      large blocks; a low bytes-per-block means small scattered writes, which is what wears
 *      a card out. This is why the archive buffers instead of flushing per batch.
 *   3. **spilled events** — the event log accumulating on disk because the database is behind.
 *      Climbing means the database, not the archive, is the bottleneck.
 *
 * `unknown_pending` is the migration tell: it counts telemetry fields that arrived but are not
 * in the registry. A node still running v1 firmware shows up here as a number, rather than as
 * a column of dashes nobody notices.
 */

import { useState } from 'react';
import { Clock, Cog, Cpu, DatabaseZap, HardDrive, RefreshCw, Thermometer, Zap } from 'lucide-react';
import { getHealth, rotateArchive } from '@/lib/api';
import { useAsync, useInterval } from '@/hooks/useAsync';
import { formatBytes, formatDuration, ABSENT } from '@/lib/format';
import { Badge, ErrorState, Loading, Panel, TONE, type Tone, cn } from './ui';

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
function bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function Stat({
  label,
  value,
  hint,
  tone = 'idle',
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: Tone;
}) {
  return (
    <div className={cn('rounded-xl border px-3 py-2.5 flex flex-col gap-0.5', TONE[tone].bg, TONE[tone].border)}>
      <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </span>
      <span className={cn('text-lg font-bold leading-tight', TONE[tone].text)}>{value}</span>
      {hint ? <span className="text-[10px] text-slate-500">{hint}</span> : null}
    </div>
  );
}

export function HealthPanel() {
  const { data, error, loading, reload } = useAsync(() => getHealth(), []);
  const [rotating, setRotating] = useState(false);
  const [rotateMessage, setRotateMessage] = useState<string | null>(null);

  useInterval(reload, 30_000);

  const archive = record(data?.archive);
  const ingest = record(data?.ingest);
  const rollup = record(data?.rollup);
  const events = record(data?.events);
  const commands = record(data?.commands);
  const database = record(data?.database);
  const storage = record(data?.storage);
  const dbStorage = record(storage.database);
  const sysStorage = record(storage.system);

  const hardware = data?.hardware ?? null;
  const cpuTemp = num(hardware?.cpu_temp_c);
  const rtc = hardware?.rtc ?? null;
  const rtcTemp = num(rtc?.temp_c);
  const rtcStatus = typeof rtc?.status === 'string' ? rtc.status : 'unavailable';
  const powerIn = hardware?.power_in ?? null;
  const power5v = hardware?.power_5v ?? null;

  const vinV = num(powerIn?.voltage_v);
  const vinA = num(powerIn?.current_a);
  const vinW = num(powerIn?.power_w);

  const v5vV = num(power5v?.voltage_v);
  const v5vA = num(power5v?.current_a);
  const v5vW = num(power5v?.power_w);

  const cpuTone: Tone = cpuTemp === null ? 'idle' : cpuTemp < 65 ? 'ok' : cpuTemp < 78 ? 'warn' : 'bad';
  const rtcTone: Tone = rtcStatus === 'ok' ? 'ok' : rtcStatus === 'power_lost' ? 'warn' : 'idle';
  const vinTone: Tone = vinV === null ? 'idle' : (vinV >= 10.5 && vinV <= 14.5) ? 'ok' : 'warn';
  const v5vTone: Tone = v5vV === null ? 'idle' : (v5vV >= 4.75 && v5vV <= 5.25) ? 'ok' : 'warn';

  const backlog = data?.flush_backlog_s ?? null;
  const blockWrites = num(archive.block_writes);
  const bytesPerBlock = num(archive.bytes_per_block);
  const rowsWritten = num(archive.rows_written);
  const bytesWritten = num(archive.bytes_written);
  const rotations = num(archive.rotations);
  const flushes = num(archive.flushes);
  const openFiles = record(archive.open_files);
  const spilled = num(events.spilled);
  const buffered = num(events.buffered);
  const unknownPending = num(ingest.unknown_pending);
  const connected = bool(ingest.connected);
  const dbAvailable = bool(database.available);
  const dbSizeMb = num(dbStorage.used_mb) ?? num(database.database_size_mb);
  const dbDisplay = typeof dbStorage.display === 'string' ? dbStorage.display : (dbSizeMb !== null ? `${dbSizeMb} MB / 15 GB` : ABSENT);
  const sysDisplay = typeof sysStorage.display === 'string' ? sysStorage.display : ABSENT;
  const sysPercent = num(sysStorage.percent_used);
  const sysTone: Tone = sysPercent === null ? 'idle' : sysPercent < 80 ? 'ok' : sysPercent < 90 ? 'warn' : 'bad';
  const dbError = typeof database.error === 'string' ? database.error : null;

  // Backlog thresholds mirror the runbook: a day of backlog is a conversation, two days is a
  // problem, and anything approaching the 14-day retention window is data loss in progress.
  const backlogTone: Tone =
    backlog === null ? 'idle' : backlog < 86_400 ? 'ok' : backlog < 3 * 86_400 ? 'warn' : 'bad';

  // 64 KiB blocks are the target; below ~4 KiB the writes are effectively unbuffered.
  const writeTone: Tone =
    bytesPerBlock === null ? 'idle' : bytesPerBlock >= 16_384 ? 'ok' : bytesPerBlock >= 4_096 ? 'warn' : 'bad';

  const spilledTone: Tone = spilled === null ? 'idle' : spilled === 0 ? 'ok' : spilled < 100 ? 'warn' : 'bad';

  const onRotate = async () => {
    setRotating(true);
    setRotateMessage(null);
    try {
      const result = await rotateArchive();
      setRotateMessage(
        `cerrados ${result.rotated.length} archivo(s) abiertos, finalizados ${result.finalised.length} registro(s) obsoletos para ${result.today}`,
      );
      reload();
    } catch (err: unknown) {
      setRotateMessage(err instanceof Error ? `falló: ${err.message}` : 'falló');
    } finally {
      setRotating(false);
    }
  };

  return (
    <Panel
      title="Salud del gateway"
      subtitle="El backlog de escritura es el número que predice pérdida de datos"
      icon={<HardDrive className="w-4 h-4 text-sky-400" />}
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
      {error ? <ErrorState error={error} onRetry={reload} compact /> : null}
      {!error && loading && !data ? <Loading label="Leyendo salud del gateway" /> : null}

      {!error && data ? (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-5 gap-2.5">
            <Stat
              label="Backlog de escritura"
              value={backlog === null ? ABSENT : formatDuration(backlog)}
              hint={
                backlog === null
                  ? 'sin archivos pendientes de subir'
                  : `subida pendiente más antigua · ventana de retención de 14 d`
              }
              tone={backlogTone}
            />
            <Stat
              label="Bloques escritos a SD"
              value={blockWrites === null ? ABSENT : blockWrites.toLocaleString()}
              hint={
                bytesPerBlock === null
                  ? 'desde el inicio del gateway'
                  : `${formatBytes(bytesPerBlock)} por bloque desde el inicio`
              }
              tone={writeTone}
            />
            <Stat
              label="Almacenamiento BD"
              value={dbDisplay}
              hint={
                dbAvailable
                  ? 'Cuota 15 GB · PostgreSQL'
                  : 'base de datos sin conexión'
              }
              tone={dbAvailable === null ? 'idle' : dbAvailable ? 'ok' : 'warn'}
            />
            <Stat
              label="Disco Sistema (SO)"
              value={sysDisplay}
              hint="espacio en almacenamiento raíz"
              tone={sysTone}
            />
            <Stat
              label="Eventos derramados"
              value={spilled === null ? ABSENT : spilled.toLocaleString()}
              hint={
                buffered !== null
                  ? `${buffered} en búfer de memoria`
                  : 'en cola en disco, base de datos atrasada'
              }
              tone={spilledTone}
            />
          </div>

          {/* HAT Hardware Telemetry: INA226s, DS3231 RTC, CPU Temp */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
            <Stat
              label="Térmica (CPU / RTC)"
              value={cpuTemp !== null ? `${cpuTemp} °C` : ABSENT}
              hint={
                rtcTemp !== null
                  ? `RTC DS3231: ${rtcTemp} °C`
                  : 'RTC temp no disponible'
              }
              tone={cpuTone}
            />
            <Stat
              label="Entrada Vin (12V)"
              value={vinV !== null ? `${vinV.toFixed(2)} V` : ABSENT}
              hint={
                vinA !== null && vinW !== null
                  ? `${vinA.toFixed(2)} A · ${vinW.toFixed(2)} W`
                  : 'INA226 (0x40) sin lectura'
              }
              tone={vinTone}
            />
            <Stat
              label="Salida 5V"
              value={v5vV !== null ? `${v5vV.toFixed(2)} V` : ABSENT}
              hint={
                v5vA !== null && v5vW !== null
                  ? `${v5vA.toFixed(2)} A · ${v5vW.toFixed(2)} W`
                  : 'INA226 (0x41) sin lectura'
              }
              tone={v5vTone}
            />
            <Stat
              label="Reloj RTC DS3231"
              value={
                rtcStatus === 'ok'
                  ? 'Sincronizado'
                  : rtcStatus === 'power_lost'
                  ? 'Pérdida Energía'
                  : 'No disponible'
              }
              hint={
                rtc?.valid
                  ? 'Oscilador activo · Batería OK'
                  : 'Comprobar batería de respaldo'
              }
              tone={rtcTone}
            />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 text-[11px]">
            <div className="rounded-xl border border-scada-border bg-scada-well px-3 py-2.5">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5 flex items-center gap-1.5">
                <HardDrive className="w-3 h-3" />
                Escritor de archivo
              </p>
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
                <dt className="text-slate-500">filas escritas</dt>
                <dd className="text-right font-mono text-slate-300">
                  {rowsWritten === null ? ABSENT : rowsWritten.toLocaleString()}
                </dd>
                <dt className="text-slate-500">bytes escritos</dt>
                <dd className="text-right font-mono text-slate-300">
                  {formatBytes(bytesWritten)}
                </dd>
                <dt className="text-slate-500">vaciados a disco</dt>
                <dd className="text-right font-mono text-slate-300">
                  {flushes === null ? ABSENT : flushes.toLocaleString()}
                </dd>
                <dt className="text-slate-500">rotaciones</dt>
                <dd className="text-right font-mono text-slate-300">
                  {rotations === null ? ABSENT : rotations.toLocaleString()}
                </dd>
              </dl>
              {Object.keys(openFiles).length > 0 ? (
                <div className="mt-2 pt-2 border-t border-scada-border/70">
                  <p className="text-slate-500 mb-0.5">archivos abiertos</p>
                  {Object.entries(openFiles).map(([role, path]) => (
                    <p key={role} className="font-mono text-[10px] text-slate-400 truncate" title={String(path)}>
                      {role}: {String(path)}
                    </p>
                  ))}
                </div>
              ) : (
                <p className="mt-2 pt-2 border-t border-scada-border/70 text-slate-500">
                  sin archivos abiertos — nada se ha escrito en esta sesión
                </p>
              )}
            </div>

            <div className="rounded-xl border border-scada-border bg-scada-well px-3 py-2.5">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5 flex items-center gap-1.5">
                <DatabaseZap className="w-3 h-3" />
                Pipeline
              </p>
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
                <dt className="text-slate-500">mqtt</dt>
                <dd className="text-right font-mono text-slate-300">
                  {connected === null ? ABSENT : connected ? 'conectado' : 'desconectado'}
                </dd>
                <dt className="text-slate-500">nodos monitoreados</dt>
                <dd className="text-right font-mono text-slate-300">
                  {num(ingest.tracked_nodes)?.toString() ?? ABSENT}
                </dd>
                <dt className="text-slate-500">buckets de agregado</dt>
                <dd className="text-right font-mono text-slate-300">
                  {num(rollup.open_buckets)?.toString() ??
                    num(rollup.buckets_open)?.toString() ??
                    num(rollup.pending)?.toString() ??
                    ABSENT}
                </dd>
                <dt className="text-slate-500">suscriptores en vivo</dt>
                <dd className="text-right font-mono text-slate-300">
                  {data.live_subscribers}
                </dd>
                <dt className="text-slate-500">comandos emitidos</dt>
                <dd className="text-right font-mono text-slate-300">
                  {num(commands.issued)?.toString() ?? ABSENT}
                </dd>
                <dt className="text-slate-500">comandos reemplazados</dt>
                <dd className="text-right font-mono text-slate-300">
                  {num(commands.superseded)?.toString() ?? ABSENT}
                </dd>
              </dl>

              {unknownPending !== null && unknownPending > 0 ? (
                <div className={cn('mt-2 pt-2 border-t border-scada-border/70')}>
                  <p className={cn('flex items-center gap-1.5 font-semibold', TONE.warn.text)}>
                    <Badge tone="warn">{unknownPending} campo(s) desconocido(s)</Badge>
                  </p>
                  <p className="text-slate-500 mt-1">
                    Llegó telemetría con campos que el registro no define. Normalmente un nodo
                    todavía con el firmware antiguo — sus valores se descartan del archivo, no se
                    malinterpretan.
                  </p>
                </div>
              ) : null}

              {dbError ? (
                <p className={cn('mt-2 pt-2 border-t border-scada-border/70', TONE.warn.text)}>
                  db: {dbError}
                </p>
              ) : null}
            </div>
          </div>

          {/* Rotation is normally driven by a systemd timer. Exposing it is for the runbook:
              when a day boundary is missed, an operator needs to close the files by hand. */}
          <div className="flex flex-wrap items-center gap-3 border-t border-scada-border pt-3">
            <button
              type="button"
              disabled={rotating}
              onClick={onRotate}
              className={cn(
                'inline-flex items-center gap-1.5 text-[11px] font-semibold px-3 py-1.5 rounded-lg border transition-colors',
                rotating
                  ? 'border-scada-border text-slate-500 cursor-wait'
                  : 'border-scada-border bg-scada-elevated hover:bg-scada-borderHover text-slate-200',
              )}
            >
              <Cog className={cn('w-3 h-3', rotating && 'animate-spin')} />
              Cerrar archivos de archivo abiertos
            </button>
            <span className="text-[10px] text-slate-500">
              Normalmente lo ejecuta <code>aqua-rotate.timer</code> a las 00:05. Es seguro
              ejecutarlo a mano.
            </span>
            {rotateMessage ? (
              <span className="text-[10px] text-slate-400">{rotateMessage}</span>
            ) : null}
          </div>
        </>
      ) : null}
    </Panel>
  );
}
