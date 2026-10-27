'use client';

/**
 * The export history modal: what bundles already exist for an experiment.
 *
 * Reads `GET /api/experiments` to let the operator inspect past or expired experiments,
 * and `GET /api/experiments/{id}/exports` to scan the filesystem.
 *
 * Allows generating fresh exports (crudo / alineado) and downloading bundles with one click.
 */

import { useMemo, useState } from 'react';
import { Activity, Cpu, Download, FileArchive, FileSpreadsheet, History, Loader2, RefreshCw, X } from 'lucide-react';
import '@/lib/chartSetup';
import { Line } from 'react-chartjs-2';
import { exportExperiment, exportExperimentNative, getExperimentExports, getExperimentPreview, getExperiments } from '@/lib/api';
import { useAsync } from '@/hooks/useAsync';
import { formatDateTime } from '@/lib/dateUtils';
import { formatBytes } from '@/lib/format';
import type { Experiment } from '@/lib/types';
import { cn } from './ui';

function formatLabel(format: string): string {
  if (format === 'aqua-native-1' || format === 'native') return 'Crudo (tasa nativa)';
  if (format === 'aligned') return 'Bundle alineado (1 Hz)';
  return format;
}

export function ExportHistoryModal({
  experimentId: initialExperimentId,
  onClose,
}: {
  experimentId: string | null;
  onClose: () => void;
}) {
  const [selectedExpId, setSelectedExpId] = useState<string | null>(initialExperimentId);
  const [generating, setGenerating] = useState<'native' | 'aligned' | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  // Load past experiments list
  const experimentsAsync = useAsync(async () => {
    const res = await getExperiments(50);
    return res.experiments;
  }, []);

  const experiments = experimentsAsync.data ?? [];
  const currentExpId = selectedExpId || (experiments.length > 0 ? experiments[0].id : null);
  const currentExp = experiments.find((e) => e.id === currentExpId);

  // Load exports for current experiment
  const exportsAsync = useAsync(
    () =>
      currentExpId
        ? getExperimentExports(currentExpId)
        : Promise.resolve({ experiment_id: '', exports: [] }),
    [currentExpId],
  );

  // Load DO preview curve for current experiment
  const previewAsync = useAsync(
    () =>
      currentExpId
        ? getExperimentPreview(currentExpId)
        : Promise.resolve(null),
    [currentExpId],
  );

  const previewPoints = useMemo(() => {
    const rawPts = previewAsync.data?.points ?? [];
    const strictlyIncreasing: typeof rawPts = [];
    let lastX = -Infinity;
    for (const pt of rawPts) {
      if (pt.t_rel_s !== null && Number.isFinite(pt.t_rel_s) && pt.t_rel_s > lastX) {
        strictlyIncreasing.push(pt);
        lastX = pt.t_rel_s;
      }
    }
    return strictlyIncreasing;
  }, [previewAsync.data?.points]);

  const previewChartData = useMemo(() => {
    return {
      datasets: [
        {
          label: 'OD (mg/L)',
          data: previewPoints.map((pt) => ({ x: pt.t_rel_s, y: pt.do })),
          borderColor: '#06b6d4',
          backgroundColor: 'rgba(6, 182, 212, 0.2)',
          fill: true,
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.2,
        },
      ],
    };
  }, [previewPoints]);

  const previewChartOptions = useMemo(() => {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false as const,
      scales: {
        x: {
          type: 'linear' as const,
          grid: { color: 'rgba(51, 65, 85, 0.3)' },
          ticks: {
            color: '#64748b',
            font: { size: 9, family: 'monospace' },
            callback: (s: number | string) => {
              const num = Number(s);
              const mins = Math.floor(num / 60);
              const secs = Math.floor(num % 60);
              return secs === 0 ? `${mins}m` : `${mins}:${String(secs).padStart(2, '0')}`;
            },
            maxRotation: 0,
          },
        },
        y: {
          type: 'linear' as const,
          grid: { color: 'rgba(51, 65, 85, 0.3)' },
          ticks: {
            color: '#64748b',
            font: { size: 9, family: 'monospace' },
            callback: (v: number | string) => Number(v).toFixed(1),
          },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          borderColor: '#334155',
          borderWidth: 1,
          padding: 6,
          titleColor: '#e2e8f0',
          titleFont: { size: 10, family: 'monospace' },
          bodyColor: '#22d3ee',
          bodyFont: { size: 10, family: 'monospace', weight: 'bold' as const },
          callbacks: {
            title: (items: any[]) => {
              if (!items.length) return '';
              const s = Number(items[0].parsed.x);
              return `t: ${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
            },
            label: (item: any) => `OD: ${Number(item.parsed.y).toFixed(2)} mg/L`,
          },
        },
      },
    };
  }, []);

  const exportsList = exportsAsync.data?.exports ?? [];

  const handleBuild = async (kind: 'native' | 'aligned') => {
    if (!currentExpId) return;
    setGenerating(kind);
    setActionMsg(kind === 'native' ? 'Generando export crudo…' : 'Generando bundle 1 Hz…');
    try {
      if (kind === 'native') {
        await exportExperimentNative(currentExpId, { compress: true });
      } else {
        await exportExperiment(currentExpId, { gridSeconds: 1, compress: true });
      }
      setActionMsg('Exportación completada. Iniciando descarga…');
      await exportsAsync.reload();
      window.location.href = `/api/experiments/${encodeURIComponent(currentExpId)}/download?format=${kind}`;
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : 'Error al exportar');
    } finally {
      setGenerating(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="bg-scada-surface border border-scada-border rounded-2xl max-w-2xl w-full p-5 space-y-4 shadow-2xl max-h-[92vh] overflow-y-auto">
        <div className="flex items-center justify-between border-b border-scada-border pb-3">
          <div className="flex items-center gap-2 text-white font-bold text-sm">
            <History className="w-4 h-4 text-amber-400" />
            <span>Historial y Descarga de Ensayos</span>
          </div>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Experiment Selector */}
        <div className="space-y-1.5">
          <label className="text-[10px] text-slate-400 font-mono uppercase tracking-wider block">
            Seleccionar Ensayo:
          </label>
          <div className="flex items-center gap-2">
            <select
              value={currentExpId ?? ''}
              onChange={(e) => {
                setSelectedExpId(e.target.value);
                setActionMsg(null);
              }}
              className="flex-1 bg-scada-well border border-scada-border text-slate-200 text-xs rounded-xl px-3 py-2 font-mono focus:outline-none focus:border-telemetry-do"
            >
              {experiments.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.id} — {e.title} ({e.status})
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => {
                experimentsAsync.reload();
                exportsAsync.reload();
                previewAsync.reload();
              }}
              className="p-2 bg-scada-elevated hover:bg-slate-700 text-slate-300 rounded-xl border border-scada-border transition"
              title="Refrescar lista"
            >
              <RefreshCw className={cn('w-4 h-4', (experimentsAsync.loading || exportsAsync.loading || previewAsync.loading) && 'animate-spin')} />
            </button>
          </div>
          {currentExp ? (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-400 font-mono px-1">
              <span>Estado: <strong className={currentExp.status === 'completed' ? 'text-emerald-400' : 'text-amber-400'}>{currentExp.status}</strong></span>
              {currentExp.started_at ? (
                <span>Inicio: {formatDateTime(currentExp.started_at, true)}</span>
              ) : null}
              {currentExp.ended_at ? (
                <span>Fin: {formatDateTime(currentExp.ended_at, true)}</span>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* DO Preview Curve (Item 13) */}
        <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-1.5 text-[10px] font-bold text-slate-300 uppercase tracking-wider">
              <Activity className="w-3.5 h-3.5 text-telemetry-do" />
              <span>Curva de Oxígeno Disuelto (Previsualización)</span>
            </div>
            {previewAsync.data && previewAsync.data.count > 0 ? (
              <div className="flex items-center gap-2 text-[10px] font-mono text-slate-400">
                <span>Min: <strong className="text-cyan-400">{previewAsync.data.min_do !== null ? previewAsync.data.min_do.toFixed(2) : '--'}</strong> mg/L</span>
                <span>·</span>
                <span>Max: <strong className="text-cyan-400">{previewAsync.data.max_do !== null ? previewAsync.data.max_do.toFixed(2) : '--'}</strong> mg/L</span>
                <span>·</span>
                <span>{Math.round(previewAsync.data.duration_s / 60)} min</span>
              </div>
            ) : null}
          </div>

          {previewAsync.loading ? (
            <div className="h-32 flex items-center justify-center text-xs text-slate-500 font-mono">
              <Loader2 className="w-4 h-4 animate-spin mr-2 text-cyan-400" />
              Cargando perfil de OD...
            </div>
          ) : previewAsync.data && previewPoints.length > 0 ? (
            <div className="h-32 relative w-full pt-1">
              <Line data={previewChartData} options={previewChartOptions as any} />
            </div>
          ) : (
            <div className="h-16 flex items-center justify-center text-xs text-slate-500 font-mono">
              {previewAsync.error ? 'Error al cargar perfil de OD' : 'Sin datos de OD registrados para este ensayo'}
            </div>
          )}
        </div>

        {/* Export generation shortcuts */}
        <div className="p-3 bg-scada-well rounded-xl border border-scada-border space-y-2">
          <div className="text-[10px] text-slate-400 font-bold uppercase">
            Generar y Descargar Nuevo Formato:
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => void handleBuild('native')}
              disabled={!currentExpId || generating !== null}
              className="px-3 py-2 rounded-lg bg-scada-elevated hover:bg-slate-700 text-slate-200 border border-scada-border flex items-center justify-between transition disabled:opacity-50 text-left"
            >
              <div>
                <span className="font-bold text-sky-400 text-xs block">Exportación Cruda</span>
                <span className="text-[9px] text-slate-400">Tasa nativa (5 Hz)</span>
              </div>
              {generating === 'native' ? (
                <Loader2 className="w-4 h-4 animate-spin text-sky-400" />
              ) : (
                <Cpu className="w-4 h-4 text-sky-400" />
              )}
            </button>

            <button
              type="button"
              onClick={() => void handleBuild('aligned')}
              disabled={!currentExpId || generating !== null}
              className="px-3 py-2 rounded-lg bg-scada-elevated hover:bg-slate-700 text-slate-200 border border-scada-border flex items-center justify-between transition disabled:opacity-50 text-left"
            >
              <div>
                <span className="font-bold text-emerald-400 text-xs block">Bundle Alineado</span>
                <span className="text-[9px] text-slate-400">Rejilla 1.0 Hz</span>
              </div>
              {generating === 'aligned' ? (
                <Loader2 className="w-4 h-4 animate-spin text-emerald-400" />
              ) : (
                <FileSpreadsheet className="w-4 h-4 text-emerald-400" />
              )}
            </button>
          </div>
          {actionMsg ? (
            <p className="text-[10px] text-telemetry-do font-mono">{actionMsg}</p>
          ) : null}
        </div>

        {/* Existing Exports List */}
        <div className="space-y-1.5">
          <div className="text-[10px] text-slate-400 font-bold uppercase">
            Archivos Generados en Disco:
          </div>
          {exportsAsync.loading ? (
            <p className="text-xs text-slate-500 font-mono py-2">Buscando archivos…</p>
          ) : null}
          {exportsAsync.error ? (
            <p className="text-xs text-rose-400 font-mono">
              {exportsAsync.error instanceof Error ? exportsAsync.error.message : 'no se pudo leer el historial'}
            </p>
          ) : null}

          {!exportsAsync.loading && !exportsAsync.error && exportsList.length === 0 ? (
            <p className="text-xs text-slate-400 py-2">
              Aún no hay archivos generados para este ensayo. Pulsa los botones superiores para crearlos y descargarlos.
            </p>
          ) : null}

          {exportsList.length > 0 ? (
            <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
              {exportsList.map((entry) => (
                <div
                  key={entry.dir}
                  className="p-2.5 rounded-xl bg-scada-canvas border border-scada-border flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-white font-bold text-xs truncate">
                        {formatLabel(entry.format)}
                      </span>
                      <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-scada-elevated text-slate-400 border border-scada-border">
                        {formatBytes(entry.size_bytes)}
                      </span>
                    </div>
                    <div className="text-slate-400 text-[10px] font-mono truncate">
                      {entry.generated_at ? formatDateTime(entry.generated_at, true) : formatDateTime(entry.modified_at, true)}
                      {' · '}
                      {entry.rows.toLocaleString()} filas · {entry.events} eventos
                    </div>
                  </div>
                  {entry.bundle ? (
                    <a
                      href={`/api/experiments/${encodeURIComponent(entry.experiment_id)}/download?format=${entry.format === 'aligned' ? 'aligned' : 'native'}`}
                      download
                      className="p-2 rounded-lg bg-scada-elevated text-telemetry-do hover:text-white hover:bg-slate-700 border border-scada-border shrink-0 transition flex items-center justify-center cursor-pointer"
                      title="Descargar bundle comprimido (.tar.gz)"
                    >
                      <Download className="w-4 h-4" />
                    </a>
                  ) : (
                    <span
                      className="p-2 rounded-lg bg-scada-elevated text-telemetry-do border border-scada-border shrink-0 opacity-40 flex items-center justify-center"
                      title="sin bundle comprimido"
                    >
                      <FileArchive className="w-4 h-4" />
                    </span>
                  )}
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div className="flex justify-between items-center pt-2 border-t border-scada-border">
          <a
            href="https://data.aqua-station.live"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 px-3 py-1.5 bg-scada-well text-sky-400 hover:text-white rounded-xl text-xs border border-scada-border hover:border-sky-500 transition font-mono"
            title="Abre el repositorio en la nube con visualizador de curvas 24/7 (Cloudflare R2)"
          >
            <span>☁ Archivo Cloud 24/7</span>
            <span>&rarr;</span>
          </a>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 bg-scada-elevated text-white rounded-xl text-xs hover:bg-slate-700 border border-scada-border transition"
          >
            Cerrar
          </button>
        </div>
      </div>
    </div>
  );
}
