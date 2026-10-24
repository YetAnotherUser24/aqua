# AquaControl — pending tasks

**As of 2026-10-10**, verified against the running station rather than from memory.
"Verified" below means the state was read off the Pi, not inferred.

This file lives outside the git repositories on purpose: several items are security
actions, and the repos are pushed to GitHub. Nothing here contains a secret value.

---

## Working state

Everything below is currently up and confirmed working:

| Component | State |
|---|---|
| `aqua-gateway`, `mosquitto`, `postgresql`, `caddy` | active |
| `aqua-rotate.timer`, `aqua-flush.timer` | active |
| Dashboard (`https://aqua-station.local/`) | HTTP 200 |
| Real hardware | aerator only — the sole client connecting from a LAN address |
| Gateway test suite | 324 pass on the Pi (aarch64) and on the dev box |
| New Supabase project `sndfoyinvomdxzeuegmj` | 14 tables + `aqua-archive` bucket |
| mosquitto password file ownership | correct, zero warnings |

**2026-10-10 — an ESP32-S3 has been connected.** Which node is not yet recorded; both
`nodo-aerador` and `nodo-mezclador` build for that board and they share no telemetry or
commands. `docs/TEST-PLAN.md` §0.1 has the bench test to tell them apart, and the plan is
written so either role can be walked through it. **Update item 6 below once the role is
known** — the virtual-then-real build order is in `docs/DEPLOYMENT.md` §4.2.

**2026-10-10 (later) — the board is an `od_sensor`.** Serial banner is
`WELCOME TO od-logger-lora`, `Sensor type: od-digisens`, MQTT user `aqua-sensor-od` — i.e.
the `nodo-sensor-od` firmware on the `virtual-s3` env, which is the **only** ESP32-S3 build
that node has (§0.1). It was reflashed and walked through `docs/TEST-PLAN.md` §A, §B, §C, §D
and §E on the dev box. **Five real defects were found and fixed** (see "Test-plan run" below);
all four commit-trees still need the fixes folded in — they are in the working trees, not
committed, because both repos carried a large pre-existing uncommitted refactor.

---

## Test-plan run — 2026-10-10 (od_sensor on the bench, dev box)

Environment: `od_sensor virtual-s3` on USB, a real Mosquitto on the WSL LAN, the real
gateway + embedded Postgres, Windows-`localhost` relay. Dashboard at `:3111` against the mock
for §A; the rest against the real gateway on `:8000`.

### Defects found and fixed

| # | Where | Symptom | Fix |
|---|---|---|---|
| 1 | `gateway/aqua_gateway/mqtt/client.py` | `AttributeError: 'MqttIngest' object has no attribute '_timezone'` on **every** envelope. `_dispatch` swallows it, so the gateway connected, acked commands and archived **nothing**. No test constructed `MqttIngest` at all. | `self._timezone = ZoneInfo(settings.timezone)`; new `tests/test_ingest_dispatch.py` drives a real envelope through the real handler into a real archive. |
| 2 | `gateway/aqua_gateway/mqtt/envelope.py` | `v=99` accepted as valid: the guard was `version // 100 != SUPPORTED_MAJOR // 100`, and `99 // 100 == 0 == 2 // 100`. | Compare majors directly, per `docs/PROTOCOL.md` §8. `test_envelope_version_gate`. |
| 3 | `firmware/aqua_protocol/*`, `nodo-{sensor-od,aerador,mezclador}/src/aqua_link.cpp` | `boot_id` published as **0** by three of four nodes. Each built `EnvelopeBuilder(role, 0, …)` at static-init and computed `s_bootId` inside `aquaLinkBegin()` without passing it on. Effect: after a reboot `seq` restarts at 1 while the gateway holds the previous high-water mark → a healthy restart logged as `out_of_order_batch {current_seq:1, previous_seq:132}`. `nodo-bomba` was already correct. | Added `EnvelopeBuilder::setBootId()` and call it in the three `aquaLinkBegin()`s. Verified live: `boot_id` 0 → 45306, new value on the next reset, no `out_of_order_batch`. Conformance 126 → 129 checks. |
| 4 | `gateway/aqua_gateway/export/{bundle,flat}.py` | Export reported *"no telemetry found"* for data plainly on disk. `role_files` took `start.date()` in UTC, but files are named by the **Lima** day of the device timestamps. | Convert the window to `settings.timezone` before taking the date. `test_day_boundary_follows_the_archive_timezone_not_utc`. |
| 5 | `gateway/aqua_gateway/db/engine.py` | `/api/experiments` returned **500**, not 503, when Postgres was down (TEST-PLAN §D4 asserts 503). The cached `db.available` flag lags, so the first request after the outage hit a raw `asyncpg.CannotConnectNowError`. | Normalise any driver error to `DatabaseUnavailable` in one place (`Database._connection`, `_Transaction.__aenter__`); added an app-level `DatabaseUnavailable` → 503 handler. All DB endpoints now 503 and the gateway self-heals without a restart. |

