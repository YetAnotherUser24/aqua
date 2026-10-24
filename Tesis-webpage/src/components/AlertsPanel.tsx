'use client';

import React from 'react';
import { Alert } from '@/types';
import { AlertTriangle, AlertOctagon, Info, CheckCircle, Clock, ShieldCheck } from 'lucide-react';
import { formatPeruDateTime } from '@/lib/dateUtils';

interface AlertsPanelProps {
  alerts: Alert[];
  onAcknowledgeAlert: (alertId: number, newStatus: 'reconocida' | 'resuelta') => void;
}

export const AlertsPanel: React.FC<AlertsPanelProps> = ({ alerts, onAcknowledgeAlert }) => {
  const getSeverityIcon = (severity: string) => {
    switch (severity) {
      case 'critical':
        return <AlertOctagon className="w-5 h-5 text-rose-400 shrink-0 animate-pulse" />;
      case 'warning':
        return <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0" />;
      default:
        return <Info className="w-5 h-5 text-cyan-400 shrink-0" />;
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'activa':
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-rose-950/80 text-rose-400 border border-rose-800 animate-pulse">
            Activa
          </span>
        );
      case 'reconocida':
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-amber-950/80 text-amber-400 border border-amber-800">
            Reconocida
          </span>
        );
      case 'resuelta':
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-emerald-950/80 text-emerald-400 border border-emerald-800">
            Resuelta
          </span>
        );
      default:
        return null;
    }
  };

  const activeAlerts = alerts.filter(a => a.status === 'activa');

  return (
    <div className="glass-panel rounded-2xl p-5 border border-cyan-900/30 flex flex-col gap-4">
      <div className="flex items-center justify-between border-b border-slate-800 pb-3">
        <div className="flex items-center gap-2">
          <div className="p-2 rounded-xl bg-amber-950/80 border border-amber-800/60">
            <AlertTriangle className="w-4 h-4 text-amber-400" />
          </div>
          <div>
            <h3 className="text-sm font-bold text-white flex items-center gap-2">
              <span>Panel de Alertas del Sistema</span>
              {activeAlerts.length > 0 ? (
                <span className="text-xs px-2 py-0.5 rounded-full bg-rose-950 text-rose-300 border border-rose-800 font-bold">
                  {activeAlerts.length} críticas
                </span>
              ) : (
                <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-950 text-emerald-400 border border-emerald-800 font-bold">
                  Sistema Normal
                </span>
              )}
            </h3>
            <p className="text-xs text-slate-400">Oxígeno disuelto bajo, fallos de sensor y sobrecalentamiento</p>
          </div>
        </div>
      </div>

      <div className="space-y-3">
        {alerts.length === 0 ? (
          <div className="p-6 text-center text-slate-500 flex flex-col items-center gap-2">
            <ShieldCheck className="w-8 h-8 text-emerald-400/60" />
            <p className="text-xs font-medium">No hay alertas activas en los estanques de acuicultura.</p>
          </div>
        ) : (
          alerts.slice(0, 5).map((alert) => (
            <div
              key={alert.id}
              className={`p-3.5 rounded-xl border flex flex-col sm:flex-row sm:items-center justify-between gap-3 transition-all ${
                alert.status === 'activa'
                  ? 'bg-rose-950/20 border-rose-900/40 shadow-sm'
                  : 'bg-slate-900/50 border-slate-800/80'
              }`}
            >
              <div className="flex items-start gap-3">
                {getSeverityIcon(alert.severity)}
                <div>
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-bold text-slate-200">{alert.message}</span>
                    {getStatusBadge(alert.status)}
                  </div>
                  <div className="flex items-center gap-2 text-[11px] text-slate-400">
                    <Clock className="w-3 h-3 text-slate-500" />
                    <span>{formatPeruDateTime(alert.created_at)}</span>
                  </div>
                </div>
              </div>

              {/* Botones de acción según el estado */}
              <div className="flex items-center gap-2 shrink-0 self-end sm:self-center">
                {alert.status === 'activa' && (
                  <button
                    onClick={() => onAcknowledgeAlert(alert.id, 'reconocida')}
                    className="px-2.5 py-1 text-xs font-medium rounded-lg bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700 transition-colors"
                  >
                    Reconocer
                  </button>
                )}
                {alert.status !== 'resuelta' && (
                  <button
                    onClick={() => onAcknowledgeAlert(alert.id, 'resuelta')}
                    className="flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg bg-emerald-950/80 hover:bg-emerald-900 text-emerald-300 border border-emerald-800 transition-colors"
                  >
                    <CheckCircle className="w-3 h-3" />
                    <span>Resolver</span>
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};
