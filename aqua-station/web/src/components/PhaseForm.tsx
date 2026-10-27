'use client';

/**
 * The phase parameter form — the mockup's per-phase control surface.
 *
 * The mockup hand-writes each phase's params as bespoke HTML (a `numField` here, a `segSelect`
 * there, a `toggle`, a static "0 RPM (APAGADO)" badge, a computed hint). That richness is a
 * *presentation* concern; the gateway only stores a flat `params: {key: value}` map. So this
 * renders from a declarative spec keyed by phase, and falls back to plain number fields for any
 * param the spec does not describe — a new gateway param still shows up rather than vanishing.
 *
 * Everything the operator edits is held by `RecipeStudio` and overlaid on the phase, so the
 * plan reads as one thing even though the defaults come from the gateway.
 */

import { Info } from 'lucide-react';
import { cn } from './ui';

export type ParamControl =
  | { kind: 'section'; label: string }
  | {
      kind: 'number';
      key: string;
      label: string;
      unit?: string;
      min?: number;
      max?: number;
      step?: number;
      /** Shown right-aligned under the field, e.g. a computed consigna. */
      hint?: (value: number) => string;
    }
  | { kind: 'select'; key: string; label: string; options: { value: string; label: string }[] }
  | { kind: 'toggle'; key: string; label: string; on: string; off: string }
  | { kind: 'static'; label: string; value: string }
  | { kind: 'hint'; text: (params: Record<string, unknown>) => string };