### Verified green (final)

- gateway suite **390 passed** (100% green across all 25 test suites)
- firmware conformance **129 checks, 0 failures**
- web `tsc --noEmit` clean, `next build` clean
- monorepo consolidated and pushed to `https://github.com/YetAnotherUser24/aqua`
- all pre-existing node refactors and firmware fixes folded and committed cleanly

### Gaps recorded, not closed (§G2)

- `od_sensor` real peripheral (OPTOD probe / DS3231) — the S3 board can only run `-virtual`.
- `pump` real peripheral — no S3 env at all.
- §F (the Pi) not run from this box; §G acceptance only partly run (one role on air, no
  virtual sensor/actuator pair for a real DO step).

### Documentation drift found

- §A2 #5: the SD toggle reads `SD: PAUSADO` / `SD: DETENIDO`, not `SD: INACTIVO`.
- §A2 #7 / §A3: `/api/commands` shows the SD toggle as `set_config{sd_logging:…}`; the
  §A2 row also expects `interval_s`, which is the *phase* param, not the toggle.
- §B2: the Last Will payload is `{"online":false,"reason":"will"}`, not `"lwt"`.
- §E1: `ACTIVE_EXPERIMENT` **requires** an existing `experiment_id` (400 otherwise) — the
  plan's `experiment_id: null` example cannot succeed.
- §E3: MANUAL mode does not populate `awaiting_confirm`; that field belongs to HIBRIDO.
  Manual genuinely does not auto-advance (verified), so the *behaviour* is right.
- §C4: an unknown experiment's `/exports` returns **404** with a precise detail, not an
  empty list.
- §0.1/§B3: `od_sensor`'s client id is `aqua-sensor-od` while its MQTT *user* is `od_sensor`.

---


## 🔴 Blocking

### 1. Add the Storage S3 access keys

The only thing stopping the archive from leaving the Pi. Everything else in the chain
works: rotation closes and compresses days, and the flush reaches the new project's
endpoint.

`/etc/aqua/gateway.env` currently has `AQUA_BACKUP__ACCESS_KEY_ID=CHANGE_ME` and the
same for the secret. The endpoint, region (`us-east-1`) and bucket (`aqua-archive`)
are already set correctly.

Create the keys in the Supabase dashboard under **Storage → S3 Access Keys** and add
them in your own terminal — they are secrets, so do not paste them into a chat:

```bash
ssh aqua@aqua-station
sudo -e /etc/aqua/gateway.env      # set ACCESS_KEY_ID and SECRET_ACCESS_KEY
sudo systemctl start aqua-flush.service
journalctl -u aqua-flush -n 30 --no-pager
```

Success looks like `uploaded=1 verified=1`. A failure records its reason in the
manifest's `last_error` column, which is what `docs/RUNBOOK.md` reads.

**Cost of waiting:** `pump/2026-10-09.csv.gz` has been closed and queued since 00:06,
and today's four files join the queue at midnight. **Nothing is lost** — retention
never deletes a file that is not verified remotely, so the local window grows instead.
The limit is disk, which is days away, not minutes.

---

## 🔐 Security

### 2. Rotate two secrets exposed in this session

`SUPABASE_SECRET_KEY` and the database password inside `SUPABASE_DIRECT_CONNECTION`
were printed in full by one command. The redaction pattern used matched names *ending*
in `SECRET` or `PASSWORD`, so it caught `SUPABASE_DATABASE_PASSWORD` but let
`SUPABASE_SECRET_KEY` through — the name ends in `KEY`. Both belong to the **new**
project (`sndfoyinvomdxzeuegmj`).

Rotate in the dashboard: *Settings → API* (secret key) and *Settings → Database*
(password — the pooler and direct strings change with it).

### 3. Untrack `Tesis-webpage/.env.local`

It is **tracked by git** even though `.gitignore` line 13 lists it — it was committed
in `f845304` while holding placeholder values. Filling in real keys and committing
would publish them.

```bash
cd Tesis-webpage
git rm --cached .env.local
git commit -m "chore: stop tracking .env.local (already in .gitignore)"
```

Left for you because it touches how the app is deployed.

### 4. Revoke the temporary passwordless sudo

