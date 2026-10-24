'use client';

/**
 * The milestone composer: document a point on the run.
 *
 * Kept as a small, explicit form rather than a free-text box, because the *kind* is what makes
 * a milestone useful after the fact — "sulfite" and "step" sort, filter and colour, whereas a
 * paragraph of prose does not. The kind list mirrors what the recipe actually does, so the
 * common annotations are one click and the uncommon ones are still possible via "note".
 */

import { useState } from 'react';
import { BookmarkPlus, X } from 'lucide-react';
import { postMilestone } from '@/lib/api';
import type { MilestoneKind, RunStateResponse } from '@/lib/types';
import { cn } from './ui';

const KINDS: { value: MilestoneKind; label: string; hint: string }[] = [
  { value: 'sulfite', label: 'Inyección Na₂SO₃', hint: 'Desoxigenación química (Planta 1)' },
  { value: 'step', label: 'Escalón aireador', hint: 'Cambio de consigna ODrive (Planta 2)' },
  { value: 'winkler', label: 'Muestra Winkler', hint: 'Titulación manual de contraste' },
  { value: 'note', label: 'Nota del operador', hint: 'Observación libre' },
];

const DEFAULT_DETAIL: Record<MilestoneKind, string> = {
  sulfite: '10.0 mL Na₂SO₃ vertidos',
  step: 'Escalón aplicado',
  winkler: 'Muestra tomada',
  note: '',
};

export function MilestoneComposer({
  run,
  onClose,
  onPosted,
}: {
  run: RunStateResponse | null;
  onClose: () => void;
  onPosted: () => void;
}) {
  const [kind, setKind] = useState<MilestoneKind>('note');
  const [label, setLabel] = useState('');
  const [detail, setDetail] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const experimentId = run?.experiment_id ?? null;

  const submit = async () => {
    if (!experimentId) return;
    setBusy(true);
    setError(null);
    const chosen = KINDS.find((k) => k.value === kind);
    try {
      await postMilestone(experimentId, {
        kind,
        label: label.trim() || chosen?.label || 'Hito',
        detail: detail.trim(),
      });
      onPosted();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'no se pudo registrar el hito');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="bg-scada-surface border border-scada-border rounded-2xl max-w-md w-full p-5 space-y-4 shadow-xl">
        <div className="flex items-center justify-between border-b border-scada-border pb-3">
          <div className="flex items-center gap-2 text-white font-bold text-sm">
            <BookmarkPlus className="w-4 h-4 text-telemetry-do" />
            <span>Añadir hito en la curva</span>
          </div>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-xs text-slate-400">
          Marca el segundo actual
          {run?.t_rel_s != null ? (
            <>
              {' '}
              (<span className="font-mono text-telemetry-do">t = {Math.floor(run.t_rel_s)} s</span>)
            </>
          ) : null}{' '}
          para documentar un evento en los datos del ensayo.
        </p>

        <div className="space-y-2">
          <label className="text-xs text-slate-300 font-medium block">Tipo de evento:</label>
          <div className="grid grid-cols-2 gap-2">
            {KINDS.map((k) => (
              <button
                key={k.value}
                type="button"
                onClick={() => setKind(k.value)}
                className={cn(
                  'p-2 rounded-lg border text-left transition',
                  kind === k.value
                    ? 'bg-scada-elevated border-telemetry-do/60 text-white'
                    : 'bg-scada-well border-scada-border text-slate-400 hover:text-slate-200',
                )}
              >
                <span className="block text-[11px] font-semibold">{k.label}</span>
                <span className="block text-[9px] text-slate-500">{k.hint}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          <label className="text-xs text-slate-300 font-medium block">
            Etiqueta <span className="text-slate-500">(opcional)</span>
          </label>
          <input
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={KINDS.find((k) => k.value === kind)?.label}
            className="w-full bg-scada-well border border-scada-border rounded-lg p-2 text-xs text-white focus:outline-none focus:border-telemetry-do"
          />
        </div>

        <div className="space-y-2">
          <label className="text-xs text-slate-300 font-medium block">Detalle / parámetro:</label>
          <input
            type="text"
            value={detail}
            onChange={(e) => setDetail(e.target.value)}
            placeholder={DEFAULT_DETAIL[kind]}
            className="w-full bg-scada-well border border-scada-border rounded-lg p-2 text-xs text-white font-mono focus:outline-none focus:border-telemetry-do"
          />
        </div>

        {error ? <p className="text-[11px] text-rose-400">{error}</p> : null}
        {!experimentId ? (
          <p className="text-[11px] text-amber-400">
            No hay ensayo activo: los hitos se registran contra un experimento.
          </p>
        ) : null}

        <div className="flex justify-end gap-2 pt-2 border-t border-scada-border">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-xs text-slate-300 hover:bg-slate-800"
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy || !experimentId}
            className="px-4 py-1.5 rounded-lg text-xs font-bold bg-telemetry-do hover:bg-sky-400 text-slate-900 disabled:opacity-50"
          >
            {busy ? 'Guardando…' : 'Insertar en gráfica'}
          </button>
        </div>
      </div>
    </div>
  );
}
