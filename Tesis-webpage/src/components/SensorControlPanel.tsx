'use client';

import React, { useState, useEffect, useRef } from 'react';
import {
  Activity,
  Play,
  Square,
  Zap,
  Moon,
  Clock,
  Send,
  AlertTriangle,
  CheckCircle2,
  Sliders,
  PowerOff,
  RefreshCw,
  Info,
  Radio,
  Timer,
  FlaskConical,
  Download,
  FileSpreadsheet,
  ChevronDown,
  ChevronUp,
  Sparkles,
  Trash2
} from 'lucide-react';
import { Device, SensorCommandAction, SensorCommandPayload, Experiment } from '@/types';
import { demoExperiments } from '@/lib/demoData';

interface SensorControlPanelProps {
  sensorDevice: Device | null;
  activeExperiment?: Experiment | null;
  onCommandSent: () => void;
  onExperimentStarted?: (experiment: Experiment) => void;
  onExperimentStopped?: () => void;
}

export const SensorControlPanel: React.FC<SensorControlPanelProps> = ({
  sensorDevice,
  activeExperiment: propActiveExperiment,
  onCommandSent,
  onExperimentStarted,
  onExperimentStopped
}) => {
  const isDeviceOnline = sensorDevice?.status === 'online';
  const targetDeviceId = sensorDevice?.id || 'a0000000-0000-0000-0000-000000000001';

  // 1. Estado de Modo Monitor y Frecuencia de Muestreo
  const initialMonitorActive = Boolean(sensorDevice?.metadata?.monitor_active ?? false);
  const initialMonitorInterval = Number(sensorDevice?.metadata?.monitor_interval_sec ?? 5);
  const initialSleepCycle = Number(sensorDevice?.metadata?.sleep_cycle_min ?? 15);

  const [isMonitorActive, setIsMonitorActive] = useState<boolean>(initialMonitorActive);
  const [monitorIntervalSec, setMonitorIntervalSec] = useState<number>(initialMonitorInterval);

  // 2. Estado de Ciclo de Sueño Autónomo
  const [sleepCycleMin, setSleepCycleMin] = useState<number>(initialSleepCycle);

  // 3. Estado de Suspensión Directa
  const [sleepDurationMin, setSleepDurationMin] = useState<number>(30);
  const [isIndefiniteSleep, setIsIndefiniteSleep] = useState<boolean>(false);
  const [showIndefiniteConfirm, setShowIndefiniteConfirm] = useState<boolean>(false);

  // 4. Estado del Sistema de Experimentos
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [activeExperiment, setActiveExperiment] = useState<Experiment | null>(propActiveExperiment || null);

  useEffect(() => {
    if (propActiveExperiment !== undefined) {
      setActiveExperiment(propActiveExperiment);
    }
  }, [propActiveExperiment]);

  const [showNewExpModal, setShowNewExpModal] = useState<boolean>(false);
  const [showExpHistory, setShowExpHistory] = useState<boolean>(false);

  // Formulario de nuevo experimento
  const [expName, setExpName] = useState<string>('Ensayo Reoxigenación ODrive 01');
  const [expRate, setExpRate] = useState<number>(2);
  const [expFilename, setExpFilename] = useState<string>('EXP_01.CSV');
  const [expDesc, setExpDesc] = useState<string>('Evaluación de transferencia de O2 y dinámica de saturación.');
  const [expMode, setExpMode] = useState<'manual' | 'pid' | 'fuzzy'>('manual');
  const [expPlantTarget, setExpPlantTarget] = useState<'planta_1' | 'planta_2' | 'both'>('both');
  // V4: protocolo experimental (Planta 1 / Planta 2 / Caso B lazo cerrado)
  const [expCaseType, setExpCaseType] = useState<'planta_1_deox' | 'planta_2_step' | 'closed_loop'>('planta_2_step');
  const [expSetpoint, setExpSetpoint] = useState<number>(5.0);
  const [expControllerType, setExpControllerType] = useState<'none' | 'pid' | 'on_off'>('none');
  const [expStepThrottle, setExpStepThrottle] = useState<number>(50);

  // Cronómetro en tiempo real para experimento activo
  const [elapsedSec, setElapsedSec] = useState<number>(0);

  // 5. Estados de Envío y Feedback
  const [isSending, setIsSending] = useState<boolean>(false);
  const [activeAction, setActiveAction] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ type: 'success' | 'error' | 'info' | null; message: string }>({
    type: null,
    message: ''
  });

  const feedbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
    };
  }, []);

  // Cargar lista de experimentos al inicio
  const fetchExperiments = async () => {
    try {
      const res = await fetch('/api/experiments');
      if (res.ok) {
        const data = await res.json();
        const expList: Experiment[] = data.experiments || [];
        setExperiments(expList);
        const running = expList.find(e => e.status === 'active') || null;
        setActiveExperiment(running);
      }
    } catch {
      setExperiments(demoExperiments as Experiment[]);
    }
  };

  useEffect(() => {
    fetchExperiments();
  }, []);

  // Reloj de tiempo transcurrido cuando hay un experimento activo
  useEffect(() => {
    if (!activeExperiment) {
      setElapsedSec(0);
      return;
    }

    const startTs = new Date(activeExperiment.started_at).getTime();
    const updateTimer = () => {
      const sec = Math.max(0, Math.floor((Date.now() - startTs) / 1000));
      setElapsedSec(sec);
    };

    updateTimer();
    const timerInterval = setInterval(updateTimer, 1000);
    return () => clearInterval(timerInterval);
  }, [activeExperiment]);

  // Sincronizar estado cuando los metadatos del sensor llegan o se actualizan desde el servidor
  const hasInitializedSlidersRef = useRef<boolean>(false);
  const isPendingMonitorToggleRef = useRef<boolean>(false);

  useEffect(() => {
    if (sensorDevice?.metadata) {
      if (!hasInitializedSlidersRef.current) {
        if (sensorDevice.metadata.monitor_interval_sec) {
          setMonitorIntervalSec(Number(sensorDevice.metadata.monitor_interval_sec));
        }
        if (sensorDevice.metadata.sleep_cycle_min) {
          setSleepCycleMin(Number(sensorDevice.metadata.sleep_cycle_min));
        }
        hasInitializedSlidersRef.current = true;
      }
      if (sensorDevice.metadata.monitor_active !== undefined && !isPendingMonitorToggleRef.current && !isSending) {
        setIsMonitorActive(Boolean(sensorDevice.metadata.monitor_active));
      }
      if (propActiveExperiment === undefined && sensorDevice.metadata.active_experiment !== undefined) {
        setActiveExperiment(sensorDevice.metadata.active_experiment || null);
      }
    }
  }, [sensorDevice?.metadata, isSending]);

  const showFeedback = (type: 'success' | 'error' | 'info', message: string) => {
    if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
    setFeedback({ type, message });
    feedbackTimerRef.current = setTimeout(() => {
      setFeedback({ type: null, message: '' });
    }, 6000);
  };

  // Función genérica para despachar comandos al sensor
  const sendSensorCommand = async (
    action: SensorCommandAction,
    payloadData: Partial<SensorCommandPayload> = {},
    commandType: 'start' | 'stop' | 'set_config' = 'set_config',
    successMsg: string = 'Comando enviado exitosamente'
  ): Promise<boolean> => {
    setIsSending(true);
    setActiveAction(action);
    setFeedback({ type: null, message: '' });

    try {
      const fullPayload: SensorCommandPayload = {
        action,
        ...payloadData
      };

      const res = await fetch('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: targetDeviceId,
          command_type: commandType,
          payload: fullPayload,
          requested_by: `Operador Web (${action})`
        })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Error al comunicar con el servidor');

      const offlineNotice = !isDeviceOnline 
        ? ' [Encolado: se aplicará en el próximo despertar del sensor]' 
        : '';
      showFeedback('success', `${successMsg}${offlineNotice}`);
      onCommandSent();
      return true;
    } catch (err: any) {
      showFeedback('error', err.message || 'Error de conexión con el servidor');
      return false;
    } finally {
      setIsSending(false);
      setActiveAction(null);
    }
  };

  // 1. Acciones de Modo Monitor
  const handleToggleMonitor = async (targetActive: boolean) => {
    const action: SensorCommandAction = targetActive ? 'start_monitor' : 'stop_monitor';
    const commandType = targetActive ? 'start' : 'stop';
    setIsMonitorActive(targetActive);
    isPendingMonitorToggleRef.current = true;

    try {
      const ok = await sendSensorCommand(
        action,
        { interval_sec: monitorIntervalSec },
        commandType,
        targetActive ? `Modo Monitor iniciado (muestreo cada ${monitorIntervalSec}s)` : 'Modo Monitor detenido'
      );
      if (!ok) {
        setIsMonitorActive(!targetActive);
      }
    } catch {
      setIsMonitorActive(!targetActive);
    } finally {
      setTimeout(() => {
        isPendingMonitorToggleRef.current = false;
      }, 1200);
    }
  };

  const handleUpdateSamplingRate = async (newInterval: number) => {
    setMonitorIntervalSec(newInterval);
    if (isMonitorActive) {
      await sendSensorCommand(
        'set_sampling_rate',
        { interval_sec: newInterval },
        'set_config',
        `Frecuencia de muestreo actualizada a ${newInterval}s`
      );
    }
  };

  // 2. Muestreo Manual Inmediato
  const handleManualSample = async () => {
    await sendSensorCommand(
      'manual_sample',
      {},
      'set_config',
      'Muestreo manual ejecutado. Lectura transmitida al servidor.'
    );
  };

  // 3. Ciclo de Medición Programada (Sleep Cycle)
  const handleApplySleepCycle = async () => {
    await sendSensorCommand(
      'set_sleep_cycle',
      { measure_time_min: sleepCycleMin },
      'set_config',
      `Ciclo de medición programado configurado a ${sleepCycleMin} minutos`
    );
  };

  // 4. Suspensión / Deep Sleep
  const handleTriggerSleep = async () => {
    if (isIndefiniteSleep && !showIndefiniteConfirm) {
      setShowIndefiniteConfirm(true);
      return;
    }

    setShowIndefiniteConfirm(false);
    const minutes = isIndefiniteSleep ? 0 : sleepDurationMin;
    await sendSensorCommand(
      'sleep',
      { minutes, indefinite: isIndefiniteSleep },
      'set_config',
      isIndefiniteSleep
        ? 'Sensor suspendido indefinidamente. Reactivación mediante botón RST/EN.'
        : `Sensor puesto en suspensión profunda por ${minutes} minutos.`
    );
    setIsMonitorActive(false);
  };

  // 5. Gestión de Experimentos
  const handleStartExperiment = async () => {
    if (!expName.trim()) {
      showFeedback('error', 'Por favor ingresa un nombre para el experimento.');
      return;
    }

    setIsSending(true);
    try {
      const res = await fetch('/api/experiments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: expName.trim(),
          sampling_rate_sec: expRate,
          csv_filename: expFilename,
          description: expDesc,
          mode: expMode,
          plant_target: expPlantTarget,
          // V4: protocolos experimentales y frecuencias desacopladas
          case_type: expCaseType,
          setpoint_do: expCaseType === 'closed_loop' ? expSetpoint : null,
          controller_type: expCaseType === 'closed_loop' ? 'pid' : expControllerType,
          sampling_rate_sensor_sec: expRate,
          sampling_rate_motor_sec: 0.2,
          parameters: { step_throttle_pct: expStepThrottle }
        })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Error al iniciar experimento');

      setActiveExperiment(data.experiment);
      setIsMonitorActive(true);
      setMonitorIntervalSec(expRate);
      setShowNewExpModal(false);
      showFeedback('success', `¡Experimento '${data.experiment.name}' iniciado! Muestreando a ${expRate}s en ${expFilename}`);
      
      // Auto-transición a Live View
      onExperimentStarted?.(data.experiment);
      fetchExperiments();
    } catch (err: any) {
      showFeedback('error', err.message || 'Error iniciando experimento');
    } finally {
      setIsSending(false);
    }
  };

  const handleStopExperiment = async () => {
    if (!activeExperiment) return;

    setIsSending(true);
    try {
      const res = await fetch('/api/experiments', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ experiment_id: activeExperiment.id })
      });

      if (!res.ok) throw new Error('Error al detener experimento');

      showFeedback('info', `Experimento '${activeExperiment.name}' finalizado.`);
      setActiveExperiment(null);
      setIsMonitorActive(false);
      onExperimentStopped?.();
      fetchExperiments();
    } catch (err: any) {
      showFeedback('error', err.message || 'Error deteniendo experimento');
    } finally {
      setIsSending(false);
    }
  };

  const handleDownloadCsv = (expId: string, filename: string) => {
    // Dispara la descarga del CSV directo del endpoint
    const url = `/api/experiments/${expId}/download`;
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleDeleteExperiment = async (expId: string, expName: string) => {
    const confirmDelete = window.confirm(
      `¿Deseas eliminar el experimento '${expName}'?\n\nSe eliminará del registro y se purgarán sus mediciones asociadas en la base de datos.`
    );
    if (!confirmDelete) return;

    try {
      const res = await fetch(`/api/experiments/${expId}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delete_readings: true })
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || 'Error al eliminar experimento');
      }

      showFeedback('success', `Experimento '${expName}' eliminado exitosamente.`);
      if (activeExperiment?.id === expId) {
        setActiveExperiment(null);
        setIsMonitorActive(false);
        onExperimentStopped?.();
      }
      fetchExperiments();
      onCommandSent();
    } catch (err: any) {
      showFeedback('error', err.message || 'Error eliminando experimento');
    }
  };

  const handleManualConfirmation = async () => {
    if (!activeExperiment) return;
    setIsSending(true);
    try {
      const res = await fetch('/api/events/mixer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          experiment_id: activeExperiment.id,
          event_type: 'manual_confirmation',
          rtc_timestamp_ms: Date.now()
        })
      });
      if (!res.ok) throw new Error('Error al registrar confirmación');
      showFeedback('success', 'Confirmación de inyección química registrada exitosamente.');
    } catch (err: any) {
      showFeedback('error', err.message || 'Error registrando confirmación');
    } finally {
      setIsSending(false);
    }
  };

  // Formateador de tiempo transcurrido HH:MM:SS
  const formatTimer = (totalSec: number) => {
    const hh = Math.floor(totalSec / 3600).toString().padStart(2, '0');
    const mm = Math.floor((totalSec % 3600) / 60).toString().padStart(2, '0');
    const ss = (totalSec % 60).toString().padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
  };

  const intervalPresets = [1, 2, 5, 10, 15, 30];
  const sleepCyclePresets = [5, 10, 15, 30, 60];
  const timedSleepPresets = [15, 30, 60, 120];

  return (
    <div className="glass-panel rounded-2xl p-5 border border-slate-800/80 bg-slate-900/50 backdrop-blur-md relative overflow-hidden flex flex-col gap-5 shadow-xl">
      {/* Glow ambiental de fondo */}
      <div className="absolute top-0 right-0 w-80 h-80 bg-cyan-500/5 rounded-full blur-3xl pointer-events-none" />

      {/* Cabecera del Panel */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-cyan-950/80 border border-cyan-800/60 text-cyan-400 shadow-md">
            <Activity className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-base font-bold text-white tracking-wide">
                Control del Sensor Óptico & Ensayos Experimentales
              </h3>
              <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold border ${
                isDeviceOnline
                  ? 'bg-emerald-950/80 text-emerald-400 border-emerald-800/60'
                  : 'bg-amber-950/80 text-amber-400 border-amber-800/60'
              }`}>
                <span className={`w-1.5 h-1.5 rounded-full ${isDeviceOnline ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'}`} />
                {isDeviceOnline ? 'Enlace Activo' : 'En Espera / Dormido'}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Aqualabo DIGISENS • Registro en SD (CSV) • Live Telemetry Stream
            </p>
          </div>
        </div>

        {/* Acciones Rápidas: Estado Actual y Botón Nuevo Experimento */}
        <div className="flex items-center gap-2.5">
          {activeExperiment && (
            <div className="flex items-center gap-2 px-3 py-1 rounded-xl bg-cyan-950/80 border border-cyan-700/80 text-cyan-300 text-xs font-bold animate-pulse shadow-md">
              <span className="w-2 h-2 rounded-full bg-cyan-400" />
              <span>EXP EN VIVO: {formatTimer(elapsedSec)}</span>
            </div>
          )}

          <button
            onClick={() => setShowNewExpModal(!showNewExpModal)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold text-cyan-950 bg-gradient-to-r from-cyan-400 to-teal-400 hover:from-cyan-300 hover:to-teal-300 transition-all shadow-md shadow-cyan-900/30"
          >
            <FlaskConical className="w-3.5 h-3.5" />
            <span>{showNewExpModal ? 'Cerrar Creador' : 'Nuevo Experimento'}</span>
          </button>
        </div>
      </div>

      {/* Banner de Feedback Interactivo */}
      {feedback.type && (
        <div className={`p-3 rounded-xl border flex items-center gap-3 text-xs animate-in fade-in duration-200 ${
          feedback.type === 'success'
            ? 'bg-emerald-950/80 border-emerald-800 text-emerald-300'
            : feedback.type === 'error'
            ? 'bg-rose-950/80 border-rose-800 text-rose-300'
            : 'bg-cyan-950/80 border-cyan-800 text-cyan-300'
        }`}>
          {feedback.type === 'success' ? (
            <CheckCircle2 className="w-4 h-4 flex-shrink-0 text-emerald-400" />
          ) : feedback.type === 'error' ? (
            <AlertTriangle className="w-4 h-4 flex-shrink-0 text-rose-400" />
          ) : (
            <Info className="w-4 h-4 flex-shrink-0 text-cyan-400" />
          )}
          <span className="flex-1 font-medium">{feedback.message}</span>
        </div>
      )}

      {/* ============================================================ */}
      {/* SECCIÓN ESPECIAL: EXPERIMENTO EN CURSO (STUDIO ACTIVO)       */}
      {/* ============================================================ */}
      {activeExperiment && (
        <div className="p-4 rounded-2xl border border-cyan-500/50 bg-gradient-to-r from-cyan-950/80 via-slate-900/90 to-teal-950/80 shadow-2xl flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3.5">
            <div className="p-3 rounded-2xl bg-cyan-500/20 text-cyan-300 border border-cyan-400/40">
              <FlaskConical className="w-6 h-6 animate-pulse" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-widest bg-cyan-500 text-cyan-950">
                  En Curso
                </span>
                <h4 className="text-sm font-bold text-white tracking-wide">
                  {activeExperiment.name}
                </h4>
              </div>
              <div className="flex items-center gap-3 text-xs text-slate-300 mt-1">
                <span>Archivo SD: <strong className="text-cyan-300 font-mono">{activeExperiment.csv_filename}</strong></span>
                <span>•</span>
                <span>Frecuencia: <strong className="text-cyan-300 font-mono">{activeExperiment.sampling_rate_sec}s</strong></span>
                <span>•</span>
                <span>Muestras estimadas: <strong className="text-emerald-300 font-mono">
                  {Math.floor(elapsedSec / (activeExperiment.sampling_rate_sec || 1))}
                </strong></span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <div className="text-right pr-2">
              <span className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold block">Tiempo Transcurrido</span>
              <span className="text-xl font-extrabold font-mono text-cyan-300 tracking-wider">
                {formatTimer(elapsedSec)}
              </span>
            </div>

            <button
              onClick={() => handleDownloadCsv(activeExperiment.id, activeExperiment.csv_filename)}
              className="px-3.5 py-2 rounded-xl text-xs font-semibold text-slate-200 bg-slate-800 hover:bg-slate-700 border border-slate-700 transition-all flex items-center gap-1.5 shadow-sm"
              title="Descargar CSV con las muestras tomadas hasta este momento"
            >
              <Download className="w-3.5 h-3.5 text-cyan-400" />
              <span>CSV</span>
            </button>

            <button
              onClick={handleManualConfirmation}
              disabled={isSending}
              className="px-3.5 py-2 rounded-xl text-xs font-semibold text-amber-950 bg-amber-400 hover:bg-amber-300 transition-all flex items-center gap-1.5 shadow-sm"
              title="Confirmar que se ha vertido la solución química"
            >
              <FlaskConical className="w-3.5 h-3.5" />
              <span>Marcar Inyección</span>
            </button>

            <button
              onClick={handleStopExperiment}
              disabled={isSending}
              className="px-4 py-2 rounded-xl text-xs font-bold text-rose-100 bg-rose-600 hover:bg-rose-500 transition-all flex items-center gap-1.5 shadow-lg shadow-rose-900/40"
            >
              <Square className="w-3.5 h-3.5 fill-rose-100" />
              <span>Detener Experimento</span>
            </button>
          </div>
        </div>
      )}

      {/* ============================================================ */}
      {/* MODAL / FORMULARIO INLINE: CREAR NUEVO EXPERIMENTO            */}
      {/* ============================================================ */}
      {showNewExpModal && (
        <div className="p-5 rounded-2xl border border-cyan-700/60 bg-slate-950/90 shadow-2xl animate-in fade-in zoom-in-95 duration-200 space-y-4">
          <div className="flex items-center justify-between border-b border-slate-800 pb-3">
            <div className="flex items-center gap-2 text-cyan-300 font-bold text-sm">
              <Sparkles className="w-4 h-4 text-cyan-400" />
              <span>Configuración del Nuevo Experimento</span>
            </div>
            <span className="text-xs text-slate-400">Genera archivo CSV aislado en memoria SD</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* Nombre del Experimento */}
            <div className="md:col-span-2 space-y-1">
              <label className="text-xs font-semibold text-slate-300">Nombre / Identificador del Ensayo:</label>
              <input
                type="text"
                value={expName}
                onChange={(e) => {
                  setExpName(e.target.value);
                  // Auto-sugerir nombre de archivo 8.3 FAT
                  const safeName = e.target.value.replace(/[^a-zA-Z0-9]/g, '').slice(0, 6).toUpperCase();
                  if (safeName) setExpFilename(`${safeName}.CSV`);
                }}
                className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-cyan-500 font-medium"
                placeholder="Ej: Ensayo Reoxigenación ODrive 01"
              />
            </div>

            {/* Nombre de archivo CSV en SD */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-300">Archivo en SD (Formato 8.3 FAT):</label>
              <input
                type="text"
                value={expFilename}
                onChange={(e) => setExpFilename(e.target.value.toUpperCase().slice(0, 12))}
                className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-cyan-300 font-mono focus:outline-none focus:border-cyan-500"
                placeholder="EXP_01.CSV"
              />
            </div>
            
            {/* Caso Experimental (Protocolos V4) */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-300">Protocolo Experimental:</label>
              <select
                value={expCaseType}
                onChange={(e) => {
                  const value = e.target.value as 'planta_1_deox' | 'planta_2_step' | 'closed_loop';
                  setExpCaseType(value);
                  if (value === 'planta_1_deox') { setExpPlantTarget('planta_1'); setExpControllerType('none'); setExpMode('manual'); }
                  else if (value === 'planta_2_step') { setExpPlantTarget('planta_2'); setExpControllerType('none'); setExpMode('manual'); }
                  else { setExpPlantTarget('both'); setExpControllerType('pid'); setExpMode('pid'); }
                }}
                className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-300 focus:outline-none focus:border-cyan-500"
              >
                <option value="planta_1_deox">Planta 1 · Desoxigenación química (caída de OD)</option>
                <option value="planta_2_step">Planta 2 · Respuesta al escalón (KLa)</option>
                <option value="closed_loop">Caso B · Evaluación de lazo cerrado (PID)</option>
              </select>
            </div>

            {/* Parámetros según el protocolo */}
            {(expCaseType === 'closed_loop' || expCaseType === 'planta_2_step') && (
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label className="text-xs font-semibold text-slate-300">Setpoint OD (mg/L):</label>
                  <input
                    type="number" step="0.1" min="0" max="14"
                    value={expSetpoint}
                    disabled={expCaseType !== 'closed_loop'}
                    onChange={(e) => setExpSetpoint(Number(e.target.value))}
                    className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-cyan-300 font-mono focus:outline-none focus:border-cyan-500 disabled:opacity-40"
                  />
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-semibold text-slate-300">Escalón (% throttle):</label>
                  <input
                    type="number" step="5" min="0" max="100"
                    value={expStepThrottle}
                    disabled={expCaseType !== 'planta_2_step'}
                    onChange={(e) => setExpStepThrottle(Number(e.target.value))}
                    className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-cyan-300 font-mono focus:outline-none focus:border-cyan-500 disabled:opacity-40"
                  />
                </div>
              </div>
            )}

            {/* Target Planta */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-300">Planta / Sistema Objetivo:</label>
              <select
                value={expPlantTarget}
                onChange={(e) => setExpPlantTarget(e.target.value as any)}
                className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-300 focus:outline-none focus:border-cyan-500"
              >
                <option value="planta_1">Planta 1 (Bomba Dosificadora)</option>
                <option value="planta_2">Planta 2 (Aireador ODrive)</option>
                <option value="both">Ambas Plantas / Lazo Cerrado</option>
              </select>
            </div>

            {/* Modalidad */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-300">Modo de Operación:</label>
              <div className="flex bg-slate-900 rounded-xl border border-slate-700 p-1">
                <button
                  onClick={() => setExpMode('manual')}
                  className={`flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors ${expMode === 'manual' ? 'bg-cyan-500 text-cyan-950' : 'text-slate-400 hover:text-white'}`}
                >
                  Manual
                </button>
                <button
                  onClick={() => setExpMode('pid')}
                  disabled={expPlantTarget === 'planta_1'}
                  className={`flex-1 py-1 text-[11px] font-bold rounded-lg transition-colors disabled:opacity-30 disabled:cursor-not-allowed ${expMode === 'pid' ? 'bg-cyan-500 text-cyan-950' : 'text-slate-400 hover:text-white'}`}
                >
                  PID Auto
                </button>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-center">
            {/* Frecuencia de Muestreo */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <span className="text-slate-300 font-semibold">Tasa de Muestreo:</span>
                <span className="text-cyan-300 font-mono font-bold">{expRate} segundos</span>
              </div>
              <div className="flex items-center gap-1.5">
                {[1, 2, 5, 10, 15, 30].map(val => (
                  <button
                    key={val}
                    type="button"
                    onClick={() => setExpRate(val)}
                    className={`flex-1 py-1 rounded text-[11px] font-mono transition-colors ${
                      expRate === val
                        ? 'bg-cyan-500 text-cyan-950 font-bold'
                        : 'bg-slate-800 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {val}s
                  </button>
                ))}
              </div>
            </div>

            {/* Descripción */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-300">Objetivo / Observaciones:</label>
              <input
                type="text"
                value={expDesc}
                onChange={(e) => setExpDesc(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-300 focus:outline-none focus:border-cyan-500"
                placeholder="Ej: Registro continuo con motor a 65%..."
              />
            </div>

            {/* Botón de Lanzamiento */}
            <div className="flex items-end pt-5">
              <button
                onClick={handleStartExperiment}
                disabled={isSending}
                className="w-full py-2.5 px-4 rounded-xl font-bold text-xs text-cyan-950 bg-gradient-to-r from-cyan-400 to-teal-400 hover:from-cyan-300 hover:to-teal-300 transition-all flex items-center justify-center gap-2 shadow-lg shadow-cyan-900/40"
              >
                <Play className="w-3.5 h-3.5 fill-cyan-950" />
                <span>Lanzar Experimento & Pasar a Vista en Vivo</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Cuadrícula de Funcionalidades Principales */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        
        {/* ============================================================ */}
        {/* TARJETA 1: Modo Monitor (Start / Stop y Sampling Rate)        */}
        {/* ============================================================ */}
        <div className="p-4 rounded-xl border border-slate-800 bg-slate-950/40 flex flex-col justify-between gap-4">
          <div>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-cyan-400 flex items-center gap-1.5">
                <Activity className="w-3.5 h-3.5" /> Modo Monitor
              </span>
              <span className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded-full ${
                isMonitorActive ? 'bg-cyan-500/20 text-cyan-300' : 'bg-slate-800 text-slate-400'
              }`}>
                {isMonitorActive ? 'Activo' : 'Inactivo'}
              </span>
            </div>
            <p className="text-[11px] text-slate-400 mt-1">
              Mantiene la boya despierta para muestreo y visualización continua en pantalla OLED y gráficos.
            </p>
          </div>

          <div className="space-y-3">
            {/* Intervalo / Sampling Rate */}
            <div>
              <div className="flex items-center justify-between text-xs mb-1">
                <span className="text-slate-300">Frecuencia de Muestreo:</span>
                <strong className="text-cyan-300 font-mono">{monitorIntervalSec} seg</strong>
              </div>
              <input
                type="range"
                min="1"
                max="60"
                step="1"
                value={monitorIntervalSec}
                onChange={(e) => handleUpdateSamplingRate(Number(e.target.value))}
                className="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-cyan-400"
              />
              <div className="flex items-center justify-between gap-1 mt-1.5">
                {intervalPresets.map((val) => (
                  <button
                    key={val}
                    onClick={() => handleUpdateSamplingRate(val)}
                    className={`px-2 py-0.5 rounded text-[10px] font-mono transition-colors ${
                      monitorIntervalSec === val
                        ? 'bg-cyan-500/30 text-cyan-300 border border-cyan-500/50'
                        : 'bg-slate-800/80 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {val}s
                  </button>
                ))}
              </div>
            </div>

            {/* Botones Iniciar / Detener */}
            <div className="pt-1">
              {isMonitorActive ? (
                <button
                  onClick={() => handleToggleMonitor(false)}
                  disabled={isSending}
                  className="w-full py-2 px-3 rounded-lg font-semibold text-xs text-rose-200 bg-rose-950/80 hover:bg-rose-900 border border-rose-800 transition-all flex items-center justify-center gap-2 shadow-sm"
                >
                  <Square className="w-3.5 h-3.5 fill-rose-300" />
                  <span>Detener Muestreo Continuo</span>
                </button>
              ) : (
                <button
                  onClick={() => handleToggleMonitor(true)}
                  disabled={isSending}
                  className="w-full py-2 px-3 rounded-lg font-semibold text-xs text-cyan-950 bg-gradient-to-r from-cyan-400 to-teal-400 hover:from-cyan-300 hover:to-teal-300 transition-all flex items-center justify-center gap-2 shadow-md shadow-cyan-500/20"
                >
                  <Play className="w-3.5 h-3.5 fill-cyan-950" />
                  <span>Iniciar Modo Monitor ({monitorIntervalSec}s)</span>
                </button>
              )}
            </div>
          </div>
        </div>

        {/* ============================================================ */}
        {/* TARJETA 2: Muestreo Inmediato (Manual Sample)                 */}
        {/* ============================================================ */}
        <div className="p-4 rounded-xl border border-slate-800 bg-slate-950/40 flex flex-col justify-between gap-4">
          <div>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-amber-400 flex items-center gap-1.5">
                <Zap className="w-3.5 h-3.5" /> Muestreo Manual
              </span>
              <span className="text-[10px] bg-slate-800 text-slate-400 px-2 py-0.5 rounded-full font-mono">
                Puntual
              </span>
            </div>
            <p className="text-[11px] text-slate-400 mt-1">
              Fuerza una lectura inmediata del sensor DIGISENS, registrándola en EEPROM/SD y publicándola a Supabase.
            </p>
          </div>

          <div className="space-y-2">
            <div className="p-2.5 rounded-lg bg-slate-900/80 border border-slate-800 text-[11px] text-slate-400 flex items-start gap-2">
              <Info className="w-4 h-4 text-amber-400/80 flex-shrink-0 mt-0.5" />
              <span>Ideal para verificar el estado del agua sin alterar el ciclo de bajo consumo programado.</span>
            </div>

            <button
              onClick={handleManualSample}
              disabled={isSending}
              className="w-full py-2.5 px-3 rounded-lg font-semibold text-xs text-amber-200 bg-amber-950/80 hover:bg-amber-900 border border-amber-800/80 transition-all flex items-center justify-center gap-2 shadow-sm"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${activeAction === 'manual_sample' ? 'animate-spin' : ''}`} />
              <span>{activeAction === 'manual_sample' ? 'Muestreando...' : 'Tomar Muestra Inmediata'}</span>
            </button>
          </div>
        </div>

        {/* ============================================================ */}
        {/* TARJETA 3: Ciclos de Medición Programada (Sleep Cycles)       */}
        {/* ============================================================ */}
        <div className="p-4 rounded-xl border border-slate-800 bg-slate-950/40 flex flex-col justify-between gap-4">
          <div>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-emerald-400 flex items-center gap-1.5">
                <Timer className="w-3.5 h-3.5" /> Ciclo de Sueño
              </span>
              <span className="text-[10px] bg-emerald-950/80 text-emerald-300 px-2 py-0.5 rounded-full font-mono">
                {sleepCycleMin} min
              </span>
            </div>
            <p className="text-[11px] text-slate-400 mt-1">
              Intervalo autónomo en el que el ESP32 despierta, toma muestra, sincroniza telemetría y vuelve a dormir.
            </p>
          </div>

          <div className="space-y-3">
            <div>
              <div className="flex items-center justify-between text-xs mb-1">
                <span className="text-slate-300">Despertar cada:</span>
                <strong className="text-emerald-300 font-mono">{sleepCycleMin} minutos</strong>
              </div>
              <div className="grid grid-cols-5 gap-1">
                {sleepCyclePresets.map((min) => (
                  <button
                    key={min}
                    onClick={() => setSleepCycleMin(min)}
                    className={`py-1 rounded text-[11px] font-mono text-center transition-colors ${
                      sleepCycleMin === min
                        ? 'bg-emerald-500/30 text-emerald-300 border border-emerald-500/60 font-bold'
                        : 'bg-slate-800/80 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {min}m
                  </button>
                ))}
              </div>
            </div>

            <button
              onClick={handleApplySleepCycle}
              disabled={isSending}
              className="w-full py-2 px-3 rounded-lg font-semibold text-xs text-emerald-200 bg-emerald-950/80 hover:bg-emerald-900 border border-emerald-800 transition-all flex items-center justify-center gap-2 shadow-sm"
            >
              <Send className="w-3.5 h-3.5" />
              <span>Guardar Ciclo Programado</span>
            </button>
          </div>
        </div>

        {/* ============================================================ */}
        {/* TARJETA 4: Suspensión Directa (Deep Sleep: Timed / Indefinite)*/}
        {/* ============================================================ */}
        <div className="p-4 rounded-xl border border-slate-800 bg-slate-950/40 flex flex-col justify-between gap-4">
          <div>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-indigo-400 flex items-center gap-1.5">
                <Moon className="w-3.5 h-3.5" /> Suspender Sensor
              </span>
              <span className="text-[10px] bg-slate-800 text-slate-400 px-2 py-0.5 rounded-full font-mono">
                Bajo Consumo
              </span>
            </div>
            <p className="text-[11px] text-slate-400 mt-1">
              Fuerza la transición a sueño profundo para conservar batería o trasladar la boya.
            </p>
          </div>

          <div className="space-y-2.5">
            {/* Toggle indefinido vs temporizado */}
            <div className="flex items-center justify-between p-2 rounded-lg bg-slate-900/90 border border-slate-800 text-xs">
              <span className="text-slate-300 font-medium">Sueño Indefinido:</span>
              <button
                type="button"
                onClick={() => {
                  setIsIndefiniteSleep(!isIndefiniteSleep);
                  setShowIndefiniteConfirm(false);
                }}
                className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out ${
                  isIndefiniteSleep ? 'bg-indigo-600' : 'bg-slate-700'
                }`}
              >
                <span
                  className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                    isIndefiniteSleep ? 'translate-x-4' : 'translate-x-0'
                  }`}
                />
              </button>
            </div>

            {/* Selector de minutos si no es indefinido */}
            {!isIndefiniteSleep ? (
              <div className="grid grid-cols-4 gap-1">
                {timedSleepPresets.map((m) => (
                  <button
                    key={m}
                    onClick={() => setSleepDurationMin(m)}
                    className={`py-1 rounded text-[10px] font-mono text-center transition-colors ${
                      sleepDurationMin === m
                        ? 'bg-indigo-500/30 text-indigo-300 border border-indigo-500/60 font-bold'
                        : 'bg-slate-800/80 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {m}m
                  </button>
                ))}
              </div>
            ) : (
              <div className="p-2 rounded bg-indigo-950/60 border border-indigo-900/60 text-[10px] text-indigo-300 flex items-center gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5 text-indigo-400 flex-shrink-0" />
                <span>Requiere botón físico RST en la boya para despertar.</span>
              </div>
            )}

            {/* Confirmación modal para sueño indefinido */}
            {showIndefiniteConfirm ? (
              <div className="space-y-1.5 pt-1">
                <p className="text-[10px] text-amber-300 font-bold text-center">
                  ¿Confirmar apagado total de temporizadores?
                </p>
                <div className="grid grid-cols-2 gap-1.5">
                  <button
                    onClick={handleTriggerSleep}
                    className="py-1.5 px-2 rounded bg-rose-600 hover:bg-rose-500 text-white font-bold text-[11px]"
                  >
                    Sí, Suspender
                  </button>
                  <button
                    onClick={() => setShowIndefiniteConfirm(false)}
                    className="py-1.5 px-2 rounded bg-slate-800 text-slate-300 text-[11px]"
                  >
                    Cancelar
                  </button>
                </div>
              </div>
            ) : (
              <button
                onClick={handleTriggerSleep}
                disabled={isSending}
                className="w-full py-2 px-3 rounded-lg font-semibold text-xs text-indigo-200 bg-indigo-950/80 hover:bg-indigo-900 border border-indigo-800 transition-all flex items-center justify-center gap-2 shadow-sm"
              >
                <PowerOff className="w-3.5 h-3.5" />
                <span>
                  {isIndefiniteSleep ? 'Dormir Indefinidamente' : `Suspender por ${sleepDurationMin} min`}
                </span>
              </button>
            )}
          </div>
        </div>

      </div>

      {/* ============================================================ */}
      {/* SECCIÓN HISTORIAL DE EXPERIMENTOS (DESPLEGABLE / TABLA)      */}
      {/* ============================================================ */}
      <div className="border-t border-slate-800/80 pt-3">
        <button
          onClick={() => setShowExpHistory(!showExpHistory)}
          className="flex items-center justify-between w-full text-xs font-semibold text-slate-400 hover:text-slate-200 transition-colors py-1"
        >
          <span className="flex items-center gap-2">
            <FileSpreadsheet className="w-4 h-4 text-cyan-400" />
            <span>Historial de Ensayos y Descarga de Archivos CSV ({experiments.length})</span>
          </span>
          {showExpHistory ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </button>

        {showExpHistory && (
          <div className="mt-3 space-y-2 animate-in fade-in duration-150">
            {experiments.length === 0 ? (
              <p className="text-xs text-slate-500 py-3 text-center">No hay experimentos registrados aún.</p>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2.5">
                {experiments.map((exp) => (
                  <div
                    key={exp.id}
                    className="p-3 rounded-xl border border-slate-800 bg-slate-950/60 flex flex-col justify-between gap-2 text-xs"
                  >
                    <div>
                      <div className="flex items-center justify-between">
                        <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded ${
                          exp.status === 'active'
                            ? 'bg-cyan-500/20 text-cyan-300'
                            : 'bg-slate-800 text-slate-400'
                        }`}>
                          {exp.status === 'active' ? 'En Curso' : 'Completado'}
                        </span>
                        <span className="text-[10px] text-slate-400 font-mono">
                          {new Date(exp.started_at).toLocaleDateString()}
                        </span>
                      </div>
                      <h5 className="font-bold text-slate-200 mt-1.5 leading-snug line-clamp-1">{exp.name}</h5>
                      <p className="text-[11px] text-slate-400 font-mono mt-0.5">
                        Archivo: <strong className="text-cyan-400">{exp.csv_filename}</strong> ({exp.sampling_rate_sec}s)
                      </p>
                    </div>

                    <div className="flex items-center justify-between pt-2 border-t border-slate-800/80">
                      <span className="text-[10px] text-slate-500">
                        {exp.total_samples > 0 ? `${exp.total_samples} muestras` : 'Datos listos'}
                      </span>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => handleDownloadCsv(exp.id, exp.csv_filename)}
                          className="flex items-center gap-1 text-[11px] font-semibold text-cyan-400 hover:text-cyan-300 transition-colors"
                          title="Descargar mediciones CSV"
                        >
                          <Download className="w-3 h-3" />
                          <span>CSV</span>
                        </button>
                        <button
                          onClick={() => handleDeleteExperiment(exp.id, exp.name)}
                          className="flex items-center gap-1 text-[11px] font-semibold text-rose-400/70 hover:text-rose-300 hover:bg-rose-950/40 p-1 rounded transition-colors"
                          title="Eliminar este experimento y sus mediciones"
                        >
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
