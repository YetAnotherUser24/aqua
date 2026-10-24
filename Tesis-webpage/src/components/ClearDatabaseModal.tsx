'use client';

import React, { useState } from 'react';
import { Trash2, AlertTriangle, X, CheckSquare, Square, Clock, ShieldAlert, Loader2 } from 'lucide-react';
import { ClearCategory, TimeScope } from '@/types';

interface ClearDatabaseModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

interface CategoryOption {
  id: ClearCategory;
  label: string;
  description: string;
}

const CATEGORIES: CategoryOption[] = [
  {
    id: 'sensor_readings',
    label: 'Mediciones del Sensor OD',
    description: 'Registros de oxígeno disuelto, saturación %, temp. agua y batería.'
  },
  {
    id: 'motor_telemetry',
    label: 'Telemetría de Motores (ODrive / ESC)',
    description: 'Lecturas de RPM, potencia (W), corriente y eventos de arranque/parada.'
  },
  {
    id: 'alerts_commands',
    label: 'Alertas y Cola de Comandos',
    description: 'Historial de alertas preventivas y comandos despachados a dispositivos.'
  },
  {
    id: 'experiments',
    label: 'Registro de Experimentos Archivados',
    description: 'Lista histórica de ensayos guardados en el registro general del sistema.'
  }
];

const TIME_SCOPES: { id: TimeScope; label: string }[] = [
  { id: 'all', label: 'Todo el historial (Purga completa)' },
  { id: 'older_than_1h', label: 'Anteriores a 1 hora' },
  { id: 'older_than_24h', label: 'Anteriores a 24 horas' },
  { id: 'older_than_today', label: 'Anteriores al día de hoy (00:00)' }
];

