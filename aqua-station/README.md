# aqua-station

Self-hosted monitoring, control and archival stack for the AquaControl aquaculture
aeration testbed. Replaces the previous **Vercel + Supabase** deployment with a single
Raspberry Pi 3B+ on the local network.

## Why this exists

The previous architecture served a constant, low-rate, always-on telemetry workload on a
**scale-to-zero, per-CPU-metered** platform. Vercel's Hobby tier allots **4 hours of Active
CPU per month**; the estimate was ~17 CPU-hours. Two structural causes:

1. **Pull-based commands.** `nodo-aerador` polled `GET /api/commands/pending` every 1–1.5 s,
   24/7 — ~2,400–3,600 requests/hour, **77% of all traffic** — and nearly every response was
   `has_command: false`.
2. **Work on every request.** Each invocation ran a fresh serverless function: TLS handshake,
   SHA-256 device auth, and a `devices.upsert()` **write on every single request**.

Neither is a bug. Both are consequences of running an always-on control workload on the wrong
kind of platform. This repo moves it onto one box that is always on.

## Design in one picture

```
ESP32 nodes ──MQTT──> Mosquitto ──> aqua-gateway ──┬──> live cache ──SSE 1 Hz──> Caddy ──> Cloudflare Tunnel ──> https://aqua-station.live
 (4 roles)            (LAN)         (Python)       ├──> CSV archive ──> /data/telemetry
                                                   ├──> 1-min rollups ──> PostgreSQL
                                                   └──> unified event stream
                                                                    │
                                                post-run / daily R2 │
                                                                    v
                                               Cloudflare R2 Edge Storage (raw/*.csv.gz,
                                                exports/*.tar.gz) ──> https://data.aqua-station.live
```

**Notable properties**

- **Zero HTTP requests from nodes.** MQTT replaces polling; data volume is similar, but the
  per-request fixed cost (handshake, auth hash, DB upsert, cold start) disappears entirely.
- **The control loop never touches this stack.** PID runs on-node at 20 Hz; nodes are
  peer-to-peer over ESP-NOW. This system is supervisory only, so network latency cannot affect
  control.
- **Two logging tiers.** PostgreSQL holds operational state and 1-minute rollups; full-fidelity
  telemetry lives in append-only CSV files. A relational row costs ~2.4× the bytes and dirties
  an 8 KB page per write; a CSV append is sequential and near-free.
- **Flush-aware retention.** Local files are only pruned once the archive is verified to hold
  them. A node or the Pi being offline for weeks never causes data loss.
- **Heterogeneous by construction.** The four node roles share almost no fields, so schemas are
  declared data (`gateway/aqua_gateway/schema/roles.yaml`), not code.

## Layout

| Path | What |
|---|---|
| `docs/` | Architecture, protocol contract, event taxonomy, data formats |
| `gateway/` | The Python service: MQTT ingest, archive, rollups, events, API, backup |
| `web/` | Dashboard: static export, registry-driven, served by Caddy |
| `deploy/` | systemd units, Caddy/Mosquitto/Postgres config, install script |
| `firmware-patches/` | MQTT-migration patches for the node repos (kept separate) |
| `tools/` | Experiment export, MQTT simulator, mock gateway, verification, analysis |

## Start here

- **`docs/PROTOCOL.md`** — the contract every node implements. Read this first.
- **`docs/ARCHITECTURE.md`** — components, data flow, resource budget.
- **`docs/DEPLOYMENT.md`** — host preparation and install.
- **`docs/RUNBOOK.md`** — what to do when something breaks.
- **`docs/TEST-PLAN.md`** — the end-to-end procedure: every button, command and component,
  against a real node. Read before the first bench session.

## Status

**Gateway implemented and tested; dashboard ported and refactored.**

Done and verified

- `gateway/aqua_gateway/` — all 33 modules: MQTT ingest, CSV/gzip archive tier, verified flush
  to object storage, rollups, unified event stream, derived rules with hysteresis, command
  outbox with atomic supersession, export builder, HTTP API. **324 unit tests pass**
  (`cd gateway && python -m pytest -q`).
- `deploy/` — five systemd units, Caddy, Mosquitto, tuned PostgreSQL, volatile journald.
- `web/` — the dashboard, rewritten against the gateway. One SSE connection replaces four
  polling endpoints; every card is rendered from `roles.yaml`. Builds to a ~1.3 MB static
  export; `tools/mock-gateway.py` runs it without a broker or database.
- `POST /api/experiments` — implemented, so `start_experiment` is reachable through the API.
  Before this, `control_commands.experiment_id` had a foreign key into a table nothing could
  insert into, so every attempt returned a bare 500.
- `POST /api/state` (`pause` / `resume` / `confirm`) and
  `POST /api/state/phase-mode?phase_id=…&mode=…` — the orchestrator's MANUAL mode waits for an
  operator confirm instead of advancing on its own.
- `firmware-patches/` — all four `.patch` files land the MQTT migration.
- `POST /api/experiments/{id}/download` — export bundles (aligned CSV + manifest) and native high-frequency bundles are directly downloadable over HTTP and via the UI export modal.
- Automated post-experiment cloud sync — `_auto_export_and_upload()` in `engine.py` builds export bundles and uploads them to Cloudflare R2 via `aqua-r2-sync` as soon as a run finishes.
- 24/7 Cloudflare R2 edge data portal (`https://data.aqua-station.live/index.html`) — interactive multi-axis charting (DO, RPM, Temp, Mixer) and one-click downloads available even when the bench Pi is offline.
- Cloudflare Tunnel (`https://aqua-station.live`) — zero-port-forwarding secure remote access to live SCADA dashboard and SSE telemetry stream.

Not done yet

- No web test framework — `npm run typecheck && npm run build` is the gate. Component
  behaviour is verified by the manual table in `docs/TEST-PLAN.md` §A2.

See `docs/ARCHITECTURE.md` for the build order and `docs/DEPLOYMENT.md` before touching the Pi.

## Related repositories (unchanged, not part of this repo)

- `nodo-aerador` — ODrive S1 aerator, ESP-NOW peer
- `nodo-sensor-od` — optical DO probe + DS3231 RTC (deployment time authority)
- `nodo-mezclador` — T-200 / SNR8503 mixer
- `nodo-bomba` — peristaltic dosing pump

Firmware stays in place. `firmware-patches/` holds the reviewable diffs that adopt this
protocol.
