'use client';

/**
 * The experiment action row and its options, as the mockup's Ensayo tab footer.
 *
 * Four controls: run toggle, stop, SD logging, and a download dropdown. Plus a collapsible
 * "global options" block (SD record, fast transmission, target filename).
 *
 * Two deliberate deviations from the mockup, both because the backend is the authority:
 *
 * * **The filename is read-only.** The gateway names the archive file from the experiment id
 *   and the date; letting the browser name it would produce a name nothing downstream agrees
 *   with. It is shown so the operator can see it, not typed.
 * * **The download offers what the gateway can actually emit.** The mockup's "kLa dataset" is
 *   gone (no kLa calculation exists). In its place: the aligned 1 Hz bundle and the native-rate
 *   export, which are the two shapes the backend really produces.
 */

import { useState } from 'react';
import { Activity, ChevronDown, Cpu, Download, FileSpreadsheet, HardDrive, History, Play, Square } from 'lucide-react';
import { exportExperiment, exportExperimentNative, getExperiments, postCommand } from '@/lib/api';
import { useAsync } from '@/hooks/useAsync';
import type { RunStateResponse } from '@/lib/types';
import { cn } from './ui';
import { ExportHistoryModal } from './ExportHistoryModal';

export function ExperimentActions({
  run,
  busy,
  sdLogging,
  filename,
  title,
  notes,
  onToggleRun,
  onStop,
  onToggleSd,
}: {
  run: RunStateResponse | null;
  busy: boolean;
  /** Reserved. The SD toggle is disabled (no firmware verb yet); kept so callers compile. */
  sdLogging: boolean;
  filename: string;
  /** The name and description the operator gave the run in the pre-start dialog, if any. */
  title?: string | null;
  notes?: string | null;
  onToggleRun: () => void;
  onStop: () => void;
  /** Reserved. See `sdLogging`. */
  onToggleSd: () => void;
}) {
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [optsOpen, setOptsOpen] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  const active = run?.state === 'ACTIVE_EXPERIMENT';
  const paused = Boolean(run?.paused);
  
  // If no experiment is active right now (e.g. IDLE after run finished/expired),
  // fall back to the most recent experiment so the operator can download it immediately.
  const recentExperiments = useAsync(async () => {
    try {
      const res = await getExperiments(10);
      return res.experiments;
    } catch {
      return [];
    }
  }, [run?.experiment_id]);

  const targetExperimentId =
    run?.experiment_id ||
    (recentExperiments.data && recentExperiments.data.length > 0
      ? recentExperiments.data[0].id
      : null);

  const build = async (kind: 'aligned' | 'native') => {
    if (!targetExperimentId) return;
    setDownloadOpen(false);
    setWorking(true);
    setMsg(kind === 'native' ? 'Construyendo export crudo…' : 'Construyendo bundle 1 Hz…');
    try {
      const report =
        kind === 'native'
          ? await exportExperimentNative(targetExperimentId, { compress: true })
          : await exportExperiment(targetExperimentId, { gridSeconds: 1, compress: true });
      setMsg(
        `${kind === 'native' ? 'Export crudo' : 'Bundle 1 Hz'}: ${report.rows.toLocaleString()} filas · ${report.events} eventos`,
      );
      // Automatically prompt browser to download the generated file
      window.location.href = `/api/experiments/${encodeURIComponent(targetExperimentId)}/download?format=${kind}`;
    } catch (err) {
      setMsg(err instanceof Error ? err.message : 'la exportación falló');
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="space-y-2.5">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <button
          type="button"
          onClick={onToggleRun}
          disabled={busy}
          className={cn(
            'py-2.5 rounded-xl font-bold text-xs flex items-center justify-center gap-1.5 transition shadow-sm active:scale-95 disabled:opacity-50',
            active && !paused
              ? 'bg-amber-600 hover:bg-amber-500 text-white'
              : 'bg-emerald-600 hover:bg-emerald-500 text-white',
          )}
        >
          <Play className="w-3.5 h-3.5 fill-white" />
          <span>{active && !paused ? 'Pausar Ensayo' : paused ? 'Reanudar' : 'Iniciar Ensayo'}</span>
        </button>

        <button
          type="button"
          onClick={onStop}
          disabled={busy || !active}
          className={cn(
            'py-2.5 rounded-xl font-bold text-xs flex items-center justify-center gap-1.5 border transition shadow-sm active:scale-95',
            active
              ? 'bg-rose-950/80 hover:bg-rose-900 text-rose-300 border-rose-800'
              : 'bg-slate-800 text-slate-500 border-slate-700 cursor-not-allowed opacity-50',
          )}
          title="Detiene el ensayo y desarma los actuadores a 0"
        >
          <Square className="w-3.5 h-3.5 fill-current" />
          <span>Detener</span>
        </button>

        <button
          type="button"
          disabled
          className="py-2.5 rounded-xl font-mono text-xs flex items-center justify-center gap-1.5 border transition bg-slate-800 text-slate-500 border-slate-700 opacity-50 cursor-not-allowed"
          title="El firmware del nodo aún no expone un verbo de grabación en MicroSD; el botón se habilitará cuando lo haga."
        >
          <HardDrive className="w-3.5 h-3.5" />
          <span>SD: N/D</span>
        </button>

        <div className="relative">
          <button
            type="button"
            onClick={() => setDownloadOpen((v) => !v)}
            disabled={working || (!targetExperimentId && (recentExperiments.data?.length ?? 0) === 0)}
            className="w-full py-2.5 bg-scada-elevated hover:bg-slate-700 text-slate-200 rounded-xl font-bold text-xs flex items-center justify-center gap-1.5 border border-scada-border transition active:scale-95 disabled:opacity-50"
            title={targetExperimentId ? `Descargar exportación para ${targetExperimentId}` : 'Descargar o revisar historial'}
          >
            <Download className="w-3.5 h-3.5 text-telemetry-do" />
            <span>{working ? 'Exportando…' : 'Descargar'}</span>
            <ChevronDown className="w-3 h-3 text-slate-400" />
          </button>

          {downloadOpen ? (
            <div className="absolute right-0 bottom-full mb-1 w-64 bg-scada-surface border border-scada-border rounded-xl shadow-2xl p-2 space-y-1.5 z-30 font-mono text-xs">
              <div className="px-1 text-[10px] text-slate-400 font-bold uppercase border-b border-scada-border pb-1">
                {run?.experiment_id
                  ? `Ensayo activo: ${run.experiment_id}`
                  : targetExperimentId
                    ? `Último ensayo: ${targetExperimentId}`
                    : 'Formatos de exportación:'}
              </div>
              {/* Native first, deliberately. It is the complete record: every sample at the
                  rate it was recorded. The aligned bundle resamples onto a 1 Hz grid and is
                  lossy by construction, so an operator reaching for the top entry gets the
                  one that keeps the 5 Hz aerator signal. The panel is bottom-anchored
                  (`bottom-full`), so the first child is the closest to the button. */}
              {targetExperimentId ? (
                <>
                  <button
                    type="button"
                    onClick={() => void build('native')}
                    className="w-full text-left px-2 py-1.5 rounded hover:bg-scada-elevated text-slate-200 flex items-center justify-between transition"
                  >
                    <div>
                      <span className="font-bold text-sky-400 block">
                        Exportación cruda (tasa nativa)
                        <span className="ml-1.5 text-[9px] font-normal px-1 py-px rounded bg-sky-950/60 text-sky-300 border border-sky-800/60">
                          completo
                        </span>
                      </span>
                      <span className="text-[9px] text-slate-400">
                        Cada muestra · sensores + actuadores + eventos
                      </span>
                    </div>
                    <Cpu className="w-3.5 h-3.5 text-sky-400" />
                  </button>
                  <button
                    type="button"
                    onClick={() => void build('aligned')}
                    className="w-full text-left px-2 py-1.5 rounded hover:bg-scada-elevated text-slate-200 flex items-center justify-between transition"
                  >
                    <div>
                      <span className="font-bold text-emerald-400 block">Bundle alineado (1.0 Hz)</span>
                      <span className="text-[9px] text-slate-400">
                        Rejilla común · remuestreado, pierde los 5 Hz
                      </span>
                    </div>
                    <FileSpreadsheet className="w-3.5 h-3.5 text-emerald-400" />
                  </button>
                </>
              ) : null}
              <button
                type="button"
                onClick={() => {
                  setDownloadOpen(false);
                  setHistoryOpen(true);
                }}
                className="w-full text-left px-2 py-1 rounded hover:bg-scada-elevated text-slate-300 border-t border-slate-800 flex items-center justify-between pt-1.5 transition"
              >
                <span className="text-[11px] font-bold text-amber-400">Historial y todos los ensayos</span>
                <History className="w-3.5 h-3.5 text-amber-400" />
              </button>
            </div>
          ) : null}
        </div>
      </div>

      {run?.recipe_id === 'planta_1_deox' ? (
        <button
          type="button"
          disabled={busy}
          onClick={async () => {
            try {
              await postCommand({ role: 'od_sensor', action: 'start', target: { interval_sec: 5 } });
              setMsg('Loggeo de OD activo a 5 s (alta cadencia continua)');
            } catch (e) {
              setMsg(e instanceof Error ? e.message : 'Error al solicitar loggeo');
            }
          }}
          className="w-full py-2 px-3 rounded-xl bg-teal-950/70 hover:bg-teal-900 border border-teal-700/80 text-teal-300 font-bold text-xs flex items-center justify-center gap-2 transition active:scale-95 shadow-sm"
          title="Permite que el sensor de OD siga registrando datos a 5 s incluso al finalizar el ensayo de desoxigenación"
        >
          <Activity className="w-3.5 h-3.5 text-teal-400" />
          <span>Continuar con Loggeo de OD (Sensor a 5 s)</span>
        </button>
      ) : null}

      {msg ? <p className="text-[10px] text-slate-400 font-mono px-1">{msg}</p> : null}

      {/* Collapsible global options */}
      <div className="border border-scada-border rounded-xl bg-scada-canvas overflow-hidden">
        <button
          type="button"
          onClick={() => setOptsOpen((v) => !v)}
          className="w-full px-3 py-2 flex items-center justify-between text-xs text-slate-300 font-medium hover:bg-scada-elevated transition"
        >
          <span className="flex items-center gap-1.5">
            <HardDrive className="w-3.5 h-3.5 text-telemetry-do" /> Opciones globales del ensayo
          </span>
          <ChevronDown className={cn('w-3.5 h-3.5 text-slate-400 transition-transform', optsOpen && 'rotate-180')} />
        </button>
        {optsOpen ? (
          <div className="p-3 border-t border-scada-border space-y-2.5 text-[11px]">
            <div className="flex items-center justify-between">
              <div>
                <span className="text-slate-200 font-medium block">Grabar en MicroSD del sensor</span>
                <span className="text-slate-500 text-[10px]">
                  Pendiente de verbo en firmware (aún no disponible)
                </span>
              </div>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded border bg-slate-800 text-slate-500 border-slate-700 opacity-60">
                N/D
              </span>
            </div>
            <div className="flex items-center justify-between">
              <div>
                <span className="text-slate-200 font-medium block">Transmisión rápida (5 Hz)</span>
                <span className="text-slate-500 text-[10px]">Corriente, potencia y RPM del ODrive</span>
              </div>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded border bg-slate-800 text-slate-400 border-slate-700">
                declarado en el firmware
              </span>
            </div>
            <div className="pt-1 border-t border-slate-800">
              <label className="text-[10px] text-slate-400 block mb-1">
                Nombre del ensayo:
              </label>
              {/* Echoed back, not editable here. The name is chosen in the dialog that opens
                  when the run starts, because the experiment row is created at that moment and
                  renaming it afterwards is a separate operation the gateway does not offer.
                  Showing it read-only is what makes the earlier choice verifiable. */}
              <input
                type="text"
                value={title ?? '—'}
                readOnly
                className="w-full bg-scada-well text-slate-300 text-xs border border-scada-border rounded p-1.5 cursor-not-allowed"
                title="Se elige al pulsar Iniciar Ensayo"
              />
              {notes ? (
                <p className="mt-1.5 text-[10px] text-slate-400 leading-relaxed line-clamp-3">
                  {notes}
                </p>
              ) : (
                <p className="mt-1.5 text-[10px] text-slate-500 italic">
                  Sin descripción. Se define al pulsar Iniciar Ensayo.
                </p>
              )}
            </div>
            <div className="pt-1 border-t border-slate-800">
              <label className="text-[10px] text-slate-400 block mb-1">
                Archivo destino (asignado por el gateway):
              </label>
              <input
                type="text"
                value={filename}
                readOnly
                className="w-full bg-scada-well text-slate-300 text-xs border border-scada-border rounded p-1.5 font-mono cursor-not-allowed"
                title="El gateway nombra el archivo a partir del id de ensayo y la fecha"
              />
            </div>
          </div>
        ) : null}
      </div>

      {historyOpen ? (
        <ExportHistoryModal experimentId={targetExperimentId} onClose={() => setHistoryOpen(false)} />
      ) : null}
    </div>
  );
}
