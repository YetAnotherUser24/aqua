/**
 * Verificación del estado real del sistema: tablas, órdenes, dispositivos, telemetría y roles.
 *
 * Transporte por defecto: **PostgREST** (`SUPABASE_URL` + `SUPABASE_SECRET_KEY` del `.env`), que
 * cubre toda la verificación de datos. Para SQL libre existe `db.js sql`, que requiere una cadena
 * válida en `DATABASE_URL` (ver la nota del comando `sql`).
 *
 * Uso:
 *   node scripts/db.js audit             # contrato completo: tablas, columnas clave y roles
 *   node scripts/db.js tables            # inventario con conteo de filas
 *   node scripts/db.js devices           # tipo, rol y estado de cada nodo
 *   node scripts/db.js roles             # coherencia rol ↔ tipo ↔ tabla de archivo
 *   node scripts/db.js commands [n]      # últimas n órdenes, con payload resuelto
 *   node scripts/db.js telemetry [n]     # últimas n filas por tabla de telemetría
 *   node scripts/db.js recent [min]      # actividad reciente por tabla
 *   node scripts/db.js sql "SELECT ..."  # (requiere DATABASE_URL)
 *
 * NUNCA imprime claves ni cadenas de conexión.
 */
const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.join(__dirname, '..', '..');
const REST_PREFIX = '/rest/v1';

