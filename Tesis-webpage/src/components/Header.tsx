'use client';

import React from 'react';
import { Activity, Wifi, WifiOff, RefreshCw, Layers, ShieldCheck, Trash2 } from 'lucide-react';
import { Device } from '@/types';

interface HeaderProps {
  sensorDevice: Device | null;
  motorDevice: Device | null;
  escDevice?: Device | null;
  isLive: boolean;
  isLoading: boolean;
  onRefresh: () => void;
  lastUpdatedText: string;
  onOpenClearModal?: () => void;
}

export const Header: React.FC<HeaderProps> = ({
  sensorDevice,
  motorDevice,
  escDevice,
  isLive,
  isLoading,
  onRefresh,
  lastUpdatedText,
  onOpenClearModal
}) => {
  const isSensorOnline = sensorDevice?.status === 'online';
  const isMotorOnline = motorDevice?.status === 'online';
  const isEscOnline = escDevice?.status === 'online';

  return (
    <header className="glass-panel sticky top-0 z-50 border-b border-cyan-900/30 px-4 lg:px-8 py-3.5 flex flex-wrap items-center justify-between gap-4">
      {/* Brand & Logo */}
      <div className="flex items-center gap-3">
        <div className="relative flex items-center justify-center w-10 h-10 rounded-xl bg-gradient-to-tr from-cyan-600 via-teal-500 to-blue-600 shadow-lg shadow-cyan-500/20 ring-1 ring-cyan-400/30">
          <Activity className="w-5 h-5 text-white animate-pulse" />
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-bold tracking-tight bg-gradient-to-r from-white via-cyan-100 to-cyan-400 bg-clip-text text-transparent">
              AquaControl
            </h1>
            <span className="text-[10px] font-semibold tracking-wider uppercase px-2 py-0.5 rounded-full bg-cyan-950/80 text-cyan-400 border border-cyan-800/60">
              Acuicultura v1.0
            </span>
          </div>
          <p className="text-xs text-slate-400 font-medium">
            Supervisión de OD & Control Dual de Aireación (ODrive S1 & T200)
          </p>
        </div>
      </div>

      {/* Dispositivos y Estado de Red */}
      <div className="flex items-center gap-2 sm:gap-4 flex-wrap">
        {/* Sensor Status Pill */}
        <div className="flex items-center gap-2 bg-slate-900/80 px-2.5 py-1.5 rounded-lg border border-slate-800">
          <div className={`w-2 h-2 rounded-full ${isSensorOnline ? 'bg-emerald-400 shadow-[0_0_8px_#34d399]' : 'bg-rose-500'}`} />
          <div className="text-left">
            <div className="text-[11px] font-medium text-slate-300 flex items-center gap-1">
              <span>Sensor OD</span>
              {isSensorOnline ? <Wifi className="w-3 h-3 text-emerald-400" /> : <WifiOff className="w-3 h-3 text-rose-400" />}
            </div>
            <div className="text-[9px] text-slate-500">
              {isSensorOnline ? 'En línea' : 'Desconectado'}
            </div>
          </div>
        </div>

        {/* ODrive Status Pill */}
        <div className="flex items-center gap-2 bg-slate-900/80 px-2.5 py-1.5 rounded-lg border border-slate-800">
          <div className={`w-2 h-2 rounded-full ${isMotorOnline ? 'bg-cyan-400 shadow-[0_0_8px_#22d3ee]' : 'bg-rose-500'}`} />
          <div className="text-left">
            <div className="text-[11px] font-medium text-slate-300 flex items-center gap-1">
              <span>ODrive S1</span>
              {isMotorOnline ? <Wifi className="w-3 h-3 text-cyan-400" /> : <WifiOff className="w-3 h-3 text-rose-400" />}
            </div>
            <div className="text-[9px] text-slate-500">
              {isMotorOnline ? 'En línea' : 'Desconectado'}
            </div>
          </div>
        </div>

        {/* ESC T-200 Status Pill */}
        <div className="flex items-center gap-2 bg-slate-900/80 px-2.5 py-1.5 rounded-lg border border-slate-800">
          <div className={`w-2 h-2 rounded-full ${isEscOnline ? 'bg-amber-400 shadow-[0_0_8px_#fbbf24]' : 'bg-rose-500'}`} />
          <div className="text-left">
            <div className="text-[11px] font-medium text-slate-300 flex items-center gap-1">
              <span>T-200 (ESC)</span>
              {isEscOnline ? <Wifi className="w-3 h-3 text-amber-400" /> : <WifiOff className="w-3 h-3 text-rose-400" />}
            </div>
            <div className="text-[9px] text-slate-500">
              {isEscOnline ? 'En línea' : 'Desconectado'}
            </div>
          </div>
        </div>

        {/* Botones de Acción: Refresco Manual y Limpieza BD */}
        <div className="flex items-center gap-2">
          {onOpenClearModal && (
            <button
              onClick={onOpenClearModal}
              title="Limpieza granular de base de datos"
              className="flex items-center gap-1.5 bg-rose-950/40 hover:bg-rose-900/60 active:scale-95 transition-all text-rose-300 hover:text-rose-200 px-2.5 py-1.5 rounded-lg border border-rose-800/60 text-xs font-medium"
            >
              <Trash2 className="w-3.5 h-3.5 text-rose-400" />
              <span className="hidden sm:inline">Limpiar BD</span>
            </button>
          )}

          <button
            onClick={onRefresh}
            disabled={isLoading}
            title="Refrescar datos ahora"
            className="flex items-center gap-1.5 bg-slate-800/80 hover:bg-slate-700 active:scale-95 transition-all text-slate-200 px-3 py-1.5 rounded-lg border border-slate-700/60 text-xs font-medium"
          >
            <RefreshCw className={`w-3.5 h-3.5 text-cyan-400 ${isLoading ? 'animate-spin' : ''}`} />
            <span className="hidden sm:inline">Actualizar</span>
          </button>
          <span className="text-[10px] text-slate-400 hidden xl:inline">
            {lastUpdatedText}
          </span>
        </div>
      </div>
    </header>
  );
};
