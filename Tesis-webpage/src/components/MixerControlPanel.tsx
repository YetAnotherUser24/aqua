'use client';

import React, { useState, useEffect } from 'react';
import { Power, Send, Gauge, OctagonAlert, Fan, ShieldAlert } from 'lucide-react';
import { Device, MotorTelemetry } from '@/types';

interface MixerControlPanelProps {
  mixerDevice: Device | null;
  currentTelemetry: MotorTelemetry | null;
  onCommandSent: () => void;
}

export const MixerControlPanel: React.FC<MixerControlPanelProps> = ({
  mixerDevice,
  currentTelemetry,
  onCommandSent
}) => {
  const isDeviceOnline = mixerDevice?.status === 'online';
  const targetDeviceId = mixerDevice?.id || 'c0000000-0000-0000-0000-000000000003';

  const [isOn, setIsOn] = useState<boolean>(currentTelemetry?.is_on ?? false);
  const [targetRpm, setTargetRpm] = useState<number>(currentTelemetry?.rpm ?? 0);
  const [isSending, setIsSending] = useState<boolean>(false);
  const [feedback, setFeedback] = useState<{ type: 'success' | 'error' | null; message: string }>({
    type: null,
    message: ''
  });

  const feedbackTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
    };
  }, []);

  // Parada inmediata
  const handleQuickStop = async () => {
    if (!isDeviceOnline) return;
    setIsSending(true);
    setFeedback({ type: null, message: '' });

    try {
      const res = await fetch('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: targetDeviceId,
          command_type: 'stop',
          speed_percent: 0,
          payload: { action: 'quick_stop', target_rpm: 0, target_rad_s: 0 },
          requested_by: 'Parada Manual T-200 (Web)'
        })
      });

      if (!res.ok) throw new Error('Error enviando orden de parada al mezclador');

      setIsOn(false);
      setTargetRpm(0);
      setFeedback({
        type: 'error',
        message: 'T-200 DETENIDO (0 RPM).'
      });
      onCommandSent();
    } catch (err: any) {
      setFeedback({ type: 'error', message: err.message || 'Error de comunicación' });
    } finally {
      setIsSending(false);
    }
  };

  const handleApplyChanges = async () => {
    if (!isDeviceOnline) {
      setFeedback({
        type: 'error',
        message: 'El controlador del mezclador está desconectado.'
      });
      return;
    }

    setIsSending(true);
    setFeedback({ type: null, message: '' });

    try {
      const rpmToSend = isOn ? targetRpm : 0;
      const commandType = isOn ? (targetRpm > 0 ? 'set_speed' : 'stop') : 'stop';
      const radsToSend = (rpmToSend * 2 * Math.PI) / 60.0;

      const res = await fetch('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: targetDeviceId,
          command_type: commandType,
          speed_percent: (rpmToSend / 3000) * 100, // opcional
          payload: {
            mode: 'closed_loop',
            target_rpm: rpmToSend,
            target_rad_s: radsToSend
          },
          requested_by: 'Operador Web (Mezclador T-200)'
        })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Error al transmitir comando de RPM');

      setFeedback({
        type: 'success',
        message: isOn
          ? `Mezclador a ${rpmToSend} RPM`
          : 'Mezclador en reposo (0 RPM)'
      });

      onCommandSent();

      if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current);
      feedbackTimerRef.current = setTimeout(() => {
        setFeedback(prev => (prev.type === 'success' ? { type: null, message: '' } : prev));
      }, 4000);

    } catch (err: any) {
      setFeedback({
        type: 'error',
        message: err.message || 'Error de conexión'
      });
    } finally {
      setIsSending(false);
    }
  };

  const currentRpm = currentTelemetry?.rpm ?? 0;

  return (
    <div className="glass-panel rounded-2xl p-5 border border-amber-900/30 relative overflow-hidden shadow-2xl flex flex-col justify-between h-full bg-slate-950/40">
      {/* Glow background accent */}
      <div className="absolute -top-12 -right-12 w-36 h-36 bg-amber-600/10 rounded-full blur-2xl pointer-events-none" />

      <div>
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-3 mb-4">
          <div className="flex items-center gap-2.5">
            <div className="p-2 rounded-lg bg-amber-950/80 border border-amber-800/60 text-amber-400">
              <Fan className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h4 className="text-sm font-bold text-white tracking-wide">
                  Mezclador Principal (T-200 Thruster)
                </h4>
                {isDeviceOnline ? (
                  <span className="text-[9px] font-semibold px-2 py-0.5 rounded-full bg-emerald-950 text-emerald-400 border border-emerald-800">
                    Online
                  </span>
                ) : (
                  <span className="text-[9px] font-semibold px-2 py-0.5 rounded-full bg-rose-950 text-rose-400 border border-rose-800">
                    Offline
                  </span>
                )}
              </div>
              <p className="text-[11px] text-slate-400">
                Driver Closed-Loop (0–3000 RPM)
              </p>
            </div>
          </div>

          <button
            onClick={handleQuickStop}
            disabled={!isDeviceOnline || isSending}
            title="Parada rápida T-200"
            className="p-1.5 bg-red-950/80 hover:bg-red-900 border border-red-800/70 text-red-400 hover:text-white rounded-lg transition-all active:scale-95 disabled:opacity-40"
          >
            <OctagonAlert className="w-4 h-4" />
          </button>
        </div>

        {/* Controles Principales */}
        <div className="space-y-4">
          {/* Switch de Encendido / Desarmado */}
          <div className="flex items-center justify-between bg-slate-900/60 p-2.5 rounded-xl border border-slate-800/60">
            <div className="flex items-center gap-2">
              <Power className={`w-4 h-4 ${isOn ? 'text-emerald-400' : 'text-slate-500'}`} />
              <span className="text-xs font-semibold text-slate-300">
                {isOn ? 'MEZCLADOR ARMADO' : 'MEZCLADOR EN REPOSO'}
              </span>
            </div>
            <button
              onClick={() => setIsOn(prev => !prev)}
              disabled={!isDeviceOnline || isSending}
              className={`px-3 py-1 rounded-lg text-xs font-bold transition-all border ${
                !isDeviceOnline
                  ? 'bg-slate-800 text-slate-500 border-slate-700 cursor-not-allowed'
                  : isOn
                  ? 'bg-emerald-600 hover:bg-emerald-500 text-white border-emerald-400 shadow-md shadow-emerald-950/50'
                  : 'bg-slate-800 hover:bg-slate-700 text-slate-300 border-slate-700'
              }`}
            >
              {isOn ? 'ENCENDIDO' : 'APAGADO'}
            </button>
          </div>

          {/* Slider de Velocidad RPM */}
          <div className="bg-slate-900/60 p-3 rounded-xl border border-slate-800/60 space-y-2">
            <div className="flex justify-between items-center text-xs">
              <label className="font-semibold text-slate-300 flex items-center gap-1.5">
                <Gauge className="w-3.5 h-3.5 text-amber-400" />
                <span>Velocidad (RPM):</span>
              </label>
              <div className="flex items-center gap-2">
                {isOn && (
                   <span className="text-[10px] text-emerald-400">Actual: {currentRpm.toFixed(0)} RPM</span>
                )}
                <span className="font-mono font-bold text-amber-300 bg-amber-950/80 px-2 py-0.5 rounded border border-amber-800/50">
                  {isOn ? `${targetRpm} RPM` : '0 RPM'}
                </span>
              </div>
            </div>

            <input
              type="range"
              min="0"
              max="3000"
              step="50"
              value={targetRpm}
              disabled={!isDeviceOnline || isSending}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
                  let val = Number(e.target.value);
                  // Apply deadband
                  if (val > 0 && val < 500) val = 500;
                  setTargetRpm(val);
              }}
              className="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-amber-400 disabled:opacity-40"
            />

            <div className="flex justify-between text-[10px] text-slate-400">
              <span>Deadband: 500 RPM</span>
              <span>Máx: 3000 RPM</span>
            </div>
          </div>
        </div>
      </div>

      {/* Botón de Envío y Feedback */}
      <div className="mt-4 space-y-2">
        <button
          onClick={handleApplyChanges}
          disabled={!isDeviceOnline || isSending}
          className="w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-xl font-bold text-xs bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 text-white border border-amber-400/30 shadow-md shadow-amber-950/50 transition-all active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Send className={`w-3.5 h-3.5 ${isSending ? 'animate-spin' : ''}`} />
          <span>{isSending ? 'Enviando...' : 'Aplicar Consigna Mezclador'}</span>
        </button>

        {feedback.message && (
          <div
            className={`text-[11px] text-center px-2 py-1 rounded-lg border font-medium ${
              feedback.type === 'success'
                ? 'text-emerald-300 bg-emerald-950/60 border-emerald-800/60'
                : 'text-rose-300 bg-rose-950/60 border-rose-800/60'
            }`}
          >
            {feedback.message}
          </div>
        )}
      </div>
    </div>
  );
};
