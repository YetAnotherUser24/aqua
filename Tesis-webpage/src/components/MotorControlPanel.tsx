'use client';

import React, { useState, useEffect } from 'react';
import { Power, Send, Gauge, AlertCircle, CheckCircle2, ShieldAlert, Cpu, Sliders, Zap, OctagonAlert, RotateCcw } from 'lucide-react';
import { Device, MotorTelemetry } from '@/types';

interface MotorControlPanelProps {
  motorDevice: Device | null;
  currentTelemetry: MotorTelemetry | null;
  onCommandSent: () => void;
}

type ControlMode = 'pid' | 'fuzzy' | 'manual';

export const MotorControlPanel: React.FC<MotorControlPanelProps> = ({
  motorDevice,
  currentTelemetry,
  onCommandSent
}) => {
  const isDeviceOnline = motorDevice?.status === 'online';
  
  // Modos de control: 'pid' | 'fuzzy' | 'manual'
  const [controlMode, setControlMode] = useState<ControlMode>('pid');
  
  // Parámetros de consigna y sintonización
  const [targetDo, setTargetDo] = useState<number>(7.5);
  const [kp, setKp] = useState<number>(100.0);
  const [ki, setKi] = useState<number>(1.5);
  const [kd, setKd] = useState<number>(10.0);

  // Estado del modo manual (consigna del operador independiente de telemetria)
  const [isOn, setIsOn] = useState<boolean>(false);
  const [targetSpeed, setTargetSpeed] = useState<number>(25);
  const [isSending, setIsSending] = useState<boolean>(false);
  type FeedbackState = { type: 'success' | 'error' | null; message: string };
  const [feedback, setFeedback] = useState<FeedbackState>({
    type: null,
    message: ''
  });

  const feedbackTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
    };
  }, []);

  // Consigna permanece bajo control del operador (sin reseteo automatico por parpadeos de red)

  // Cálculo de RPM de consigna para comando (0 a 600 RPM)
  const commandRpm = !isOn ? 0 : Math.round((targetSpeed / 100) * 600);

  // Parada de Emergencia Inmediata
  const handleEmergencyStop = async () => {
    if (!isDeviceOnline) return;
    setIsSending(true);
    setFeedback({ type: null, message: '' });

    try {
      const res = await fetch('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: motorDevice?.id,
          command_type: 'emergency_stop',
          speed_percent: 0,
          payload: { action: 'emergency_stop' },
          requested_by: 'PARADA DE EMERGENCIA (Web)'
        })
      });

      if (!res.ok) throw new Error('Error al ejecutar parada de emergencia');

      setIsOn(false);
      setTargetSpeed(0);
      setControlMode('manual');
      setFeedback({
        type: 'error',
        message: '¡PARADA DE EMERGENCIA ENVIADA! El motor ha sido desarmado a 0 RPM.'
      });
      onCommandSent();
    } catch (err: any) {
      setFeedback({ type: 'error', message: err.message || 'Error de conexión' });
    } finally {
      setIsSending(false);
    }
  };

  /**
   * Reanudar tras una parada de emergencia.
   *
   * El E-Stop del firmware es un latch: `_emergencyStopActive` deja a `computeOutputRpm()` en 0
   * para siempre y `configure()` (usado por `set_mode`) NO lo limpia. Hasta ahora
   * `ControlEngine::clearEmergencyStop()` no se invocaba desde ningún sitio y la web no tenía
   * botón, así que un ODrive que hubiera latcheado un E-Stop nunca volvía a moverse.
   */
  const handleResumeFromEstop = async () => {
    if (!isDeviceOnline) return;
    setIsSending(true);
    setFeedback({ type: null, message: '' });

    try {
      const res = await fetch('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: motorDevice?.id,
          // `set_speed` es el único tipo admitido por el CHECK de `control_commands`;
          // la intención real viaja en payload.action.
          command_type: 'set_speed',
          speed_percent: 0,
          payload: { action: 'clear_estop' },
          requested_by: 'Reanudar tras E-Stop (Web)'
        })
      });

      if (!res.ok) throw new Error('Error al reanudar el controlador');

      setFeedback({
        type: 'success',
        message: 'E-Stop liberado. El motor aceptará consignas de nuevo.'
      });
      onCommandSent();
    } catch (err: any) {
      setFeedback({ type: 'error', message: err.message || 'Error de conexión' });
    } finally {
      setIsSending(false);
    }
  };

  const handleApplyChanges = async () => {
    if (!isDeviceOnline) {
      setFeedback({
        type: 'error',
        message: 'No se puede enviar comandos: el ESP32 del motor está desconectado.'
      });
      return;
    }

    setIsSending(true);
    setFeedback({ type: null, message: '' });

    try {
      let commandType = 'set_speed';
      let speed = targetSpeed;
      let payload: any = {};

      if (controlMode === 'manual') {
        const active = isOn || targetSpeed > 0;
        if (targetSpeed > 0 && !isOn) setIsOn(true);
        commandType = active && targetSpeed > 0 ? 'set_speed' : 'stop';
        speed = active ? targetSpeed : 0;
        payload = { mode: 'manual', manual_throttle_pct: speed };
      } else {
        commandType = 'set_mode';
        payload = {
          mode: controlMode,
          target_do: targetDo,
          kp,
          ki,
          kd
        };
      }

      const res = await fetch('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: motorDevice?.id,
          command_type: commandType,
          speed_percent: speed,
          payload,
          requested_by: `Operador AquaControl (${controlMode.toUpperCase()})`
        })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Error al enviar comando');

      setFeedback({
        type: 'success',
        message: controlMode === 'manual'
          ? `¡Comando transmitido! Modo MANUAL: ${speed}% (${commandRpm} RPM)`
          : `¡Modo ${controlMode.toUpperCase()} activo! Consigna OD: ${targetDo} mg/L | Algoritmo local gobernando motor.`
      });

      onCommandSent();

      if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
      feedbackTimerRef.current = setTimeout(() => {
        setFeedback((prev: FeedbackState) => (prev.type === 'success' ? { type: null, message: '' } : prev));
      }, 5000);

    } catch (err: any) {
      setFeedback({
        type: 'error',
        message: err.message || 'Error de conexión con el servidor'
      });
    } finally {
      setIsSending(false);
    }
  };

  return (
    <div className="glass-panel rounded-2xl p-6 border border-cyan-900/40 relative overflow-hidden shadow-2xl">
      {/* Glow background accent */}
      <div className="absolute -top-16 -left-16 w-56 h-56 bg-cyan-600/10 rounded-full blur-3xl pointer-events-none" />
      <div className="absolute -bottom-16 -right-16 w-56 h-56 bg-blue-600/10 rounded-full blur-3xl pointer-events-none" />

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between border-b border-slate-800/80 pb-4 mb-5 gap-3">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-cyan-950/80 border border-cyan-800/60 shadow-inner">
            <Cpu className="w-5 h-5 text-cyan-400" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-base font-bold text-white tracking-wide">
                Control Principal de Aireación (ODrive S1 — M8325s)
              </h3>
              {isDeviceOnline ? (
                <span className="text-[10px] font-semibold px-2.5 py-0.5 rounded-full bg-emerald-950 text-emerald-400 border border-emerald-800">
                  ESP32 Online
                </span>
              ) : (
                <span className="text-[10px] font-semibold px-2.5 py-0.5 rounded-full bg-rose-950 text-rose-400 border border-rose-800">
                  ESP32 Offline
                </span>
              )}
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Lazo cerrado autónomo (Core 1) con telemetría en tiempo real a Supabase (Core 0)
            </p>
          </div>
        </div>

        {/* Emergency Stop, Resume & Status */}
        <div className="flex items-center gap-2">
          <button
            onClick={handleEmergencyStop}
            disabled={!isDeviceOnline || isSending}
            className="flex items-center gap-2 px-4 py-2 bg-gradient-to-r from-red-600 to-rose-700 hover:from-red-500 hover:to-rose-600 text-white font-bold text-xs rounded-xl shadow-lg shadow-red-950/50 border border-red-400/30 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <OctagonAlert className="w-4 h-4 text-white animate-pulse" />
            <span>PARADA DE EMERGENCIA</span>
          </button>
          <button
            onClick={handleResumeFromEstop}
            disabled={!isDeviceOnline || isSending}
            title="Libera el latch de emergencia del ODrive para volver a aceptar consignas"
            className="flex items-center gap-2 px-3 py-2 bg-slate-800 hover:bg-slate-700 text-emerald-300 font-bold text-xs rounded-xl border border-emerald-800/60 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <RotateCcw className="w-4 h-4" />
            <span>REANUDAR</span>
          </button>
        </div>
      </div>

      {/* Selector de Modo de Control (Tabs) */}
      <div className="flex items-center gap-2 p-1.5 bg-slate-900/90 border border-slate-800 rounded-xl mb-6 max-w-md">
        <button
          onClick={() => setControlMode('pid')}
          className={`flex-1 flex items-center justify-center gap-1.5 py-2 px-3 rounded-lg text-xs font-bold transition-all ${
            controlMode === 'pid'
              ? 'bg-gradient-to-r from-cyan-600 to-blue-600 text-white shadow-md'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Sliders className="w-3.5 h-3.5" />
          <span>PID Automático</span>
        </button>

        <button
          onClick={() => setControlMode('fuzzy')}
          className={`flex-1 flex items-center justify-center gap-1.5 py-2 px-3 rounded-lg text-xs font-bold transition-all ${
            controlMode === 'fuzzy'
              ? 'bg-gradient-to-r from-teal-600 to-emerald-600 text-white shadow-md'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Zap className="w-3.5 h-3.5" />
          <span>Lógica Difusa (Fuzzy)</span>
        </button>

        <button
          onClick={() => setControlMode('manual')}
          className={`flex-1 flex items-center justify-center gap-1.5 py-2 px-3 rounded-lg text-xs font-bold transition-all ${
            controlMode === 'manual'
              ? 'bg-gradient-to-r from-amber-600 to-orange-600 text-white shadow-md'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Gauge className="w-3.5 h-3.5" />
          <span>Manual Override</span>
        </button>
      </div>

      {/* Contenido Dinámico según Modo */}
      {controlMode === 'manual' ? (
        /* MODO MANUAL (SLIDERS DIRECTOS) */
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-center bg-slate-900/50 p-4 rounded-xl border border-slate-800/60">
          <div className="flex flex-col gap-2">
            <label className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
              Interruptor de Mando Directo
            </label>
            <button
              onClick={() => setIsOn(prev => !prev)}
              disabled={!isDeviceOnline || isSending}
              className={`flex items-center justify-center gap-3 py-3 px-5 rounded-xl font-bold transition-all border shadow-lg ${
                !isDeviceOnline
                  ? 'bg-slate-800/50 text-slate-500 border-slate-700 cursor-not-allowed'
                  : isOn
                  ? 'bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white border-emerald-400/50 shadow-emerald-900/30 active:scale-[0.98]'
                  : 'bg-slate-800 hover:bg-slate-700 text-slate-300 border-slate-700 active:scale-[0.98]'
              }`}
            >
              <Power className={`w-5 h-5 ${isOn ? 'text-white' : 'text-slate-400'}`} />
              <span>{isOn ? 'MOTOR HABILITADO' : 'MOTOR DETENIDO'}</span>
            </button>
            <span className="text-[11px] text-slate-500 text-center">
              {isOn ? 'Control de velocidad manual activo' : 'ODrive S1 en reposo (0 RPM)'}
            </span>
          </div>

          <div className="flex flex-col gap-2">
            <div className="flex justify-between items-center">
              <label className="text-xs font-semibold text-slate-300 uppercase tracking-wider flex items-center gap-1">
                <Gauge className="w-3.5 h-3.5 text-amber-400" />
                <span>Acelerador Manual:</span>
              </label>
              <span className="text-sm font-bold text-amber-300 bg-amber-950/80 px-2 py-0.5 rounded border border-amber-800/60">
                {isOn ? `${targetSpeed}% (${commandRpm} RPM)` : '0% (Inactivo)'}
              </span>
            </div>

            <div className="flex items-center gap-3">
              <span className="text-xs text-slate-500 font-bold">0%</span>
              <input
                type="range"
                min="0"
                max="100"
                step="5"
                value={targetSpeed}
                disabled={!isDeviceOnline || isSending}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
                  const val = Number(e.target.value);
                  setTargetSpeed(val);
                  if (val > 0 && !isOn) setIsOn(true);
                }}
                className="w-full h-2 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-amber-400 disabled:opacity-40 disabled:cursor-not-allowed"
              />
              <span className="text-xs text-slate-500 font-bold">100%</span>
            </div>

            <div className="flex justify-between text-[11px] text-slate-400">
              <span>Rango FOC: <strong className="text-slate-400">0 – 600 RPM</strong></span>
              <span>Consigna a Enviar: <strong className="text-amber-400 font-bold">{commandRpm} RPM ({isOn ? targetSpeed : 0}%)</strong></span>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <label className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
              Transmitir Orden
            </label>
            <button
              onClick={handleApplyChanges}
              disabled={!isDeviceOnline || isSending}
              className="flex items-center justify-center gap-2 py-3 px-5 rounded-xl font-bold bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 text-white border border-amber-400/40 shadow-lg shadow-amber-900/30 transition-all active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Send className={`w-4 h-4 ${isSending ? 'animate-spin' : ''}`} />
              <span>{isSending ? 'Transmitiendo...' : 'Aplicar Mando Manual'}</span>
            </button>
            <span className="text-[11px] text-slate-500 text-center">
              Sobrescribe el control autónomo con aceleración fija
            </span>
          </div>
        </div>
      ) : (
        /* MODOS AUTÓNOMOS (PID / FUZZY) */
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 bg-slate-900/50 p-4 rounded-xl border border-slate-800/60">
          {/* Consigna de Oxígeno Disuelto */}
          <div className="flex flex-col gap-2">
            <div className="flex justify-between items-center">
              <label className="text-xs font-semibold text-cyan-300 uppercase tracking-wider">
                Consigna Objetivo (Setpoint)
              </label>
              <span className="text-sm font-bold text-cyan-400 bg-cyan-950/80 px-2 py-0.5 rounded border border-cyan-800/60">
                {targetDo.toFixed(1)} mg/L
              </span>
            </div>
            <input
              type="range"
              min="4.0"
              max="10.0"
              step="0.1"
              value={targetDo}
              disabled={!isDeviceOnline || isSending}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTargetDo(Number(e.target.value))}
              className="w-full h-2 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-cyan-400 disabled:opacity-40"
            />
            <div className="flex justify-between text-[11px] text-slate-400">
              <span>Mínimo: 4.0 mg/L</span>
              <span>Óptimo Acuicultura: 6.5–8.0 mg/L</span>
              <span>Máx: 10.0 mg/L</span>
            </div>
          </div>

          {/* Ajuste de Ganancias PID */}
          <div className="flex flex-col gap-2">
            <label className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
              {controlMode === 'pid' ? 'Ganancias PID (Kp, Ki, Kd)' : 'Perfil Difuso (Fuzzy Rules)'}
            </label>
            {controlMode === 'pid' ? (
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <span className="text-[10px] text-slate-400">Kp:</span>
                  <input
                    type="number"
                    value={kp}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setKp(Number(e.target.value))}
                    className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-white"
                  />
                </div>
                <div>
                  <span className="text-[10px] text-slate-400">Ki:</span>
                  <input
                    type="number"
                    step="0.1"
                    value={ki}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setKi(Number(e.target.value))}
                    className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-white"
                  />
                </div>
                <div>
                  <span className="text-[10px] text-slate-400">Kd:</span>
                  <input
                    type="number"
                    value={kd}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setKd(Number(e.target.value))}
                    className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-white"
                  />
                </div>
              </div>
            ) : (
              <div className="text-xs text-slate-300 bg-slate-800/80 p-2 rounded border border-slate-700">
                Inferencia Sugeno de 4 conjuntos: Error OD $\times$ Tasa de variación $\to$ RPM
              </div>
            )}
            <span className="text-[11px] text-slate-500">
              {controlMode === 'pid' ? 'Anti-windup e integración activa en Core 1' : 'Control adaptativo sin sobreimpulso'}
            </span>
          </div>

          {/* Confirmar Mando Autónomo */}
          <div className="flex flex-col gap-2">
            <label className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
              Activar Modo Autónomo
            </label>
            <button
              onClick={handleApplyChanges}
              disabled={!isDeviceOnline || isSending}
              className="flex items-center justify-center gap-2 py-3 px-5 rounded-xl font-bold bg-gradient-to-r from-cyan-600 via-blue-600 to-teal-600 hover:from-cyan-500 hover:to-teal-500 text-white border border-cyan-400/40 shadow-lg shadow-cyan-900/30 transition-all active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Send className={`w-4 h-4 ${isSending ? 'animate-spin' : ''}`} />
              <span>{isSending ? 'Transmitiendo...' : `Iniciar Lazo ${controlMode.toUpperCase()}`}</span>
            </button>
            <span className="text-[11px] text-slate-500 text-center">
              Transmite consigna y actualiza NVS Flash en el ESP32
            </span>
          </div>
        </div>
      )}

      {/* Banner de Feedback */}
      {feedback.type && (
        <div className={`mt-4 p-3 rounded-xl flex items-center gap-2.5 text-xs font-medium border ${
          feedback.type === 'success'
            ? 'bg-emerald-950/80 text-emerald-300 border-emerald-800/80'
            : 'bg-rose-950/80 text-rose-300 border-rose-800/80'
        }`}>
          {feedback.type === 'success' ? (
            <CheckCircle2 className="w-4 h-4 text-emerald-400 flex-shrink-0" />
          ) : (
            <AlertCircle className="w-4 h-4 text-rose-400 flex-shrink-0" />
          )}
          <span>{feedback.message}</span>
        </div>
      )}
    </div>
  );
};
