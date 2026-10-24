'use client';

import React from 'react';
import { MotorEvent } from '@/types';
import { History, Play, Square, FastForward, AlertTriangle, Radio } from 'lucide-react';
import { formatPeruDateTime } from '@/lib/dateUtils';

interface EventsTableProps {
  events: MotorEvent[];
}

export const EventsTable: React.FC<EventsTableProps> = ({ events }) => {
  const getEventBadge = (type: string) => {
    switch (type) {
      case 'start':
        return (
          <span className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-950/80 text-emerald-400 border border-emerald-800">
            <Play className="w-3 h-3 fill-current" />
            <span>Encendido</span>
          </span>
        );
      case 'stop':
        return (
          <span className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700">
            <Square className="w-3 h-3 fill-current" />
            <span>Apagado</span>
          </span>
        );
      case 'speed_change':
        return (
          <span className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-cyan-950/80 text-cyan-400 border border-cyan-800">
            <FastForward className="w-3 h-3" />
            <span>Cambio Velocidad</span>
          </span>
        );
      case 'offline':
        return (
          <span className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-rose-950/80 text-rose-400 border border-rose-800">
            <AlertTriangle className="w-3 h-3" />
            <span>Desconectado</span>
          </span>
        );
      default:
        return (
          <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-800 text-slate-300">
            {type}
          </span>
        );
    }
  };

  const getSourceBadge = (source: string) => {
    switch (source) {
      case 'manual':
        return <span className="text-xs text-cyan-400 font-medium">Manual (Operador)</span>;
      case 'automatic':
        return <span className="text-xs text-teal-400 font-medium">Automático (OD Regla)</span>;
      case 'emergency':
        return <span className="text-xs text-rose-400 font-semibold">Parada Emergencia</span>;
      default:
        return <span className="text-xs text-slate-400 font-medium">Sistema</span>;
    }
  };

  return (
    <div className="glass-panel rounded-2xl p-5 border border-cyan-900/30 flex flex-col gap-4">
      <div className="flex items-center justify-between border-b border-slate-800 pb-3">
        <div className="flex items-center gap-2">
          <div className="p-2 rounded-xl bg-cyan-950/80 border border-cyan-800/60">
            <History className="w-4 h-4 text-cyan-400" />
          </div>
          <div>
            <h3 className="text-sm font-bold text-white flex items-center gap-2">
              <span>Registro de Eventos del Motor</span>
              <span className="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 font-medium">
                Últimas 24h
              </span>
            </h3>
            <p className="text-xs text-slate-400">Encendidos, paradas, y variaciones de velocidad registradas</p>
          </div>
        </div>
        <span className="text-xs text-slate-500 font-medium">
          {events.length} registros
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs text-slate-300">
          <thead className="bg-slate-900/80 uppercase text-[11px] text-slate-400 font-semibold border-y border-slate-800">
            <tr>
              <th className="py-2.5 px-3">Fecha y Hora (GMT-5)</th>
              <th className="py-2.5 px-3">Tipo de Evento</th>
              <th className="py-2.5 px-3">Velocidad / PWM</th>
              <th className="py-2.5 px-3">Origen</th>
              <th className="py-2.5 px-3">Notas / Detalle</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60 font-normal">
            {events.length === 0 ? (
              <tr>
                <td colSpan={5} className="text-center py-6 text-slate-500">
                  No hay eventos registrados aún
                </td>
              </tr>
            ) : (
              events.slice(0, 8).map((evt, idx) => (
                <tr key={evt.id || idx} className="hover:bg-slate-800/30 transition-colors">
                  <td className="py-2.5 px-3 whitespace-nowrap text-slate-300 font-mono text-[11px]">
                    {formatPeruDateTime(evt.started_at, true)}
                  </td>
                  <td className="py-2.5 px-3">{getEventBadge(evt.event_type)}</td>
                  <td className="py-2.5 px-3 whitespace-nowrap">
                    <span className="font-bold text-white">{evt.speed_percent}%</span>
                    <span className="text-slate-500 text-[10px] ml-1">({evt.pwm_us} µs)</span>
                  </td>
                  <td className="py-2.5 px-3 whitespace-nowrap">{getSourceBadge(evt.source)}</td>
                  <td className="py-2.5 px-3 text-slate-400 max-w-xs truncate" title={evt.notes}>
                    {evt.notes || '--'}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
