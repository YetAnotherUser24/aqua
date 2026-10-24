/**
 * Transporte HTTP del banco virtual: cliente de la API web y acceso directo a Supabase
 * (solo para VERIFICAR persistencia y LIMPIAR lo que el banco creó).
 *
 * Sin dependencias: usa `fetch` nativo de Node 18+.
 */
const { WRITTEN_TABLES } = require('./config');

const DEFAULT_TIMEOUT_MS = 15000;

/** POST/GET con timeout. Devuelve `{ status, ok, body, error }` sin lanzar por errores HTTP. */
async function request(url, { method = 'GET', body, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  try {
    const res = await fetch(url, {
      method,
      headers: { ...headers, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs)
    });
    const text = await res.text();
    let parsed = null;
    if (text) {
      try { parsed = JSON.parse(text); } catch { parsed = text; }
    }
    return { status: res.status, ok: res.ok, body: parsed, error: null };
  } catch (err) {
    return { status: 0, ok: false, body: null, error: err.message };
  }
}

/** Cliente de la API web (actúa como lo haría un nodo ESP32 por HTTPS). */
class ApiClient {
  constructor(baseUrl, verbose) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.verbose = verbose;
  }

  /** GET autenticado como dispositivo. */
  deviceGet(pathname, deviceKey) {
    return request(`${this.baseUrl}${pathname}`, { headers: { 'X-Device-Key': deviceKey } });
  }

  /** POST autenticado como dispositivo. */
  devicePost(pathname, deviceKey, body) {
    return request(`${this.baseUrl}${pathname}`, {
      method: 'POST',
      headers: { 'X-Device-Key': deviceKey },
      body
    });
  }

  /** GET/POST sin credencial de dispositivo (endpoints de operador). */
  get(pathname) { return request(`${this.baseUrl}${pathname}`); }
  post(pathname, body) { return request(`${this.baseUrl}${pathname}`, { method: 'POST', body }); }

  log(...args) { if (this.verbose) console.log('   ·', ...args); }
}

/**
 * Acceso directo a Supabase para VERIFICAR y LIMPIAR.
 *
 * La limpieza es por RANGO: se toma el máximo antes de la corrida y al final se borra sólo lo que
 * quedó por encima. Es determinista y no depende de ventanas de tiempo.
 *
 * OJO: `control_commands.id` es un UUID, no un bigint, así que no admite `id > N`. Para esa tabla se
 * usa `created_at` como marca y se restringe el borrado a los 4 dispositivos canónicos, de modo que
 * nunca se tocan filas ajenas al banco.
 */
const { DEVICES } = require('./config');

/** Tablas cuya clave no es numérica: se limpian por marca temporal. */
const TEMPORAL_KEY_TABLES = { control_commands: 'created_at' };

class Store {
  constructor(creds) {
    this.creds = creds;
    this.headers = creds ? { apikey: creds.key, Authorization: `Bearer ${creds.key}` } : null;
    this.missingTables = [];
    this.available = Boolean(creds);
  }

  async maxId(table) {
    if (!this.available) return null;
    const res = await request(`${this.creds.url}/rest/v1/${table}?select=id&order=id.desc&limit=1`, {
      headers: this.headers
    });
    if (res.status === 404) { this.missingTables.push(table); return null; }
    if (!res.ok || !Array.isArray(res.body) || res.body.length === 0) return 0;
    // Si la clave no es numérica (p. ej. UUID) no sirve el rango: se marca como nula.
    const asNumber = Number(res.body[0].id);
    return Number.isFinite(asNumber) ? asNumber : null;
  }

  /** Última marca temporal de una tabla (para claves no numéricas). */
  async maxTimestamp(table, column) {
    if (!this.available) return null;
    const res = await request(`${this.creds.url}/rest/v1/${table}?select=${column}&order=${column}.desc&limit=1`, {
      headers: this.headers
    });
    if (res.status === 404) { this.missingTables.push(table); return null; }
    if (!res.ok || !Array.isArray(res.body) || res.body.length === 0) return null;
    return res.body[0][column];
  }

  /** Consulta PostgREST cruda sobre una tabla (para las aserciones del banco). */
  async query(table, qs) {
    if (!this.available) return null;
    const res = await request(`${this.creds.url}/rest/v1/${table}?${qs}`, { headers: this.headers });
    if (!res.ok || !Array.isArray(res.body)) return null;
    return res.body;
  }

  /** Snapshot de las marcas de todas las tablas que el banco podría escribir. */
  async snapshot() {
    if (!this.available) return null;
    const snap = {};
    for (const t of WRITTEN_TABLES) {
      snap[t] = TEMPORAL_KEY_TABLES[t]
        ? await this.maxTimestamp(t, TEMPORAL_KEY_TABLES[t])
        : await this.maxId(t);
    }
    return snap;
  }

  /** Cuenta filas creadas desde el snapshot (por tabla). */
  async countCreated(table, sinceId) {
    if (!this.available || sinceId === null || sinceId === undefined) return null;
    const res = await request(`${this.creds.url}/rest/v1/${table}?select=id&id=gt.${sinceId}`, {
      headers: { ...this.headers, Prefer: 'count=exact' }
    });
    if (res.status === 404) return null;
    if (!res.ok || !Array.isArray(res.body)) return null;
    return res.body.length;
  }

  /** Borra únicamente las filas que el banco creó durante la corrida. */
  async cleanup(snapshot) {
    if (!this.available || !snapshot) return { deleted: 0, tables: [] };
    let deleted = 0;
    const tables = [];

    for (const t of WRITTEN_TABLES) {
      const marker = snapshot[t];
      if (marker === null || marker === undefined) continue;

      const temporal = TEMPORAL_KEY_TABLES[t];
      let url;
      if (temporal) {
        // Restringido a los nodos del banco para no borrar órdenes ajenas.
        const ids = Object.values(DEVICES).map(d => d.id).join(',');
        url = `${this.creds.url}/rest/v1/${t}?${temporal}=gt.${encodeURIComponent(marker)}&device_id=in.(${ids})`;
      } else {
        url = `${this.creds.url}/rest/v1/${t}?id=gt.${marker}`;
      }

      const res = await request(url, { method: 'DELETE', headers: { ...this.headers, Prefer: 'return=representation' } });
      if (res.ok && Array.isArray(res.body)) {
        if (res.body.length > 0) tables.push(`${t}:${res.body.length}`);
        deleted += res.body.length;
      } else if (res.status !== 404 && !res.ok) {
        console.warn(`   ! Limpieza de ${t} falló (HTTP ${res.status}): ${JSON.stringify(res.body)?.slice(0, 160)}`);
      }
    }
    return { deleted, tables };
  }
}

module.exports = { ApiClient, Store, request };
