/**
 * Registry-driven selection and derived state.
 *
 * The dashboard renders from `GET /api/roles` rather than from hardcoded field names, so
 * adding a fifth node type stays a `roles.yaml` edit. This module is where that promise is
 * cashed in: nothing below names a field except the one map explained in
 * `QUANTITY_FIELD`.
 */

import type { FieldDef, LiveNode, LiveSnapshot, Registry, RoleDef } from './types';

export const QUANTITIES = {
  dissolvedOxygen: 'dissolved_oxygen',
  waterTemperature: 'water_temperature',
} as const;

/**
 * Quantity -> the field carrying it in the authoritative role.
 *
 * This is the only place the frontend has to name a field, and it exists because
 * `roles.yaml` declares `authoritative_for: [dissolved_oxygen, water_temperature]` on
 * `od_sensor` without saying *which field* carries each quantity. It is therefore not
 * derivable from the registry as it stands.
 *
 * Rather than trust it, `readQuantity` validates the name against the registry on every
 * read and returns a `fault` when it is absent. So renaming `od_do_mg_l` produces an
 * explicit configuration error on screen, not a blank card — the failure is loud.
 *
 * Adding a `primary_field:` next to each `authoritative_for` entry in `roles.yaml` would
 * remove this constant entirely. Until then it must stay in sync with the registry.
 */
export const QUANTITY_FIELD: Record<string, string> = {
  [QUANTITIES.dissolvedOxygen]: 'od_do_mg_l',
  [QUANTITIES.waterTemperature]: 'od_temp_c',
};

/**
 * The echoed copy of a quantity that an actuator may hold (`peer_*` fields).
 *
 * Used *only* as a fallback when the authoritative node is silent, and always labelled
 * `peer` in the UI. It is a real measurement — the aerator genuinely received it over
 * ESP-NOW — but it is not the owning node's claim, and averaging the two would be wrong.
 */
export const QUANTITY_PEER_FIELD: Record<string, string> = {
  [QUANTITIES.dissolvedOxygen]: 'peer_do_mg_l',
  [QUANTITIES.waterTemperature]: 'peer_temp_c',
};

export interface Reading {
  value: number | null;
  field: FieldDef | null;
  role: string | null;
  node: LiveNode | null;
  /** Who is the source of this value, and therefore how much it can be trusted. */
  source: 'authoritative' | 'peer' | 'none';
  /** The holding node has gone quiet for longer than its cadence allows. */
  stale: boolean;
  /** True when no node reported this quantity at all — distinct from a node that is
   *  present but silent, which is `stale`/`value === null`. */
  missing: boolean;
  /** Registry configuration fault. Distinct from "no data" — one is our bug, the other is
   *  the pond's. */
  fault: string | null;
}

const EMPTY_READING: Reading = {
  value: null,
  field: null,
  role: null,
  node: null,
  source: 'none',
  stale: false,
  missing: true,
  fault: null,
};

export function roleDef(registry: Registry | null, role: string): RoleDef | undefined {
  return registry?.roles?.[role];
}

export function fieldDef(
  registry: Registry | null,
  role: string,
  field: string,
): FieldDef | undefined {
  return roleDef(registry, role)?.fields.find((f) => f.name === field);
}

export function roleLabel(registry: Registry | null, role: string): string {
  return roleDef(registry, role)?.display_name ?? role;
}

export function roles(registry: Registry | null): string[] {
  return registry ? Object.keys(registry.roles) : [];
}

/**
 * Whether a node's silence is a fault.
 *
 * The gateway marks `online` from MQTT Last Will & Testament, which only fires on a
 * *disconnect*. A node that is connected but wedged reports nothing and still looks
 * online, so presence alone is not enough — age against the declared cadence is.
 *
 * Three missed intervals, floored at 30 s: long enough that one dropped packet is not an
 * alarm, short enough that an operator notices before the next measurement is due.
 */
export function nodeStale(node: LiveNode | null, def: RoleDef | undefined): boolean {
  if (!node) return true;
  if (!node.online) return true;
  const idleHz = def?.expected_rate_hz?.idle ?? 0.0167; // 1/min
  const periodS = idleHz > 0 ? 1 / idleHz : 60;
  return (node.age_s ?? 0) > Math.max(periodS * 3, 30);
}

/**
 * Read a quantity, preferring the authoritative node and falling back to a peer echo.
 *
 * The order is the point. `od_sensor` owns dissolved oxygen; the aerator merely repeats
 * what it received. If both are available, the owner wins regardless of which value looks
 * more plausible.
 */