export const ClearDatabaseModal: React.FC<ClearDatabaseModalProps> = ({
  isOpen,
  onClose,
  onSuccess
}) => {
  const [selectedCategories, setSelectedCategories] = useState<ClearCategory[]>([
    'sensor_readings',
    'motor_telemetry',
    'alerts_commands'
  ]);
  const [timeScope, setTimeScope] = useState<TimeScope>('all');
  const [isDeleting, setIsDeleting] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  if (!isOpen) return null;

  const toggleCategory = (cat: ClearCategory) => {
    setSelectedCategories(prev =>
      prev.includes(cat) ? prev.filter(c => c !== cat) : [...prev, cat]
    );
  };

  const handleSelectAll = () => {
    if (selectedCategories.length === CATEGORIES.length) {
      setSelectedCategories([]);
    } else {
      setSelectedCategories(CATEGORIES.map(c => c.id));
    }
  };

  const handleConfirmClear = async () => {
    if (selectedCategories.length === 0) {
      setErrorMsg('Seleccione al menos una categoría para continuar.');
      return;
    }

    setIsDeleting(true);
    setErrorMsg(null);

    try {
      const res = await fetch('/api/database/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          categories: selectedCategories,
          time_scope: timeScope
        })
      });

      const data = await res.json();
      if (!res.ok && res.status !== 207) {
        throw new Error(data.error || data.message || 'Error al limpiar la base de datos');
      }

      if (data.details) {
        const errorEntries = Object.entries(data.details).filter(
          ([_, val]) => typeof val === 'string' && (val as string).startsWith('Error:')
        );
        if (errorEntries.length > 0) {
          const errList = errorEntries.map(([cat, val]) => `${cat}: ${val}`).join(' | ');
          throw new Error(`Limpieza con advertencias: ${errList}`);
        }
      }

      onSuccess();
      onClose();
    } catch (err: any) {
      setErrorMsg(err.message || 'Error de conexión al purgar base de datos');
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-in fade-in duration-200">
      <div className="relative w-full max-w-lg rounded-2xl border border-rose-900/50 bg-slate-950 p-6 shadow-2xl shadow-rose-950/40 text-slate-100 flex flex-col gap-5">
        
        {/* Encabezado */}
        <div className="flex items-start justify-between gap-3 border-b border-slate-800 pb-4">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-rose-950/80 border border-rose-800 text-rose-400 shadow-md">
              <Trash2 className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-bold text-white flex items-center gap-2">
                <span>Limpieza Granular de Base de Datos</span>
              </h3>
              <p className="text-xs text-slate-400">
                Seleccione qué tablas y ventana temporal desea purgar.
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            disabled={isDeleting}
            className="text-slate-500 hover:text-slate-300 transition-colors p-1"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Advertencia */}
        <div className="flex items-start gap-2.5 p-3 rounded-xl bg-rose-950/30 border border-rose-900/40 text-xs text-rose-300">
          <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
          <div>
            <strong className="font-semibold block text-rose-200">Acción destructiva permanente:</strong>
            Los registros eliminados de Supabase no podrán recuperarse.
          </div>
        </div>

        {/* 1. Selección de Categorías */}
        <div className="space-y-2.5">
          <div className="flex items-center justify-between text-xs font-semibold text-slate-300">
            <span>Tablas / Categorías a Purgar:</span>
            <button
              type="button"
              onClick={handleSelectAll}
              className="text-[11px] font-medium text-cyan-400 hover:text-cyan-300 transition-colors"
            >
              {selectedCategories.length === CATEGORIES.length ? 'Desmarcar todo' : 'Marcar todo'}
            </button>
          </div>

          <div className="grid grid-cols-1 gap-2 max-h-48 overflow-y-auto pr-1">
            {CATEGORIES.map((cat) => {
              const isChecked = selectedCategories.includes(cat.id);
              return (
                <button
                  key={cat.id}
                  type="button"
                  onClick={() => toggleCategory(cat.id)}
                  className={`flex items-start gap-3 p-2.5 rounded-xl border text-left transition-all ${
                    isChecked
                      ? 'bg-rose-950/20 border-rose-800/80 text-slate-200'
                      : 'bg-slate-900/50 border-slate-800/60 text-slate-400 hover:border-slate-700'
                  }`}
                >
                  <div className="pt-0.5 text-rose-400">
                    {isChecked ? <CheckSquare className="w-4 h-4" /> : <Square className="w-4 h-4 text-slate-600" />}
                  </div>
                  <div className="text-xs">
                    <span className="font-semibold block text-slate-200">{cat.label}</span>
                    <span className="text-[11px] text-slate-400">{cat.description}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* 2. Alcance Temporal */}
        <div className="space-y-2">
          <label className="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
            <Clock className="w-3.5 h-3.5 text-cyan-400" />
            <span>Ventana Temporal de la Purga:</span>
          </label>
          <select
            value={timeScope}
            onChange={(e) => setTimeScope(e.target.value as TimeScope)}
            className="w-full text-xs rounded-xl bg-slate-900 border border-slate-700 px-3 py-2 text-slate-200 focus:outline-none focus:border-rose-500 transition-colors"
          >
            {TIME_SCOPES.map(ts => (
              <option key={ts.id} value={ts.id}>
                {ts.label}
              </option>
            ))}
          </select>
        </div>

        {errorMsg && (
          <div className="p-2.5 rounded-lg bg-rose-950/50 border border-rose-700 text-xs text-rose-200">
            {errorMsg}
          </div>
        )}

        {/* Acciones */}
        <div className="flex items-center justify-end gap-3 pt-2 border-t border-slate-800">
          <button
            type="button"
            onClick={onClose}
            disabled={isDeleting}
            className="px-4 py-2 text-xs font-semibold text-slate-400 hover:text-slate-200 transition-colors"
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={handleConfirmClear}
            disabled={isDeleting || selectedCategories.length === 0}
            className="flex items-center gap-1.5 bg-rose-600 hover:bg-rose-500 active:scale-95 disabled:opacity-50 disabled:pointer-events-none transition-all text-white px-4 py-2 rounded-xl text-xs font-bold shadow-lg shadow-rose-900/40"
          >
            {isDeleting ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                <span>Purgando datos...</span>
              </>
            ) : (
              <>
                <Trash2 className="w-4 h-4" />
                <span>Confirmar y Limpiar</span>
              </>
            )}
          </button>
        </div>

      </div>
    </div>
  );
};
