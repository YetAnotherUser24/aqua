'use client';

import React, { useState, useEffect, useMemo } from 'react';
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine
} from 'recharts';
import { HistoryDataPoint, SystemThresholds, Experiment } from '@/types';
import { LineChart as ChartIcon, Eye, EyeOff, Calendar, FlaskConical, Download } from 'lucide-react';

interface ChartsSectionProps {
  history: HistoryDataPoint[];
  thresholds: SystemThresholds;
  selectedRange: string;
  onRangeChange: (range: string) => void;
  isLoading: boolean;
  activeExperiment?: Experiment | null;
}

interface PerturbationMarker {
  id: string;
  action: string;
  device_id: string;
  executed_rtc_ms: number | null;
  created_at: string;
}

export const ChartsSection: React.FC<ChartsSectionProps> = ({
  history,
  thresholds,
  selectedRange,
  onRangeChange,
  isLoading,
  activeExperiment
}) => {
  const [activeTab, setActiveTab] = useState<'combined' | 'do' | 'motor' | 'experiment'>('combined');
  const [showDO, setShowDO] = useState<boolean>(true);
  const [showSpeed, setShowSpeed] = useState<boolean>(true);
  const [showTemp, setShowTemp] = useState<boolean>(true);

  // Filtrar estrictamente los datos para la gráfica del experimento
  // Comienza limpio desde activeExperiment.started_at (t = 0)
  const experimentData = useMemo(() => {
    if (!activeExperiment || !activeExperiment.started_at) {
      return [];
    }
    const startMs = new Date(activeExperiment.started_at).getTime();
    const endMs = activeExperiment.ended_at ? new Date(activeExperiment.ended_at).getTime() : Infinity;

    return history.filter(p => {
      const t = new Date(p.timestamp).getTime();
      return t >= startMs && t <= endMs;
    });
  }, [history, activeExperiment]);

  // Auto-transición a vista de experimento cuando se inicia uno nuevo
  useEffect(() => {
    if (activeExperiment && activeExperiment.status === 'active') {
      setActiveTab('experiment');
    }
  }, [activeExperiment?.id, activeExperiment?.status]);

  // V4: perturbaciones físicas con instante exacto de ejecución (ACK rtc_timestamp_ms),
  // usadas para marcar en la gráfica el milisegundo real de la inyección sin latencia de red.
  const [perturbations, setPerturbations] = useState<PerturbationMarker[]>([]);

  useEffect(() => {
    if (!activeExperiment || activeExperiment.status !== 'active') {
      setPerturbations([]);
      return;
    }
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch('/api/commands/recent?hours=6&limit=20', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setPerturbations(Array.isArray(data.perturbations) ? data.perturbations : []);
      } catch {
        /* silencioso: los marcadores son informativos */
      }
    };
    load();
    const timer = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [activeExperiment?.id, activeExperiment?.status]);

  const perturbationMarkers = useMemo(() => {
    if (!perturbations.length || experimentData.length === 0) return [];
    return perturbations
      .map(p => {
        const target = p.executed_rtc_ms as number;
        let best = experimentData[0];
        let bestDiff = Math.abs(new Date(best.timestamp).getTime() - target);
        for (const pt of experimentData) {
          const diff = Math.abs(new Date(pt.timestamp).getTime() - target);
          if (diff < bestDiff) { bestDiff = diff; best = pt; }
        }
        return { id: p.id, label: best.timeLabel, action: p.action, diffMs: bestDiff };
      })
      // Solo se dibuja si la muestra más cercana está a <= 15 s de la perturbación real.
      .filter(m => m.diffMs <= 15000)
      .slice(0, 5);
  }, [perturbations, experimentData]);

  const ranges = [
    { label: 'Última Hora', value: '1h' },
    { label: '24 Horas', value: '24h' },
    { label: '7 Días', value: '7d' }
  ];

  // Custom Tooltip estilizado
  const CustomTooltip = ({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      return (
        <div className="bg-slate-950/95 border border-slate-700/80 p-3 rounded-xl shadow-2xl backdrop-blur-md text-xs">
          <p className="font-semibold text-slate-300 mb-1.5 border-b border-slate-800 pb-1 flex items-center justify-between gap-4">
            <span>Hora de lectura (GMT-5):</span>
            <span className="text-cyan-400 font-bold">{label}</span>
          </p>
          <div className="space-y-1">
            {payload.map((entry: any, index: number) => (
              <div key={`item-${index}`} className="flex items-center justify-between gap-3 font-medium">
                <span style={{ color: entry.color }} className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full" style={{ backgroundColor: entry.color }} />
                  {entry.name}:
                </span>
                <span className="text-slate-100 font-bold">
                  {entry.value} {entry.unit || ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      );
    }
    return null;
  };

  return (
    <div className="glass-panel rounded-2xl p-5 border border-cyan-900/30 flex flex-col gap-5">
      {/* Controles de Gráfica: Pestañas de Vista y Selector de Rango */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 pb-4">
        {/* Selector de tipo de gráfica */}
        <div className="flex items-center gap-1.5 bg-slate-900/90 p-1 rounded-xl border border-slate-800">
          <button
            onClick={() => setActiveTab('combined')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === 'combined'
                ? 'bg-cyan-600 text-white shadow-md shadow-cyan-900/40'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Gráfica Combinada
          </button>
          <button
            onClick={() => setActiveTab('do')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === 'do'
                ? 'bg-cyan-600 text-white shadow-md shadow-cyan-900/40'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Oxígeno Disuelto (OD)
          </button>
          <button
            onClick={() => setActiveTab('motor')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === 'motor'
                ? 'bg-cyan-600 text-white shadow-md shadow-cyan-900/40'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            ODrive S1 (RPM)
          </button>

          <button
            onClick={() => setActiveTab('experiment')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === 'experiment'
                ? 'bg-emerald-600 text-white shadow-md shadow-emerald-900/40'
                : activeExperiment
                ? 'text-emerald-400 bg-emerald-950/60 border border-emerald-800/80 animate-pulse'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <FlaskConical className="w-3.5 h-3.5" />
            <span>Experimento en Vivo</span>
            {activeExperiment && <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping" />}
          </button>
        </div>

        {/* Filtros de Rango Temporal y Zona Horaria */}
        <div className="flex items-center gap-2.5">
          <span className="hidden sm:inline-block text-[11px] text-cyan-400/90 font-medium px-2.5 py-1 rounded-lg bg-cyan-950/60 border border-cyan-900/60">
            Zona: Perú (GMT-5)
          </span>
          <div className="flex items-center gap-1 bg-slate-900/90 p-1 rounded-xl border border-slate-800">
            {ranges.map((r) => (
              <button
                key={r.value}
                onClick={() => onRangeChange(r.value)}
                disabled={isLoading}
                className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                  selectedRange === r.value
                    ? 'bg-slate-700 text-cyan-300 font-semibold'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Toggles de Series para la Gráfica Combinada */}
      {activeTab === 'combined' && (
        <div className="flex flex-wrap items-center justify-between text-xs gap-3">
          <span className="text-slate-400 font-medium">Visibilidad de Series:</span>
          <div className="flex items-center gap-4">
            <button
              onClick={() => setShowDO(!showDO)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border transition-all ${
                showDO
                  ? 'bg-cyan-950/80 text-cyan-300 border-cyan-800'
                  : 'bg-slate-900 text-slate-500 border-slate-800 line-through'
              }`}
            >
              {showDO ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
              <span>Oxígeno Disuelto (mg/L)</span>
            </button>

            <button
              onClick={() => setShowSpeed(!showSpeed)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border transition-all ${
                showSpeed
                  ? 'bg-blue-950/80 text-blue-300 border-blue-800'
                  : 'bg-slate-900 text-slate-500 border-slate-800 line-through'
              }`}
            >
              {showSpeed ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
              <span>ODrive S1 (RPM)</span>
            </button>

            <button
              onClick={() => setShowTemp(!showTemp)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border transition-all ${
                showTemp
                  ? 'bg-teal-950/80 text-teal-300 border-teal-800'
                  : 'bg-slate-900 text-slate-500 border-slate-800 line-through'
              }`}
            >
              {showTemp ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
              <span>Temperatura (°C)</span>
            </button>
          </div>
        </div>
      )}

      {/* Banner de Monitoreo Experimental en Vivo */}
      {activeTab === 'experiment' && (
        <div className="flex flex-wrap items-center justify-between gap-2 p-3 rounded-xl bg-emerald-950/40 border border-emerald-800/60 text-xs animate-in fade-in duration-150">
          <div className="flex items-center gap-2 text-emerald-300">
            <FlaskConical className="w-4 h-4 text-emerald-400 animate-pulse" />
            <span className="font-bold">
              {activeExperiment ? activeExperiment.name : 'Monitoreo Experimental en Vivo'}
            </span>
            {activeExperiment && (
              <span className="text-[11px] font-mono text-emerald-400/80">
                (Archivo SD: {activeExperiment.csv_filename} • Muestreo cada {activeExperiment.sampling_rate_sec}s)
              </span>
            )}
          </div>
          {activeExperiment && (
            <a
              href={`/api/experiments/${activeExperiment.id}/download`}
              download={activeExperiment.csv_filename}
              className="flex items-center gap-1 text-[11px] font-bold text-emerald-300 hover:text-emerald-200 transition-colors"
            >
              <Download className="w-3.5 h-3.5" />
              <span>Descargar CSV</span>
            </a>
          )}
        </div>
      )}

      {/* Contenedor de la Gráfica */}
      <div className="h-[340px] w-full pt-2">
        {activeTab === 'experiment' && experimentData.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-slate-400 gap-3">
            <div className="p-3.5 rounded-2xl bg-emerald-950/50 border border-emerald-800/80 text-emerald-400 shadow-xl shadow-emerald-950/40">
              <FlaskConical className={`w-8 h-8 ${activeExperiment ? 'animate-bounce text-emerald-400' : 'text-slate-500'}`} />
            </div>
            <div className="text-center space-y-1 max-w-md px-4">
              <h4 className="font-bold text-slate-200 text-sm">
                {activeExperiment ? `Experimento activo: ${activeExperiment.name}` : 'No hay ningún experimento en curso'}
              </h4>
              <p className="text-xs text-slate-400">
                {activeExperiment
                  ? `Esperando las primeras muestras del sensor (${activeExperiment.csv_filename} cada ${activeExperiment.sampling_rate_sec}s). La gráfica arrancará limpia desde este instante.`
                  : 'Inicie un ensayo desde la sección de Experimentos en el panel de control del sensor para visualizar las curvas en tiempo real.'}
              </p>
            </div>
          </div>
        ) : history.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-slate-500 gap-2">
            <ChartIcon className="w-8 h-8 text-slate-600" />
            <p className="text-sm font-medium">Sin datos históricos para este rango</p>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            {activeTab === 'combined' ? (
              // 1. GRÁFICA COMBINADA: Doble Eje Y (OD mg/L y Velocidad %)
              <AreaChart data={history} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                <defs>
                  <linearGradient id="colorDO" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#06b6d4" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#06b6d4" stopOpacity={0.0} />
                  </linearGradient>
                  <linearGradient id="colorSpeed" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" opacity={0.6} />
                <XAxis dataKey="timeLabel" stroke="#64748b" tick={{ fontSize: 11 }} />
                
                {/* Eje Y Izquierdo para Oxígeno Disuelto */}
                <YAxis
                  yAxisId="left"
                  stroke="#06b6d4"
                  domain={[0, 12]}
                  tick={{ fontSize: 11 }}
                  unit=" mg/L"
                />
                {/* Eje Y Derecho para Velocidad Aireador ODrive S1 (RPM) */}
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  stroke="#3b82f6"
                  domain={[0, 600]}
                  tick={{ fontSize: 11 }}
                  unit=" RPM"
                />
                
                <Tooltip content={<CustomTooltip />} />
                
                {/* Línea de referencia: OD Mínimo Seguro */}
                <ReferenceLine
                  yAxisId="left"
                  y={thresholds.critical}
                  stroke="#f43f5e"
                  strokeDasharray="4 4"
                  label={{
                    value: `OD Crítico (${thresholds.critical} mg/L)`,
                    fill: '#f43f5e',
                    fontSize: 10,
                    position: 'insideBottomRight'
                  }}
                />

                {showSpeed && (
                  <Area
                    yAxisId="right"
                    type="monotone"
                    dataKey="odrive_rpm"
                    name="ODrive S1 (RPM)"
                    unit=" RPM"
                    stroke="#3b82f6"
                    strokeWidth={2}
                    fillOpacity={1}
                    fill="url(#colorSpeed)"
                  />
                )}

                {showDO && (
                  <Area
                    yAxisId="left"
                    type="monotone"
                    dataKey="dissolved_oxygen_mg_l"
                    name="Oxígeno Disuelto"
                    unit="mg/L"
                    stroke="#06b6d4"
                    strokeWidth={2.5}
                    fillOpacity={1}
                    fill="url(#colorDO)"
                  />
                )}

                {showTemp && (
                  <Line
                    yAxisId="left"
                    type="monotone"
                    dataKey="water_temperature_c"
                    name="Temp. Agua"
                    unit="°C"
                    stroke="#14b8a6"
                    strokeWidth={1.5}
                    dot={false}
                  />
                )}
              </AreaChart>
            ) : activeTab === 'do' ? (
              // 2. GRÁFICA DE OXÍGENO DISUELTO CON LÍNEAS DE UMBRAL
              <AreaChart data={history} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                <defs>
                  <linearGradient id="colorDOOnly" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#06b6d4" stopOpacity={0.5} />
                    <stop offset="95%" stopColor="#06b6d4" stopOpacity={0.0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" opacity={0.6} />
                <XAxis dataKey="timeLabel" stroke="#64748b" tick={{ fontSize: 11 }} />
                <YAxis stroke="#06b6d4" domain={[0, 12]} tick={{ fontSize: 11 }} unit=" mg/L" />
                <Tooltip content={<CustomTooltip />} />
                
                {/* Umbral Crítico */}
                <ReferenceLine
                  y={thresholds.critical}
                  stroke="#f43f5e"
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                  label={{ value: `Mínimo Seguro: ${thresholds.critical} mg/L`, fill: '#f43f5e', fontSize: 11 }}
                />
                {/* Umbral Advertencia */}
                <ReferenceLine
                  y={thresholds.warning}
                  stroke="#f59e0b"
                  strokeDasharray="3 3"
                  label={{ value: `Advertencia: ${thresholds.warning} mg/L`, fill: '#f59e0b', fontSize: 11 }}
                />

                <Area
                  type="monotone"
                  dataKey="dissolved_oxygen_mg_l"
                  name="Oxígeno Disuelto"
                  unit="mg/L"
                  stroke="#06b6d4"
                  strokeWidth={3}
                  fillOpacity={1}
                  fill="url(#colorDOOnly)"
                />
              </AreaChart>
            ) : activeTab === 'experiment' ? (
              // 3. GRÁFICA EN VIVO DEL EXPERIMENTO (Solo lecturas del experimento actual)
              <AreaChart data={experimentData} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                <defs>
                  <linearGradient id="colorExpDO" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#10b981" stopOpacity={0.6} />
                    <stop offset="95%" stopColor="#06b6d4" stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" opacity={0.6} />
                <XAxis dataKey="timeLabel" stroke="#10b981" tick={{ fontSize: 11 }} />
                <YAxis yAxisId="left" stroke="#10b981" domain={[0, 12]} tick={{ fontSize: 11 }} unit=" mg/L" />
                <YAxis yAxisId="right" orientation="right" stroke="#14b8a6" domain={[15, 35]} tick={{ fontSize: 11 }} unit=" °C" />
                <Tooltip content={<CustomTooltip />} />
                <Legend wrapperStyle={{ fontSize: '11px', paddingTop: '6px' }} />

                <ReferenceLine
                  yAxisId="left"
                  y={thresholds.optimal}
                  stroke="#10b981"
                  strokeWidth={1.5}
                  strokeDasharray="3 3"
                  label={{ value: `Meta OD: ${thresholds.optimal} mg/L`, fill: '#10b981', fontSize: 10 }}
                />
                <ReferenceLine
                  yAxisId="left"
                  y={thresholds.critical}
                  stroke="#f43f5e"
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                  label={{ value: `Crítico: ${thresholds.critical} mg/L`, fill: '#f43f5e', fontSize: 10 }}
                />

                <Area
                  yAxisId="left"
                  type="monotone"
                  dataKey="dissolved_oxygen_mg_l"
                  name="OD Experimental"
                  unit="mg/L"
                  stroke="#10b981"
                  strokeWidth={3}
                  fillOpacity={1}
                  fill="url(#colorExpDO)"
                  dot={{ r: 4, fill: '#10b981', stroke: '#064e3b', strokeWidth: 1.5 }}
                  activeDot={{ r: 6, fill: '#10b981', stroke: '#ffffff', strokeWidth: 2 }}
                />

                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="water_temperature_c"
                  name="Temp. Agua"
                  unit="°C"
                  stroke="#14b8a6"
                  strokeWidth={2}
                  dot={{ r: 3, fill: '#14b8a6', stroke: '#0f766e', strokeWidth: 1 }}
                  activeDot={{ r: 5, fill: '#14b8a6', stroke: '#ffffff', strokeWidth: 2 }}
                />

                {/* V4: marcadores de perturbación (dosis/bomba/mixer/escalón) al instante exacto de ejecución */}
                {perturbationMarkers.map((marker) => (
                  <ReferenceLine
                    key={marker.id}
                    yAxisId="left"
                    x={marker.label}
                    stroke="#f59e0b"
                    strokeWidth={1.5}
                    strokeDasharray="2 2"
                    label={{ value: marker.action, fill: '#f59e0b', fontSize: 9, angle: -90, position: 'insideTopLeft' }}
                  />
                ))}
              </AreaChart>
            ) : (
              // 4. GRÁFICA DE VELOCIDAD DE AIREADOR ODRIVE S1 (M8325s) EN RPM
              <AreaChart data={history} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                <defs>
                  <linearGradient id="colorMotorOnly" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.6} />
                    <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" opacity={0.6} />
                <XAxis dataKey="timeLabel" stroke="#64748b" tick={{ fontSize: 11 }} />
                <YAxis stroke="#3b82f6" domain={[0, 600]} tick={{ fontSize: 11 }} unit=" RPM" />
                <Tooltip content={<CustomTooltip />} />

                <Area
                  type="monotone"
                  dataKey="odrive_rpm"
                  name="ODrive S1 (RPM)"
                  unit=" RPM"
                  stroke="#3b82f6"
                  strokeWidth={2.5}
                  fillOpacity={1}
                  fill="url(#colorMotorOnly)"
                />
              </AreaChart>
            )}
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
};