export function readQuantity(
  registry: Registry | null,
  snapshot: LiveSnapshot | null,
  quantity: string,
): Reading {
  if (!registry) return EMPTY_READING;

  // The role name is the *key* in the registry record; `RoleDef` itself carries no `role`
  // field, so the entry has to be kept together with its key.
  const ownerEntry = Object.entries(registry.roles).find(([, def]) =>
    def.authoritative_for.includes(quantity),
  );
  if (!ownerEntry) {
    return { ...EMPTY_READING, fault: `no role is authoritative for ${quantity}` };
  }
  const [ownerRoleName, ownerRoleDef] = ownerEntry;

  const fieldName = QUANTITY_FIELD[quantity];
  if (!fieldName) {
    return { ...EMPTY_READING, fault: `no field mapped for ${quantity}` };
  }

  const field = ownerRoleDef.fields.find((f) => f.name === fieldName);
  if (!field) {
    return {
      ...EMPTY_READING,
      fault: `${ownerRoleName} has no field ${fieldName} (registry changed?)`,
    };
  }

  const owner = snapshot?.nodes?.[ownerRoleName] ?? null;
  const owned = owner?.metrics?.[fieldName];
  if (typeof owned === 'number') {
    return {
      value: owned,
      field,
      role: ownerRoleName,
      node: owner,
      source: 'authoritative',
      stale: nodeStale(owner, ownerRoleDef),
      missing: false,
      fault: null,
    };
  }

  // Fall back to an echo, but say so.
  const peerField = QUANTITY_PEER_FIELD[quantity];
  if (peerField) {
    for (const [role, node] of Object.entries(snapshot?.nodes ?? {})) {
      const echoed = node.metrics?.[peerField];
      if (typeof echoed !== 'number') continue;
      const def = registry.roles[role];
      return {
        value: echoed,
        field: def?.fields.find((f) => f.name === peerField) ?? field,
        role,
        node,
        source: 'peer',
        stale: nodeStale(node, def),
        missing: false,
        fault: null,
      };
    }
  }

  return { ...EMPTY_READING, field, role: ownerRoleName, node: owner };
}

// ---------------------------------------------------------------------------
// Chart catalogue
// ---------------------------------------------------------------------------

export interface ChartMetric {
  role: string;
  field: string;
  /** Aggregate suffix, e.g. `avg`. */
  agg: string;
  /** Column name the API expects, e.g. `od_do_mg_l_avg`. */
  column: string;
  label: string;
  unit: string | null;
  precision: number | null;
}

/**
 * Every metric that can actually be charted.
 *
 * Built from the per-field `aggregates` the gateway derives from its own aggregate specs,
 * so it is impossible to offer a series the database cannot answer. A field with an empty
 * `aggregates` array is simply not chartable — status words and setpoints, mostly.
 */
export function chartMetrics(registry: Registry | null): ChartMetric[] {
  if (!registry) return [];
  const out: ChartMetric[] = [];
  for (const [role, def] of Object.entries(registry.roles)) {
    for (const field of def.fields) {
      for (const agg of field.aggregates) {
        out.push({
          role,
          field: field.name,
          agg,
          column: `${field.name}_${agg}`,
          label: field.name,
          unit: field.unit,
          precision: field.precision,
        });
      }
    }
  }
  return out;
}

/** The chart shown on first load: dissolved oxygen, averaged. */
export function defaultChartMetric(registry: Registry | null): ChartMetric | null {
  const all = chartMetrics(registry);
  return (
    all.find((m) => m.field === 'od_do_mg_l' && m.agg === 'avg') ??
    all.find((m) => m.agg === 'avg') ??
    all[0] ??
    null
  );
}

/** Human label for an aggregate suffix, e.g. `pct` -> `% of time`. */
export function describeAgg(agg: string): string {
  switch (agg) {
    case 'avg':
      return 'mean';
    case 'min':
      return 'min';
    case 'max':
      return 'max';
    case 'pct':
      return '% of time';
    case 'sum':
      return 'total';
    default:
      return agg;
  }
}

// ---------------------------------------------------------------------------
// Display helpers derived from role metadata
// ---------------------------------------------------------------------------

export interface RoleDisplay {
  role: string;
  label: string;
  nodeId: string;
  authoritativeFor: string[];
  /** A stable per-role accent so the same node keeps the same colour across panels. */
  accent: 'cyan' | 'blue' | 'teal' | 'amber' | 'violet' | 'rose';
  cadenceLabel: string;
}

const ACCENTS: RoleDisplay['accent'][] = ['cyan', 'blue', 'teal', 'amber', 'violet', 'rose'];

export function roleDisplays(registry: Registry | null): RoleDisplay[] {
  if (!registry) return [];
  return Object.entries(registry.roles).map(([role, def], index) => {
    const experiment = def.expected_rate_hz?.experiment;
    const idle = def.expected_rate_hz?.idle;
    const cadenceLabel =
      experiment && idle
        ? `${formatHz(experiment)} in-run · ${formatHz(idle)} idle`
        : 'cadence not declared';
    return {
      role,
      label: def.display_name,
      nodeId: def.node_id,
      authoritativeFor: def.authoritative_for,
      accent: ACCENTS[index % ACCENTS.length],
      cadenceLabel,
    };
  });
}