Still present at `/etc/sudoers.d/90-aqua-deploy`, created to let the deployment be
driven remotely. Remove it when you no longer need that:

```bash
ssh -t aqua@aqua-station 'sudo rm /etc/sudoers.d/90-aqua-deploy'
```

### 5. Still outstanding from earlier in the project

- The **GitHub PAT** from `/root/aqua/.env`.
- The **old** Supabase project's database password.

---

## 🔧 Hardware

### 6. Flash `od_sensor`, `mixer` and `pump`

Verified: **only the aerator has real hardware.** It is the sole MQTT client
connecting from a LAN address (`192.168.137.19`); the other three usernames appear
only from virtual-node test runs.

Each node needs its own credential from `/etc/aqua/mqtt-node-credentials`, compiled
into the firmware — a node cannot fetch one later. The recipe that works:

```bash
# Note: AQUA_MQTT_HOST defaults to "aqua-station.local" and resolves dynamically
# via mDNS (ESPmDNS), so -DAQUA_MQTT_HOST is no longer needed unless overriding.
export PLATFORMIO_BUILD_FLAGS='-DAQUA_MQTT_PORT=1883 -DAQUA_MQTT_USER=\"<role>\" -DAQUA_MQTT_PASSWORD=\"'"$PW"'\"'
/root/.platformio/penv/bin/pio run -t upload
```

Quote it exactly like that — single quotes outside, `\"` inside. `shlex.split` strips
quotes, and the wrong form fails with `too many decimal points in number` *and* leaks
the credential into the compiler error. Redirect the build to a file and grep for
`SUCCESS|FAILED|Hash of data verified` rather than printing it.

**Update 2026-10-10:** an ESP32-S3 is connected. Confirm which role it is before
flashing (`docs/TEST-PLAN.md` §0.1 — the two ESP32-S3 roles are aerator and mixer), then
build `-virtual` first to prove the stack and real second. Full procedure:
`docs/DEPLOYMENT.md` §4.

### 7. Pump real-hardware coverage

Blocked: no pump board attached. Once flashed, the pump needs a bench run to confirm
the peristaltic calibration path end to end.

---

## ❓ Needs your decision

### 8. Migrate the old project's data?

The previous project still holds the real thesis data — `motor_telemetry` 15,630 rows,
`sensor_readings` 10,155, `motor_events` 451, `devices` 4 rows, and more. The new
project has the schema plus the seed rows the schema file inserts, nothing else.

If the new project is meant to replace the old one, that is a separate data migration.

### 9. Point the web app at the new project

The three values it needs are staged on the Pi at `/etc/aqua/supabase-web.env`
(0600, root) — they were originally pasted into the gateway's env file, which never
reads any of them.

`Tesis-webpage/.env.local` still holds placeholders. Do item 3 first, then copy them
in locally. The app reads `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY`.

### 10. Should the gateway's archive share that project?

The gateway's Storage endpoint currently points at `sndfoyinvomdxzeuegmj` — the same
project as the web app — because that is the project whose credentials were supplied.
Both fit inside the free tier (the gzipped archive is ~100 MB/year), but if you would
rather they were separate, that is a one-line change plus new keys.

---

## 🟡 Minor

### 11. The aerator reconnects periodically

8 MQTT connections in 6 hours. Not urgent, but worth a look at the firmware's
keepalive or session handling.

### 12. Known dormant issue, for the record

`_recover_orphans` only heals records with `closed_at IS NULL`, so a closed-but-unflushed
record for *today* is never re-finalised and would keep a stale row count and digest,
failing flush verification later. The bug that created that state (a graceful shutdown
finalising today's file) is fixed, so this only matters for a host that ran the old
code — the Pi was repaired. Worth revisiting if another such path is ever added.

---

## Notes on where things live

| What | Where |
|---|---|
| Gateway config | `/etc/aqua/gateway.env` (aqua:aqua, 0640) |
| Node credentials | `/etc/aqua/mqtt-node-credentials` (root:root, 0600) — the only copy; the broker keeps hashes |
| Web app credentials | `/etc/aqua/supabase-web.env` (root:root, 0600) |
| Archive | `/data/telemetry/<role>/YYYY-MM-DD.csv[.gz]` |
| Flush manifest | `/var/lib/aqua-gateway/manifest.db` |
| Deployed tree | `/opt/aqua-station` (run its suite with `/home/aqua/.venv-aqua/bin/python -m pytest tests/ -q`) |

Deployment and recovery detail is in `aqua-station/docs/DEPLOYMENT.md` and
`docs/RUNBOOK.md`.
