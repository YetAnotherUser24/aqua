/**
 * Configuración del banco de pruebas virtual (AquaControl).
 *
 * Simula la FLOTA de nodos (sensor, ODrive, mixer, bomba) contra la API web real, sin hardware.
 * No simula la física de la planta ni el algoritmo de control: eso vive en el firmware y se valida
 * con `test_control_engine.cpp` o en el banco físico. Aquí se valida el CONTRATO de orquestación:
 * qué recibe cada nodo, qué envía, y si eso llega a la base de datos.
 */
const fs = require('fs');
const path = require('path');

const REPO_WEB = path.join(__dirname, '..', '..');
const REPO_ROOT = path.join(REPO_WEB, '..');

/** Objetivos admitidos. `local` asume `npm run dev` en el puerto 3000. */
const TARGETS = {
  prod: 'https://tesisutec.vercel.app',
  local: 'http://localhost:3000'
};

/**
 * Identidad de los nodos.
 * DEBE coincidir con `src/lib/deviceRoles.ts` y con `config.h` de cada firmware: si divergen,
 * el test deja de representar al nodo real y no valida nada.
 */
const DEVICES = {
  sensor: { id: 'a0000000-0000-0000-0000-000000000001', key: 'ESP32_OD_SENSOR' },
  odrive: { id: 'b0000000-0000-0000-0000-000000000002', key: 'ESP32_ODRIVE' },
  mixer:  { id: 'c0000000-0000-0000-0000-000000000003', key: 'ESP32_T_200' },
  pump:   { id: 'd0000000-0000-0000-0000-000000000004', key: 'ESP32_PUMP' }
};

/** Cadencias reales del firmware. No son negociables: el test las verifica. */
const CADENCE = {
  sensorSampleMs: 5000,   // 0.2 Hz
  sensorBulkFlushMs: 5000,
  odriveSampleMs: 200,    // 5 Hz
  odriveBulkFlushMs: 5000,
  commandPollMs: 1500,
  idleHeartbeatMs: 20000
};

/** Tablas que el banco puede escribir, para limpiar por rango de id. */
const WRITTEN_TABLES = [
  'control_commands',
  'sensor_readings',
  'motor_telemetry',
  'sensor_telemetry_bulk',
  'odrive_telemetry_bulk',
  'mixer_telemetry',
  'pump_telemetry',
  'mixer_events',
  'pump_events',
  'manual_overrides_log'
];

/** Lee un `.env` con formato KEY=VALUE sin dependencias. */
function readEnvFile(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    if (line.trim().startsWith('#')) continue;
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, '').trim();
  }
  return out;
}

/**
 * Resuelve credenciales de Supabase para verificar y limpiar datos.
 * Orden: variables de entorno -> `.env.local` -> `.env` raíz.
 * Sin credenciales, el banco sigue funcionando: solo omite la verificación de persistencia.
 */
function resolveSupabaseCreds() {
  const candidates = [
    { url: process.env.SUPABASE_URL, key: process.env.SUPABASE_SECRET_KEY },
    (() => {
      const e = readEnvFile(path.join(REPO_WEB, '.env.local'));
      return { url: e.NEXT_PUBLIC_SUPABASE_URL, key: e.SUPABASE_SERVICE_ROLE_KEY };
    })(),
    (() => {
      const e = readEnvFile(path.join(REPO_ROOT, '.env'));
      return { url: e.SUPABASE_URL, key: e.SUPABASE_SECRET_KEY };
    })()
  ];

  for (const c of candidates) {
    // Se descartan los placeholders del `.env.local` de desarrollo.
    if (c.url && c.key && /^https?:\/\//.test(c.url) && !/placeholder/i.test(c.key)) {
      return { url: c.url.replace(/\/$/, ''), key: c.key };
    }
  }
  return null;
}

function parseArgs(argv) {
  const args = {
    scenario: 'all',
    target: process.env.BENCH_TARGET || 'prod',
    dryRun: false,
    keepData: false,
    force: false,
    verbose: false,
    settleMs: 9000,
    timeAcceleration: Number(process.env.BENCH_TIME_SCALE || 1),
    initialDoMgL: undefined
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--scenario') args.scenario = argv[++i];
    else if (a === '--target') args.target = argv[++i];
    else if (a === '--base-url') args.baseUrl = argv[++i];
    else if (a === '--settle-ms') args.settleMs = Number(argv[++i]);
    else if (a === '--time-scale') args.timeAcceleration = Number(argv[++i]);
    else if (a === '--initial-do') args.initialDoMgL = Number(argv[++i]);
    else if (a === '--dry-run') args.dryRun = true;
    else if (a === '--keep-data') args.keepData = true;
    else if (a === '--force') args.force = true;
    else if (a === '--verbose') args.verbose = true;
    else if (a === '--help' || a === '-h') args.help = true;
  }
  args.baseUrl = args.baseUrl || TARGETS[args.target] || args.target;
  return args;
}

module.exports = {
  TARGETS,
  DEVICES,
  CADENCE,
  WRITTEN_TABLES,
  resolveSupabaseCreds,
  parseArgs,
  readEnvFile,
  REPO_WEB,
  REPO_ROOT
};