/** Per-phase control specs, keyed by `phase_id`. Order is render order. */
export const PHASE_FORMS: Record<string, ParamControl[]> = {
  // -- Protocolo A — Planta 1 -------------------------------------------------
  p1_prep: [
    { kind: 'section', label: 'Parámetros de Homogenización' },
    { kind: 'number', key: 'mixer_rpm', label: 'Mezclador T-200 (Lazo PI)', unit: 'RPM', min: 200, max: 3000, step: 50 },
    { kind: 'number', key: 'warm_time_s', label: 'Tiempo previo de mezcla', unit: 's', min: 10, max: 300, step: 10 },
    { kind: 'static', label: 'Aireador ODrive', value: '0 RPM (APAGADO)' },
  ],
  p1_dose: [
    { kind: 'section', label: 'Cálculo de Volumen & Dosificación' },
    {
      kind: 'select',
      key: 'dose_method',
      label: 'Método de Cálculo',
      options: [
        { value: 'stoich', label: 'Estequiométrico (ΔDO × 87.5 L × 7.878)' },
        { value: 'fixed', label: 'Volumen Fijo Personalizado (mL)' },
      ],
    },
    { kind: 'number', key: 'dose_ml', label: 'Volumen de solución madre', unit: 'mL', min: 0.5, max: 100, step: 0.5 },
    { kind: 'number', key: 'stock_conc_gpl', label: 'Concentración madre Na₂SO₃', unit: 'g/L', min: 10, max: 200, step: 10 },
    { kind: 'toggle', key: 'mix_during_dose', label: 'Agitación durante inyección', on: 'ACTIVA (Acelera disolución)', off: 'DETENIDA' },
  ],
  p1_deox: [
    { kind: 'section', label: 'Monitoreo de Agotamiento de Reactivo' },
    { kind: 'number', key: 'do_floor_mg_l', label: 'DO piso objetivo', unit: 'mg/L', min: 0.5, max: 7, step: 0.1 },
    { kind: 'number', key: 'safety_timeout_s', label: 'Timeout de seguridad', unit: 's', min: 60, max: 3600, step: 60 },
    { kind: 'toggle', key: 'do_alert', label: 'Alerta de seguridad si DO < 2.5 mg/L', on: 'ACTIVA', off: '' },
  ],
  p1_end: [
    { kind: 'section', label: 'Acciones de Transición' },
    { kind: 'toggle', key: 'stop_pump', label: 'Detener Bomba Peristáltica', on: 'SÍ (Desarmada)', off: '' },
    { kind: 'toggle', key: 'keep_mixer', label: 'Mantener Mezclador T-200', on: 'SÍ (Recomendado)', off: '' },
    { kind: 'toggle', key: 'keep_rec', label: 'Continuar serie temporal CSV', on: 'SÍ', off: '' },
  ],

  // -- Protocolo B — Planta 2 -------------------------------------------------
  p2_precond: [
    { kind: 'section', label: 'Límites Iniciales' },
    { kind: 'number', key: 'do_start_max_mg_l', label: 'DO inicial máximo permitido (C₀)', unit: 'mg/L', min: 0.5, max: 6, step: 0.1 },
    { kind: 'number', key: 'settle_time_s', label: 'Tiempo de reposo previo', unit: 's', min: 15, max: 300, step: 15 },
    { kind: 'static', label: 'Aireador ODrive', value: '0 RPM (EN REPOSO)' },
  ],
  p2_step: [
    { kind: 'section', label: 'Configuración del Salto' },
    { kind: 'number', key: 'step_rpm', label: 'Escalón ODrive', unit: 'RPM', min: 0, max: 600, step: 10 },
    { kind: 'toggle', key: 'auto_t0', label: 'Marca temporal automática (t₀)', on: 'SÍ (Timestamp RTC)', off: '' },
    { kind: 'section', label: 'Mezclador T-200 Auxiliar' },
    { kind: 'toggle', key: 'mixer_enable', label: 'Habilitar mezclador auxiliar', on: 'ACTIVO', off: 'APAGADO' },
    { kind: 'number', key: 'mixer_rpm', label: 'Consigna Mezclador Auxiliar', unit: 'RPM', min: 100, max: 1500, step: 50 },
  ],
  p2_acq: [
    { kind: 'section', label: 'Criterio de Parada & Cadencia' },
    {
      kind: 'select',
      key: 'stop_crit',
      label: 'Criterio de Parada',
      options: [
        { value: 'do_exact', label: 'Valor exacto de DO en mg/L (Recomendado)' },
        { value: 'time', label: 'Tiempo máximo transcurrido (s)' },
      ],
    },
    { kind: 'number', key: 'stop_do_mg_l', label: 'DO meta de corte (Exacto)', unit: 'mg/L', min: 2, max: 7.15, step: 0.05 },
    { kind: 'number', key: 'safety_timeout_s', label: 'Timeout de seguridad', unit: 's', min: 120, max: 3600, step: 60 },
    { kind: 'hint', text: (p) => `Referencia relativa: ≈ ${((Number(p.stop_do_mg_l) || 0) / 7.15 * 100).toFixed(1)}% de C* (7.15 mg/L)` },
  ],
  p2_analysis: [
    { kind: 'section', label: 'Parámetros del Ajuste OLS' },
    { kind: 'number', key: 'kla_c_star', label: 'Saturación teórica C*', unit: 'mg/L', min: 6, max: 9, step: 0.01 },
    { kind: 'number', key: 'kla_t0_offset', label: 'Retardo de membrana sensor (τp)', unit: 's', min: 0, max: 30, step: 0.5 },
    { kind: 'toggle', key: 'export_kla', label: 'Exportar dataset linealizado [ln(C*-C)]', on: 'SÍ (CSV MATLAB/Python)', off: '' },
  ],

  // -- Protocolo D — Combinado A + B ------------------------------------------
  pd_prep: [
    { kind: 'section', label: 'Parámetros de Homogenización Previa' },
    { kind: 'number', key: 'mixer_rpm', label: 'Mezclador T-200 (Lazo PI)', unit: 'RPM', min: 200, max: 3000, step: 50 },
    { kind: 'number', key: 'warm_time_s', label: 'Tiempo previo de mezcla', unit: 's', min: 10, max: 300, step: 10 },
    { kind: 'static', label: 'Aireador ODrive', value: '0 RPM (APAGADO)' },
  ],
  pd_dose: [
    { kind: 'section', label: 'Dosificación Na₂SO₃ (Desoxigenación)' },
    { kind: 'number', key: 'dose_ml', label: 'Volumen a inyectar', unit: 'mL', min: 0.5, max: 100, step: 0.5 },
    { kind: 'number', key: 'stock_conc_gpl', label: 'Concentración madre', unit: 'g/L', min: 10, max: 200, step: 10 },
    { kind: 'toggle', key: 'mix_during_dose', label: 'Agitación continua', on: 'ACTIVA', off: 'DETENIDA' },
  ],
  pd_deox: [
    { kind: 'section', label: 'Caída Libre de DO' },
    { kind: 'number', key: 'do_floor_mg_l', label: 'DO piso meta', unit: 'mg/L', min: 0.5, max: 7, step: 0.1 },
    { kind: 'number', key: 'safety_timeout_s', label: 'Timeout de seguridad', unit: 's', min: 60, max: 3600, step: 60 },
  ],
  pd_trans: [
    { kind: 'section', label: 'Transición a Reoxigenación' },
    { kind: 'number', key: 'do_start_max_mg_l', label: 'DO máximo para iniciar escalón (C₀)', unit: 'mg/L', min: 0.5, max: 6, step: 0.1 },
    { kind: 'number', key: 'settle_time_s', label: 'Tiempo de reposo', unit: 's', min: 5, max: 300, step: 5 },
  ],
  pd_step: [
    { kind: 'section', label: 'Configuración del Salto' },
    { kind: 'number', key: 'step_rpm', label: 'Escalón ODrive', unit: 'RPM', min: 0, max: 600, step: 10 },
    { kind: 'toggle', key: 'auto_t0', label: 'Marca temporal automática (t₀)', on: 'SÍ (Timestamp RTC)', off: '' },
    { kind: 'section', label: 'Mezclador T-200 Auxiliar' },
    { kind: 'toggle', key: 'mixer_enable', label: 'Habilitar mezclador auxiliar', on: 'ACTIVO', off: 'APAGADO' },
    { kind: 'number', key: 'mixer_rpm', label: 'Consigna Mezclador Auxiliar', unit: 'RPM', min: 100, max: 1500, step: 50 },
  ],
  pd_acq: [
    { kind: 'section', label: 'Adquisición Reoxigenación DO(t)' },
    { kind: 'number', key: 'stop_do_mg_l', label: 'DO meta de corte', unit: 'mg/L', min: 2, max: 7.15, step: 0.05 },
    { kind: 'number', key: 'safety_timeout_s', label: 'Timeout de adquisición', unit: 's', min: 120, max: 3600, step: 60 },
  ],
  pd_analysis: [
    { kind: 'section', label: 'Parámetros del Ajuste OLS' },
    { kind: 'number', key: 'kla_c_star', label: 'Saturación teórica C*', unit: 'mg/L', min: 6, max: 9, step: 0.01 },
    { kind: 'toggle', key: 'export_kla', label: 'Exportar dataset completo', on: 'SÍ', off: '' },
  ],

  // -- Protocolo C — Lazo cerrado --------------------------------------------
  cl_config: [
    { kind: 'section', label: 'Consigna & Algoritmo' },
    { kind: 'number', key: 'setpoint_mg_l', label: 'Setpoint Exacto de DO (SP)', unit: 'mg/L', min: 0.5, max: 12.0, step: 0.05 },
    {
      kind: 'select',
      key: 'ctrl_type',
      label: 'Algoritmo de Control',
      options: [
        { value: 'pid', label: 'PID 2-DOF (Firmware ODrive a 20 Hz)' },
        { value: 'on_off', label: 'On-Off (Banda histéresis ±0.2 mg/L)' },
        { value: 'fuzzy', label: 'Lógica Difusa / Fuzzy (Experimental)' },
      ],
    },
    { kind: 'number', key: 'kp', label: 'Ganancia Proporcional (Kp)', min: 0, max: 2000, step: 0.1 },
    { kind: 'number', key: 'ki', label: 'Ganancia Integral (Ki)', min: 0, max: 50, step: 0.001 },
    { kind: 'number', key: 'kd', label: 'Ganancia Derivativa (Kd)', min: 0, max: 10, step: 0.001 },
    { kind: 'number', key: 'beta', label: 'Ponderación Consigna (beta, 2-DOF)', min: 0.0, max: 1.0, step: 0.05 },
    { kind: 'number', key: 'tt', label: 'Constante Anti-Windup Tt', unit: 's', min: 1.0, max: 500.0, step: 5.0 },
    { kind: 'number', key: 'tau_lag', label: 'Horizonte Predictivo tau_lag', unit: 's', min: 0.0, max: 120.0, step: 5.0 },
    { kind: 'toggle', key: 'mixer_toggle', label: 'Mezclador T-200 Auxiliar', on: 'ENCENDIDO', off: 'APAGADO (Protocolo base)' },
  ],
  cl_run: [
    { kind: 'section', label: 'Seguridad & Duración' },
    { kind: 'number', key: 'run_time_s', label: 'Duración total del ensayo', unit: 's', min: 60, max: 3600, step: 60 },
    { kind: 'toggle', key: 'watchdog', label: 'Watchdog de sensor (35 s)', on: 'ACTIVO (Failsafe 0 RPM si cae sensor)', off: '' },
    { kind: 'toggle', key: 'metrics', label: 'Cálculo de métricas IAE/ISE en vivo', on: 'SÍ (Tiempo Real)', off: '' },
  ],
  cl_eval: [],
};