/**
 * Render a rate as "5 Hz" or "60 s", whichever reads more naturally.
 *
 * Exported because the presence card prints the *measured* rate beside the declared one,
 * and the two must be formatted identically or they cannot be compared at a glance.
 */
export function formatHz(hz: number): string {
  if (hz >= 1) return `${hz} Hz`;
  const periodS = 1 / hz;
  if (periodS < 90) return `${periodS.toFixed(0)} s`;
  return `${(periodS / 60).toFixed(1)} min`;
}

// ---------------------------------------------------------------------------
// KPI reads (WS3)
//
// The ribbon needs a handful of specific fields, each with its unit and precision so the
// component never restates either. These are the same reads `MetricCards` does, generalised
// so the ribbon and any future panel share one implementation.
// ---------------------------------------------------------------------------

export interface FieldReading {
  value: number | null;
  field: FieldDef | null;
  role: string | null;
  stale: boolean;
  /** True when the role is absent from the snapshot entirely. */
  missing: boolean;
}

/** Read one numeric field from one role, with its definition for formatting. */
export function readField(
  registry: Registry | null,
  snapshot: LiveSnapshot | null,
  role: string,
  field: string,
): FieldReading {
  const def = registry?.roles?.[role];
  const node = snapshot?.nodes?.[role] ?? null;
  const fieldDef = def?.fields.find((f) => f.name === field) ?? null;
  let raw = node?.metrics?.[field];
  if (raw === undefined || raw === null) {
    const legacyKey = (fieldDef as Record<string, unknown> | null)?.legacy_source as string | undefined;
    if (legacyKey && node?.metrics?.[legacyKey] !== undefined) {
      raw = node?.metrics?.[legacyKey];
    } else if (field === 'drv_rpm' && node?.metrics?.['actual_rpm'] !== undefined) {
      raw = node?.metrics?.['actual_rpm'];
    } else if (field === 'drv_rpm_target' && node?.metrics?.['target_rpm'] !== undefined) {
      raw = node?.metrics?.['target_rpm'];
    }
  }
  const value = typeof raw === 'number' ? raw : null;
  return {
    value,
    field: fieldDef,
    role,
    stale: nodeStale(node, def),
    missing: node === null,
  };
}

/**
 * The aerator's headline numbers, read through the registry so units come from one place.
 *
 * RPM is the measured value; the target is separate and never substituted for it. A stopped
 * motor reporting `drv_rpm 0` against a setpoint of 240 is exactly the distinction an
 * operator needs, so the two are read independently.
 */
export interface MotorReading {
  rpm: FieldReading;
  rpmTarget: FieldReading;
  powerW: FieldReading;
  currentA: FieldReading;
  running: boolean | null;
  stale: boolean;
}

export function readMotor(registry: Registry | null, snapshot: LiveSnapshot | null): MotorReading {
  const node = snapshot?.nodes?.['aerator'] ?? null;
  const runRaw = node?.metrics?.['drv_running'];
  const running =
    runRaw === true || runRaw === 1 || runRaw === 'true'
      ? true
      : runRaw === false || runRaw === 0 || runRaw === 'false'
        ? false
        : null;
  return {
    rpm: readField(registry, snapshot, 'aerator', 'drv_rpm'),
    rpmTarget: readField(registry, snapshot, 'aerator', 'drv_rpm_target'),
    powerW: readField(registry, snapshot, 'aerator', 'drv_power_w'),
    currentA: readField(registry, snapshot, 'aerator', 'drv_ibus_a'),
    running,
    stale: nodeStale(node, registry?.roles?.['aerator']),
  };
}

/**
 * Bulk-supply reading for the DO card.
 *
 * Saturation percentage is a *field*, not a derived quantity: the sensor reports it directly
 * (`od_sat_pct`), so computing it from DO over an assumed solubility would replace a
 * measurement with an estimate. The empirical `C*` (~7.15 mg/L) is offered separately as a
 * display reference only, and is labelled as such.
 */
export interface DoReading extends Reading {
  saturationPct: FieldReading;
  batteryV: FieldReading;
  temperature: FieldReading;
}

export function readDoDetail(
  registry: Registry | null,
  snapshot: LiveSnapshot | null,
): DoReading {
  return {
    ...readQuantity(registry, snapshot, QUANTITIES.dissolvedOxygen),
    saturationPct: readField(registry, snapshot, 'od_sensor', 'od_sat_pct'),
    batteryV: readField(registry, snapshot, 'od_sensor', 'pwr_battery_v'),
    temperature: readField(registry, snapshot, 'od_sensor', 'od_temp_c'),
  };
}
