# web/ — dashboard

Static-exported Next.js build of the AquaControl dashboard. **Served by Caddy on the Pi; the
browser does all rendering.** No Node process exists in the deployment at all — worth roughly
150–250 MB on a 905 MB machine. The build output is ~1.3 MB of HTML/CSS/JS.

## What changed from `Tesis-webpage/`

The old app was both the UI *and* the API gateway: 23 Next route handlers talking to Supabase
over PostgREST. It is now split, and this directory is only the UI.

| Was | Now |
|---|---|
| `src/app/api/**` — 23 route handlers | `gateway/` (Python, FastAPI) |
| Supabase PostgREST via `supabase-js` | PostgreSQL + CSV/gzip archive behind the gateway |
| 4 endpoints polled every 4–15 s per tab | **one** SSE connection at 1 Hz |
| Deployed on Vercel | static files on the Pi, served by Caddy |

Up to **3,600 requests/hour per open tab** are gone. That polling was the direct cause of the
Vercel Active CPU overrun.

## Layout

| Path | Purpose |
|---|---|
| `src/app/page.tsx` | composition root — the whole page in one place |
| `src/app/layout.tsx`, `globals.css` | shell and the glassmorphism styles (kept from the old app) |
| `src/lib/types.ts` | wire types, mirroring the gateway exactly |
| `src/lib/api.ts` | typed client; keeps "failed" distinct from "empty" |
| `src/lib/registry.ts` | registry-driven selection — the only place a field name is written down |
| `src/lib/format.ts` | precision-aware formatting, driven by the schema |
| `src/lib/dateUtils.ts` | all timestamps rendered in America/Lima, never the ambient zone |
| `src/hooks/useLive.ts` | SSE subscription with staleness watchdog and polling fallback |
| `src/hooks/useAsync.ts` | request/response loading with an explicit reload |
| `src/components/*` | panels; `ui.tsx` is the shared design system |

## The one architectural idea

**The UI renders from the registry, not from hardcoded field names.**

`GET /api/roles` serves `gateway/aqua_gateway/schema/roles.yaml`. Every card finds its own
headline metric, its own run flag and its own units from the field definitions, and the chart
picker is built from the per-field `aggregates` the gateway derives from its own aggregate
specs. So:

* a fifth node type is a `roles.yaml` edit, not a frontend change;
* it is impossible to offer a chart series that no database column backs;
* renaming a field breaks at one point (`QUANTITY_FIELD` in `lib/registry.ts`) rather than in a
  dozen JSX expressions.

That last point is the one wart. `roles.yaml` declares
`authoritative_for: [dissolved_oxygen, water_temperature]` on `od_sensor` but never says *which
field* carries each quantity, so the mapping cannot be derived. `readQuantity()` therefore
validates the name against the registry on every read and shows an explicit fault when it is
missing, so the failure is loud rather than a blank card. Adding a `primary_field:` next to
each `authoritative_for` entry would remove the constant entirely.

## Three states the UI insists on distinguishing

1. **Failed ≠ empty.** The gateway returns `503` with a reason rather than `[]`, because an
   empty array is a claim that nothing happened. `ErrorState` renders unreachable, degraded and
   failed as three different things.
2. **Stale ≠ current.** A node marked `online` by MQTT Last Will & Testament can still be
   wedged and silent, so presence is checked *against the declared cadence* for the role. A
   reading that is old is labelled old.
3. **Authoritative ≠ echoed.** `peer_*` fields are another node's measurement. They render in
   their own muted block and are never averaged with the `od_*` source.

## Developing

Requires no broker, no database and no Pi:

```bash
npm install                    # one-time

# terminal 1 — mock gateway: the real registry, a simulated pond
python3 ../tools/mock-gateway.py --root out --port 8899

# terminal 2
npm run build                  # -> out/
# then open http://localhost:8899
```

The harness loads `roles.yaml` and `events/derived.py` from the gateway package, so field names,
units, precisions and thresholds are real even though the values are simulated. A schema rename
breaks the harness exactly as it would break production.

Because `output: 'export'` leaves no server to proxy `/api`, hot reload needs the harness on
the same origin; build-and-serve is the reliable path.

### Gates

```bash
npm run typecheck   # tsc --noEmit — the build fails on type errors
npm run build       # -> out/
```

Type errors fail the build deliberately. A dashboard that renders a stale field name as
`undefined` is worse than one that refuses to compile, because it looks plausible.

## Deploying

Build on a **laptop, never on the Pi** — a Next.js build on 905 MB will OOM.

```bash
npm ci && npm run build   # -> out/
```

Then copy `out/` to `/srv/aqua-web/` on the Pi, or re-run `deploy/install.sh`, which rsyncs it
when present.

`deploy/caddy/Caddyfile` serves that directory and proxies `/api/*`, `/api/stream` and
`/healthz` to the gateway **on the same origin**, which is why every fetch here is a relative
path and CORS is never exercised in production.

Two Caddy details matter more than they look:

* `flush_interval -1` on the `/api/stream` route. Without it Caddy buffers the stream and the
  dashboard appears frozen — the failure that makes people give up and go back to polling.
* The `/api/stream*` route must be declared **before** the generic `/api/*` handler.

`useLive` degrades to polling `GET /api/live` after repeated stream errors, so a misconfigured
proxy yields a working-but-worse dashboard rather than a blank one. It retries the stream
periodically.

## Field naming

Field names changed in schema v2 — `is_on` → `drv_running`, `status_code` → `drv_status`,
`measured_do` → `peer_do_mg_l`, `water_temp` → `peer_temp_c`, `commanded_duty` →
`drv_duty_pct` — and the `*_centi`/`*_deci`/`*_milli` encodings are gone in favour of real
units. See `docs/PROTOCOL.md` §4–5. Nothing in this app writes those names except
`QUANTITY_FIELD`.

## Known gaps

* No browser test suite; the mock gateway is for manual review.
* The export bundle is written on the gateway host and is **not** downloadable from the UI.
  Pointing Caddy's `/downloads/*` at `AQUA_EXPORT_ROOT` would enable that.
* There is no create-experiment endpoint yet, so `ExperimentsPanel` is read-only plus export.