/** The default value a toggle/'select' falls back to when the phase has not set one. */
export function defaultParamValue(control: ParamControl): number | string | boolean | null {
  switch (control.kind) {
    case 'number':
      return control.min ?? 0;
    case 'select':
      return control.options[0]?.value ?? '';
    case 'toggle':
      return true;
    default:
      return null;
  }
}

const numberInputClass =
  'w-20 bg-scada-well text-slate-100 text-xs font-mono border border-scada-border rounded px-2 py-1 text-right focus:outline-none focus:border-telemetry-do';

/** A small green switch, as the mockup's `toggle`. */
function Switch({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onToggle}
      className={cn(
        'relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors',
        on ? 'bg-emerald-500' : 'bg-slate-800',
      )}
    >
      <span
        className={cn(
          'inline-block h-3 w-3 transform rounded-full bg-white transition-transform',
          on ? 'translate-x-3.5' : 'translate-x-0.5',
        )}
      />
    </button>
  );
}

export function PhaseForm({
  phaseId,
  params,
  onParam,
}: {
  phaseId: string;
  params: Record<string, unknown>;
  onParam: (key: string, value: number | string | boolean) => void;
}) {
  const spec = PHASE_FORMS[phaseId];
  // No spec: fall back to a plain number field per param, so an undescribed gateway param is
  // still visible and editable rather than silently dropped.
  if (!spec) {
    const keys = Object.keys(params);
    if (keys.length === 0) return null;
    return (
      <div className="space-y-2 text-[11px]">
        <div className="text-[10px] text-slate-500 uppercase font-mono tracking-wider">Parámetros de la fase</div>
        {keys.map((key) => (
          <div key={key} className="flex items-center justify-between gap-2">
            <span className="text-slate-300">{key.replace(/_/g, ' ')}</span>
            <input
              type="number"
              value={Number(params[key])}
              onChange={(e) => onParam(key, Number(e.target.value))}
              className={numberInputClass}
            />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-2 text-[11px]">
      {spec.map((control, i) => {
        switch (control.kind) {
          case 'section':
            return (
              <div
                key={`s-${i}`}
                className={cn(
                  'text-[10px] text-slate-500 uppercase font-mono tracking-wider',
                  i > 0 && 'pt-1.5 border-t border-scada-border',
                )}
              >
                {control.label}
              </div>
            );
          case 'number': {
            const raw = params[control.key];
            const value = raw === undefined || raw === null ? '' : Number(raw);
            return (
              <div key={control.key}>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-slate-300">{control.label}</span>
                  <div className="flex items-center gap-1 shrink-0">
                    <input
                      type="number"
                      value={value}
                      min={control.min}
                      max={control.max}
                      step={control.step ?? 'any'}
                      onChange={(e) => onParam(control.key, Number(e.target.value))}
                      className={numberInputClass}
                    />
                    {control.unit ? (
                      <span className="text-[10px] text-slate-500 font-mono w-14">{control.unit}</span>
                    ) : null}
                  </div>
                </div>
                {control.hint ? (
                  <div className="text-[10px] text-slate-400 font-mono text-right mt-0.5">
                    {control.hint(Number(value) || 0)}
                  </div>
                ) : null}
              </div>
            );
          }
          case 'select': {
            const value = String(params[control.key] ?? control.options[0]?.value ?? '');
            return (
              <div key={control.key} className="flex items-center justify-between gap-2">
                <span className="text-slate-300 shrink-0">{control.label}</span>
                <select
                  value={value}
                  onChange={(e) => onParam(control.key, e.target.value)}
                  className="bg-scada-well text-slate-100 text-xs border border-scada-border rounded px-2 py-1 font-mono focus:outline-none focus:border-telemetry-do max-w-[220px]"
                >
                  {control.options.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            );
          }
          case 'toggle': {
            const on = params[control.key] === undefined ? true : Boolean(params[control.key]);
            return (
              <div key={control.key} className="flex items-center justify-between gap-2">
                <span className="text-slate-300">{control.label}</span>
                <div className="flex items-center gap-2 shrink-0">
                  <Switch on={on} onToggle={() => onParam(control.key, !on)} />
                  <span className="text-[10px] text-slate-400 font-mono">{on ? control.on : control.off}</span>
                </div>
              </div>
            );
          }
          case 'static':
            return (
              <div key={`st-${i}`} className="flex items-center justify-between gap-2">
                <span className="text-slate-300">{control.label}</span>
                <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700">
                  {control.value}
                </span>
              </div>
            );
          case 'hint':
            return (
              <div key={`h-${i}`} className="text-[10px] text-slate-400 font-mono flex items-center gap-1.5">
                <Info className="w-3 h-3 text-slate-500 shrink-0" />
                {control.text(params)}
              </div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
