'use client';

/**
 * Derived rule state — and the thresholds behind it.
 *
 * This panel answers a question the previous dashboard could not: *why has that alert gone
 * quiet?* The rules are latched with a recovery deadband, deliberately. Without hysteresis an
 * oxygen reading hovering on a threshold emits an event per sample, which buries the log —
 * but the cost of latching is that silence looks like health.
 *
 * So the latched state is shown explicitly, next to the thresholds it is derived from. Nothing
 * here is invented: both come from `GET /api/events/levels`, which reads the live rule engine.
 */

import { Activity, RefreshCw, ShieldAlert } from 'lucide-react';
import { getLevels } from '@/lib/api';
import { useAsync, useInterval } from '@/hooks/useAsync';
import type { RuleLevel } from '@/lib/types';
import { Badge, EmptyState, ErrorState, Loading, Panel, TONE, cn, toneFromLevel } from './ui';

const RULE_LABELS: Record<string, string> = {
  do_low: 'oxígeno disuelto bajo',
  do_low_critical: 'oxígeno disuelto crítico',
  do_rate: 'tasa de caída de oxígeno',
  do_jump: 'salto de oxígeno',
  temp_high: 'temperatura del agua alta',
  battery_low: 'batería del sensor baja',
};

const LEVEL_EXPLANATION: Record<RuleLevel, string> = {
  OK: 'en rango',
  WARNING: 'enganchado — seguirá en silencio hasta que se recupere más allá de la banda muerta',
  CRITICAL: 'enganchado — seguirá en silencio hasta que se recupere más allá de la banda muerta',
};

export function LevelsPanel() {
  const { data, error, loading, reload } = useAsync(() => getLevels(), []);
  useInterval(reload, 20_000);

  const levels = data?.levels ?? {};
  const thresholds = data?.thresholds ?? null;
  const roles = Object.keys(levels);

  const anyLatched = roles.some((role) =>
    Object.values(levels[role] ?? {}).some((level) => level !== 'OK'),
  );

  return (
    <Panel
      title="Estado de reglas"
      subtitle="Umbrales enganchados — el silencio no es lo mismo que la salud"
      icon={<ShieldAlert className="w-4 h-4 text-sky-400" />}
      actions={
        <button
          type="button"
          onClick={reload}
          className="inline-flex items-center gap-1 text-[11px] font-semibold text-slate-400 hover:text-sky-300 transition-colors"
        >
          <RefreshCw className={cn('w-3 h-3', loading && 'animate-spin')} />
          actualizar
        </button>
      }
    >
      {error ? <ErrorState error={error} onRetry={reload} compact /> : null}

      {!error && loading && !data ? <Loading label="Leyendo estado de reglas" /> : null}

      {!error && data ? (
        <>
          {roles.length === 0 ? (
            <EmptyState
              message="Ninguna regla se ha evaluado aún."
              hint="El motor de reglas solo se engancha una vez que ha visto telemetría de un rol. Un panel vacío en un gateway recién iniciado es lo esperado."
            />
          ) : (
            <div className="flex flex-col gap-2">
              {roles.map((role) => {
                const rules = levels[role] ?? {};
                const entries = Object.entries(rules);
                const worst = entries
                  .map(([, level]) => level)
                  .sort((a, b) =>
                    (a === 'CRITICAL' ? 0 : a === 'WARNING' ? 1 : 2) -
                    (b === 'CRITICAL' ? 0 : b === 'WARNING' ? 1 : 2),
                  )[0];

                return (
                  <div
                    key={role}
                    className={cn(
                      'rounded-xl border px-3 py-2 flex items-center justify-between gap-3',
                      TONE[toneFromLevel(worst)].bg,
                      TONE[toneFromLevel(worst)].border,
                    )}
                  >
                    <div className="min-w-0">
                      <p className="text-xs font-bold text-white">{role}</p>
                      <p className="text-[10px] text-slate-400">
                        {entries.length} rule{entries.length === 1 ? '' : 's'} evaluated
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-1.5 justify-end">
                      {entries.map(([rule, level]) => (
                        <span
                          key={rule}
                          title={LEVEL_EXPLANATION[level]}
                          className={cn(
                            'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border',
                            TONE[toneFromLevel(level)].text,
                            TONE[toneFromLevel(level)].border,
                          )}
                        >
                          <span className={cn('w-1.5 h-1.5 rounded-full', TONE[toneFromLevel(level)].dot)} />
                          {RULE_LABELS[rule] ?? rule}
                        </span>
                      ))}
                    </div>
                  </div>
                );
              })}

              {!anyLatched ? (
                <p className="text-[11px] text-slate-500 flex items-center gap-1.5">
                  <Activity className="w-3 h-3" />
                  Every rule is in range.
                </p>
              ) : (
                <p className="text-[11px] text-amber-400/90">
                  A latched rule emits no further events until it recovers past its deadband.
                  Check the live reading before assuming the condition has cleared.
                </p>
              )}
            </div>
          )}

          {thresholds ? (
            <div className="border-t border-scada-border pt-3">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-2">
                Thresholds in force
              </p>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
                {(
                  [
                    ['DO warning', thresholds.do_warning_mg_l, 'mg/L'],
                    ['DO critical', thresholds.do_critical_mg_l, 'mg/L'],
                    ['DO recovery deadband', thresholds.do_recovery_deadband_mg_l, 'mg/L'],
                    ['DO drop rate', thresholds.do_drop_rate_mg_l_per_min, 'mg/L/min'],
                    ['DO step change', thresholds.do_jump_mg_l, 'mg/L'],
                    ['Water temp warning', thresholds.temp_warning_c, '°C'],
                    ['Battery warning', thresholds.battery_warning_v, 'V'],
                  ] as const
                ).map(([label, value, unit]) => (
                  <div key={label} className="contents">
                    <dt className="text-slate-500">{label}</dt>
                    <dd className="text-right font-mono text-slate-300">
                      {value} <span className="text-slate-600">{unit}</span>
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="text-[10px] text-slate-500 mt-2">
                These are the live values the rule engine is using, not frontend defaults — the
                same numbers the oxygen gauge is drawn from.
              </p>
            </div>
          ) : null}
        </>
      ) : null}
    </Panel>
  );
}