/** Lee el `.env` raíz: mapa de claves y líneas útiles (sin comentarios). */
function readRootEnv() {
  const file = path.join(REPO_ROOT, '.env');
  const map = {};
  const lines = [];
  if (!fs.existsSync(file)) return { map, lines };
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    if (!raw.trim() || raw.trim().startsWith('#')) continue;
    lines.push(raw.trim());
    const m = raw.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (m) map[m[1]] = m[2].replace(/^["']|["']$/g, '').trim();
  }
  return { map, lines };
}

const { map: ENV, lines: ENV_LINES } = readRootEnv();
const URL_BASE = (process.env.SUPABASE_URL || ENV.SUPABASE_URL || '').replace(/\/$/, '');
const KEY = process.env.SUPABASE_SECRET_KEY || ENV.SUPABASE_SECRET_KEY || '';

if (!URL_BASE || !KEY) {
  console.error('Faltan SUPABASE_URL / SUPABASE_SECRET_KEY en el entorno o en el .env raíz.');
  process.exit(2);
}
const HEADERS = { apikey: KEY, Authorization: `Bearer ${KEY}` };

/** GET de PostgREST. */
async function rest(table, queryStr = '') {
  const url = `${URL_BASE}${REST_PREFIX}/${table}${queryStr ? '?' + queryStr : ''}`;
  const res = await fetch(url, { headers: { ...HEADERS, Prefer: 'count=exact' } });
  const text = await res.text();
  let rows = null;
  try { rows = text ? JSON.parse(text) : []; } catch { rows = null; }
  return {
    status: res.status,
    ok: res.ok,
    rows: Array.isArray(rows) ? rows : null,
    error: res.ok ? null : (rows?.message || text.slice(0, 160))
  };
}

/** Conteo exacto de una tabla; `null` si no existe. */
async function count(table) {
  const res = await fetch(`${URL_BASE}${REST_PREFIX}/${table}?select=*&limit=1`, {
    headers: { ...HEADERS, Prefer: 'count=exact' }
  });
  if (res.status === 404) return null;
  const cr = res.headers.get('content-range');
  if (!cr) return 0;
  const total = cr.split('/')[1];
  return total === '*' ? 0 : Number(total);
}

/** Columnas de una tabla: se deducen de una fila; si está vacía, se sondean candidatas. */
async function columns(table) {
  const res = await rest(table, 'select=*&limit=1');
  if (res.ok && res.rows && res.rows.length > 0) return Object.keys(res.rows[0]);

  const probes = ['id', 'created_at', 'recorded_at', 'device_id', 'experiment_id', 'payload_json',
    'rpm', 'target_rpm', 'rtc_timestamp_ms', 'status', 'event_type', 'volume_ml', 'target_ml'];
  const found = [];
  for (const p of probes) {
    if ((await rest(table, `select=${p}&limit=1`)).ok) found.push(p);
  }
  return found;
}

/** Payload de una orden: columna `payload` o el JSON serializado en `error_message`. */
function resolvePayload(row) {
  if (row.payload && typeof row.payload === 'object' && Object.keys(row.payload).length > 0) return row.payload;
  const raw = row.error_message;
  if (typeof raw === 'string' && raw.trim().startsWith('{')) {
    try { return JSON.parse(raw); } catch { /* no es JSON */ }
  }
  return null;
}

function fmtPayload(p) {
  if (!p) return '—';
  const shown = ['state', 'target_role', 'mixer', 'motor_mode', 'motor_throttle_pct',
    'motor_target_do', 'mode', 'target_do', 'manual_throttle_pct', 'executed_rtc_ms',
    'clock_skew_ms', 'ack_latency_ms'];
  const extras = shown.filter(k => p[k] !== undefined).map(k => `${k}=${p[k]}`).join(' ');
  return `${p.action || '?'}${extras ? '  ' + extras : ''}`;
}

const ALL_TABLES = [
  'devices', 'sensor_readings', 'motor_telemetry', 'control_commands', 'system_settings', 'alerts',
  'sensor_telemetry_bulk', 'odrive_telemetry_bulk', 'mixer_telemetry', 'pump_telemetry',
  'mixer_events', 'pump_events', 'manual_overrides_log'
];

/** Contrato esperado: rol ↔ device_id ↔ devices.type ↔ tabla de archivo. */
const EXPECTED_BY_ROLE = {
  sensor: { type: 'sensor_do', archive: 'sensor_telemetry_bulk', id: 'a0000000-0000-0000-0000-000000000001' },
  odrive: { type: 'aerator_motor', archive: 'odrive_telemetry_bulk', id: 'b0000000-0000-0000-0000-000000000002' },
  mixer: { type: 'mixer', archive: 'mixer_telemetry', id: 'c0000000-0000-0000-0000-000000000003' },
  pump: { type: 'dosing_pump', archive: 'pump_telemetry', id: 'd0000000-0000-0000-0000-000000000004' }
};

const args = process.argv.slice(2);
const cmd = args[0] || 'audit';

(async () => {
  switch (cmd) {
    case 'tables': {
      console.log('=== Inventario del esquema public ===');
      for (const t of ALL_TABLES) {
        const c = await count(t);
        console.log(`  ${c !== null ? 'OK   ' : 'FALTA'} ${t.padEnd(24)} ${c !== null ? String(c).padStart(6) + ' filas' : ''}`);
      }
      break;
    }

    case 'devices': {
      const res = await rest('devices', 'select=id,name,type,status,last_seen_at,metadata&order=id');
      console.log('=== Dispositivos ===');
      for (const d of res.rows || []) {
        const role = d.metadata?.role || 'AUSENTE';
        console.log(`  ${String(d.type).padEnd(16)} rol=${String(role).padEnd(8)} ${String(d.status).padEnd(9)} ${String(d.last_seen_at || '').slice(0, 19)}  ${d.name}`);
      }
      break;
    }

    case 'roles': {
      console.log('=== Coherencia rol ↔ tipo ↔ tabla de archivo ===');
      const devRes = await rest('devices', 'select=id,type,metadata');
      const byId = {};
      for (const d of devRes.rows || []) byId[d.id] = d;
      let bad = 0;
      for (const [role, exp] of Object.entries(EXPECTED_BY_ROLE)) {
        const d = byId[exp.id];
        if (!d) { console.log(`  FALTA el dispositivo del rol ${role}`); bad++; continue; }
        const typeOk = d.type === exp.type;
        const roleOk = d.metadata?.role === role;
        const archiveCount = await count(exp.archive);
        const archiveOk = archiveCount !== null;
        if (!typeOk || !roleOk || !archiveOk) bad++;
        console.log(`  ${role.padEnd(8)} tipo=${String(d.type).padEnd(15)}${typeOk ? 'OK ' : 'MAL'} metadata.role=${String(d.metadata?.role || 'AUSENTE').padEnd(8)}${roleOk ? 'OK ' : 'MAL'} archivo ${exp.archive.padEnd(22)}${archiveOk ? archiveCount + ' filas' : 'FALTA'}`);
      }
      console.log(`\n  ${bad === 0 ? 'Coherencia completa' : bad + ' incoherencias'}`);
      break;
    }

    case 'commands': {
      const n = Number(args[1] || 15);
      const res = await rest('control_commands',
        `select=id,device_id,command_type,speed_percent,status,requested_by,created_at,sent_at,executed_at,error_message&order=created_at.desc&limit=${n}`);
      const byId = {};
      for (const [role, exp] of Object.entries(EXPECTED_BY_ROLE)) byId[exp.id] = role;
      console.log(`=== Últimas ${(res.rows || []).length} órdenes ===`);
      for (const r of res.rows || []) {
        const label = byId[r.device_id] || (r.device_id ? String(r.device_id).slice(0, 8) : 'global');
        console.log(`  ${String(r.created_at).slice(11, 19)} ${String(r.status).padEnd(13)} ${String(r.command_type).padEnd(11)} ${String(label).padEnd(8)} ${fmtPayload(resolvePayload(r))}`);
      }
      const pend = await rest('control_commands', 'select=id&status=eq.pending');
      console.log(`\n  pendientes: ${(pend.rows || []).length}`);
      const byStatus = [];
      for (const s of ['pending', 'sent', 'acknowledged', 'failed', 'expired']) {
        const r = await rest('control_commands', `select=id&status=eq.${s}`);
        if ((r.rows || []).length) byStatus.push(`${s}=${r.rows.length}`);
      }
      console.log('  por estado: ' + byStatus.join(' '));
      break;
    }

    case 'telemetry': {
      const n = Number(args[1] || 2);
      for (const t of ['sensor_readings', 'motor_telemetry', 'sensor_telemetry_bulk',
        'odrive_telemetry_bulk', 'mixer_telemetry', 'pump_telemetry', 'mixer_events', 'pump_events']) {
        const res = await rest(t, `select=*&order=id.desc&limit=${n}`);
        const c = await count(t);
        if (!res.ok) { console.log(`\n=== ${t} -> HTTP ${res.status} (${res.error})`); continue; }
        console.log(`\n=== ${t} (${c} filas) ===`);
        const cols = res.rows && res.rows.length ? Object.keys(res.rows[0]) : await columns(t);
        if (cols.length) console.log('  columnas: ' + cols.join(', '));
        for (const r of res.rows || []) {
          const brief = Object.entries(r)
            .filter(([k]) => k !== 'payload_json')
            .map(([k, v]) => `${k}=${v === null ? 'null' : (typeof v === 'object' ? '{}' : String(v).slice(0, 20))}`)
            .join(' ');
          console.log('  ' + brief);
        }
      }
      break;
    }

    case 'recent': {
      const min = Number(args[1] || 60);
      const since = new Date(Date.now() - min * 60000).toISOString();
      console.log(`=== Actividad desde ${since.slice(11, 19)} UTC (últimos ${min} min) ===`);
      for (const t of ALL_TABLES) {
        const cols = await columns(t);
        const tsCol = ['created_at', 'recorded_at', 'started_at', 'updated_at'].find(c => cols.includes(c));
        if (!tsCol) { console.log(`  ${t.padEnd(24)} sin marca temporal`); continue; }
        // Se selecciona la propia marca temporal: no todas las tablas tienen `id`
        // (`system_settings` usa `key` como clave).
        const res = await rest(t, `select=${tsCol}&${tsCol}=gte.${since}`);
        if (!res.ok) { console.log(`  ${t.padEnd(24)} HTTP ${res.status}`); continue; }
        const c = await count(t);
        console.log(`  ${t.padEnd(24)} +${(res.rows || []).length} en ventana / ${c} total`);
      }
      break;
    }

    case 'sql': {
      const statement = args.slice(1).join(' ');
      if (!statement) { console.error('Falta la consulta'); process.exit(2); }
      if (!/^\s*(select|with|table|show|explain)\b/i.test(statement)) {
        console.error('Solo consultas de lectura (SELECT/WITH/TABLE/SHOW/EXPLAIN).');
        process.exit(2);
      }
      let pg;
      try { pg = require('pg'); } catch {
        console.error('Falta el cliente `pg`: npm i -D pg');
        process.exit(2);
      }
      const conn = process.env.DATABASE_URL || ENV_LINES.find(l => /^postgres(ql)?:\/\/\S+$/.test(l));
      if (!conn) { console.error('Falta DATABASE_URL (o la URI postgres:// en el .env).'); process.exit(2); }
      if (/^postgres(ql)?:\/\/[^@]*@db\./.test(conn)) {
        console.error('La URI usa el host directo `db.<ref>.supabase.co`, que ya no resuelve.');
        console.error('Sustitúyela por la del **Session pooler** del panel de Supabase (Connect -> Session pooler).');
        process.exit(2);
      }
      const client = new pg.Client({ connectionString: conn, ssl: { rejectUnauthorized: false } });
      await client.connect();
      try {
        const r = await client.query(statement);
        console.log(JSON.stringify(r.rows, null, 2));
      } finally { await client.end(); }
      break;
    }

    case 'audit': {
      console.log('=== AUDITORÍA DEL CONTRATO DE ESQUEMA ===\n');
      let missing = 0;
      for (const t of ALL_TABLES) {
        const c = await count(t);
        const ok = c !== null;
        if (!ok) missing++;
        console.log(`  ${ok ? 'OK   ' : 'FALTA'} ${t.padEnd(24)} ${ok ? String(c).padStart(6) + ' filas' : ''}`);
      }

      console.log('');
      const cmdCols = await columns('control_commands');
      const hasPayload = cmdCols.includes('payload');
      console.log(`  ${hasPayload ? 'OK   ' : 'NOTA '} control_commands.payload${hasPayload ? '' : ' ausente: el payload viaja en error_message'}`);

      const mtCols = await columns('motor_telemetry');
      console.log(`  ${mtCols.includes('rpm') ? 'OK   ' : 'FALTA'} motor_telemetry.rpm`);
      console.log(`  ${mtCols.includes('target_rpm') ? 'OK   ' : 'FALTA'} motor_telemetry.target_rpm`);

      const devRes = await rest('devices', 'select=id,type,metadata');
      let rolesOk = true;
      for (const [role, exp] of Object.entries(EXPECTED_BY_ROLE)) {
        const d = (devRes.rows || []).find(x => x.id === exp.id);
        if (!d || d.type !== exp.type || d.metadata?.role !== role) rolesOk = false;
      }
      console.log(`  ${rolesOk ? 'OK   ' : 'INCOHERENTE'} roles de dispositivo (devices.type + metadata.role)`);
      if (!rolesOk) {
        for (const d of devRes.rows || []) {
          console.log(`        ${String(d.type).padEnd(16)} role=${d.metadata?.role || 'AUSENTE'}  ${d.id.slice(0, 8)}`);
        }
      }

      console.log(`\n  ${missing === 0 ? 'Todos los objetos del contrato existen' : missing + ' objetos faltantes'}`);
      break;
    }

    default:
      console.log(`Comando desconocido: ${cmd}
  audit | tables | devices | roles | commands [n] | telemetry [n] | recent [min] | sql "SELECT ..."`);
      process.exit(2);
  }
})().catch(err => {
  console.error('Error:', err.message);
  process.exit(1);
});
