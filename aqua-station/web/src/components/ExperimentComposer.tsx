'use client';

/**
 * Name and describe the run before it starts.
 *
 * The gateway has always accepted `title` and `notes` on `POST /api/experiments`, and
 * `createExperiment` has always passed them -- but nothing ever asked for them, so every run
 * was created as the recipe's own title with an empty description. The database kept a column
 * for a name nobody chose. This is the missing prompt.
 *
 * Defaults, not a blank form. The field is prefilled with the recipe title and the recipe's
 * subject line, so the fast path is still one click: an operator who does not care about
 * naming the run presses Iniciar and gets exactly what the console used to create. Typing is
 * opt-in, which is the whole reason this is a dialog and not a mandatory page.
 *
 * Only `title` and `notes` are offered. The API also carries `hypothesis`, but nothing reads
 * it and a field that is always left empty is worse than no field: it invites an operator to
 * fill one in, then never shows it back. Adding it later is a field on this form.
 */

import { useEffect, useState } from 'react';
import { FlaskConical, X } from 'lucide-react';
import type { RecipeDef } from '@/lib/types';
import { cn } from './ui';

export interface ExperimentDraft {
  title: string;
  notes: string;
}

export function ExperimentComposer({
  recipe,
  busy,
  onCancel,
  onConfirm,
}: {
  /** The recipe about to run, or null when the console has none and falls back to a default. */
  recipe: RecipeDef | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (draft: ExperimentDraft) => void;
}) {
  const defaultTitle = recipe?.title ?? 'Ensayo sin título';
  // `RecipeDef` carries no recipe-level description -- only phases do. The first phase's
  // description is the run's stated objective ("Homogeniza los 87.5 L … antes de dosificar"),
  // so it seeds the notes. It is HTML-ish prose written for the recipe studio, and `notes` is
  // rendered as text, so it is stripped to its text first or the tags would show literally.
  const defaultNotes = entityText(recipe?.phases?.[0]?.desc ?? '');

  const [title, setTitle] = useState(defaultTitle);
  const [notes, setNotes] = useState(defaultNotes);

  // Escape closes, matching every other dialog in the console. Bound on the document because
  // the dialog has no focus trap and the operator may have clicked straight into a field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const submit = () => {
    if (busy) return;
    // A blank name is not a title. Fall back to the recipe rather than persisting "" -- an
    // unnamed run is unreadable in the experiment list, where the title is the only handle.
    onConfirm({ title: title.trim() || defaultTitle, notes: notes.trim() });
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="exp-composer-title"
        className="bg-scada-surface border border-scada-border rounded-2xl max-w-lg w-full p-5 space-y-4 shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-scada-border pb-3">
          <div className="flex items-center gap-2 text-white font-bold text-sm">
            <FlaskConical className="w-4 h-4 text-telemetry-do" />
            <span id="exp-composer-title">Nombrar el ensayo</span>
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label="Cerrar"
            className="text-slate-400 hover:text-white"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-xs text-slate-400">
          Se guarda con el ensayo y aparece en la cabecera mientras corre. Puedes dejarlo tal
          cual y pulsar Iniciar.
        </p>

        <div className="space-y-2">
          <label htmlFor="exp-title" className="text-xs text-slate-300 font-medium block">
            Nombre del ensayo
          </label>
          <input
            id="exp-title"
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit();
            }}
            placeholder={defaultTitle}
            className="w-full bg-scada-well border border-scada-border rounded-lg p-2 text-xs text-white focus:outline-none focus:border-telemetry-do"
          />
        </div>

        <div className="space-y-2">
          <label htmlFor="exp-notes" className="text-xs text-slate-300 font-medium block">
            Descripción <span className="text-slate-500">(opcional)</span>
          </label>
          <textarea
            id="exp-notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={4}
            placeholder="Objetivo del ensayo, condiciones iniciales, lo que esperas observar…"
            className="w-full bg-scada-well border border-scada-border rounded-lg p-2 text-xs text-white resize-y focus:outline-none focus:border-telemetry-do"
          />
        </div>

        <div className="flex justify-end gap-2 pt-2 border-t border-scada-border">
          <button
            type="button"
            onClick={onCancel}
            className="px-3 py-1.5 rounded-lg text-xs text-slate-300 hover:bg-slate-800"
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy}
            className={cn(
              'px-4 py-1.5 rounded-lg text-xs font-bold bg-telemetry-do hover:bg-sky-400 text-slate-900',
              busy && 'opacity-50',
            )}
          >
            {busy ? 'Iniciando…' : 'Iniciar ensayo'}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Turn the recipe's authored description into plain text.
 *
 * The recipe `desc` fields carry `<strong>Objetivo:</strong>`-style markup for the recipe
 * studio, where it is rendered as HTML. `notes` is shown as text, so the tags would appear
 * literally. The browser's own parser does this correctly -- including entities like `Na₂SO₃`
 * -- and is cheaper than a regex that would be wrong on nested tags.
 */
function entityText(html: string): string {
  if (!html) return '';
  if (typeof document === 'undefined') return html;
  const el = document.createElement('div');
  el.innerHTML = html;
  return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
}
