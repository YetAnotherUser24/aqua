'use client';

import React, { useState } from 'react';
import { Activity, Play, ShieldAlert, Power } from 'lucide-react';
import { OrchestratorState, SystemState } from '@/types';
import { formatPeruTime } from '@/lib/dateUtils';

interface OrchestratorBarProps {
  systemState: SystemState | null;
  onChangeState: (state: OrchestratorState) => Promise<void>;
}

const STATE_META: Record<OrchestratorState, { label: string; hint: string; classes: string; icon: React.ReactNode }> = {
  IDLE: {
    label: 'IDLE',
    hint: 'Reposo · heartbeat 20 s · sin grabación',
    classes: 'bg-slate-700/40 text-slate-200 border-slate-500/50',
    icon: <Power className="w-4 h-4" />
  },
  ACTIVE_EXPERIMENT: {
    label: 'ACTIVE_EXPERIMENT',
    hint: 'Adquisición a alta velocidad (0.2 Hz / 5 Hz)',
    classes: 'bg-cyan-600/30 text-cyan-300 border-cyan-400/60',
    icon: <Activity className="w-4 h-4" />
  },
  MANUAL_OVERRIDE: {
    label: 'MANUAL_OVERRIDE',
    hint: 'Control directo del operador · recetas abortadas',
    classes: 'bg-rose-600/30 text-rose-300 border-rose-400/60',
    icon: <ShieldAlert className="w-4 h-4" />
  }
};

/**
 * Barra de orquestación global AquaControl V4: expone los tres estados del sistema
 * (IDLE, ACTIVE_EXPERIMENT, MANUAL_OVERRIDE) y actúa como interruptor maestro.
 */
export const OrchestratorBar: React.FC<OrchestratorBarProps> = ({ systemState, onChangeState }) => {
  const [isBusy, setIsBusy] = useState(false);
  const current: OrchestratorState = systemState?.state || 'IDLE';
  const meta = STATE_META[current];

  const transition = async (target: OrchestratorState) => {
    if (target === current || isBusy) return;
    setIsBusy(true);
    try {
      await onChangeState(target);
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center justify-between gap-4 bg-slate-900/70 border border-slate-700/60 p-4 rounded-2xl">
      <div className="flex items-center gap-3">
        <span className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border text-xs font-bold tracking-wide ${meta.classes}`}>
          {meta.icon}
          {meta.label}
        </span>
        <div className="text-xs text-slate-400">
          <div className="font-semibold text-slate-300">{meta.hint}</div>
          <div>
            Exp.: <span className="text-slate-200">{systemState?.experiment_id || '—'}</span>
            {' · '}
            desde <span className="text-slate-200">{systemState?.since ? formatPeruTime(new Date(systemState.since), true) : '—'}</span>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          disabled={isBusy}
          onClick={() => transition('IDLE')}
          className={`px-3 py-2 rounded-lg text-xs font-bold border transition-colors disabled:opacity-50 ${
            current === 'IDLE'
              ? 'bg-slate-600 text-white border-slate-400'
              : 'bg-slate-800 hover:bg-slate-700 text-slate-300 border-slate-700'
          }`}
        >
          IDLE
        </button>
        <button
          disabled={isBusy}
          onClick={() => transition('ACTIVE_EXPERIMENT')}
          className={`px-3 py-2 rounded-lg text-xs font-bold border transition-colors disabled:opacity-50 flex items-center gap-1.5 ${
            current === 'ACTIVE_EXPERIMENT'
              ? 'bg-cyan-600 text-white border-cyan-400'
              : 'bg-slate-800 hover:bg-slate-700 text-slate-300 border-slate-700'
          }`}
        >
          <Play className="w-3.5 h-3.5" /> Experimento
        </button>
        <button
          disabled={isBusy}
          onClick={() => transition(current === 'MANUAL_OVERRIDE' ? 'IDLE' : 'MANUAL_OVERRIDE')}
          className={`px-3 py-2 rounded-lg text-xs font-bold border transition-colors disabled:opacity-50 flex items-center gap-1.5 ${
            current === 'MANUAL_OVERRIDE'
              ? 'bg-rose-600 text-white border-rose-400'
              : 'bg-rose-950/60 hover:bg-rose-900 text-rose-300 border-rose-800/60'
          }`}
        >
          <ShieldAlert className="w-3.5 h-3.5" />
          {current === 'MANUAL_OVERRIDE' ? 'Salir de Override' : 'Manual Override'}
        </button>
      </div>
    </div>
  );
};
