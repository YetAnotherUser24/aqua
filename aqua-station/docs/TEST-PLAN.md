# End-to-end test plan — refactored console + real node

**Scope.** Exercise every workflow, action, command, button and component the post-refactor
console exposes, against a real node, and prove the chain
`node → MQTT → gateway → SSE → browser` end to end.

**How to use this document.** It is written to be executed **in order**, on the bench, with a
browser open. Every step states an *expected* result and where to look, so a failure names one
layer instead of "it didn't work".

**Three verbs, used precisely:**

| Verb | Means |
|---|---|
| **Verify** | Look at a running system. Non-destructive. |
| **Exercise** | Interact and observe the effect. May change state. |
| **Prove** | Cause a specific failure and confirm the system reports it correctly. |

Only **Prove** steps may break something, and each says how to restore.

---

## Contents

| § | Area | Automated coverage |
|---|---|---|
| 0 | Preconditions: the one-board rotation, backend, build order | — |
| A | Console against the mock (no hardware) | none — manual only |
| B | Real gateway + real node — **run once per role in the rotation** | `test_commands.py`, `test_events.py`, `test_lifecycle.py` |
| C | Persistence, archive, export | `test_archive_*`, `test_rollup.py`, `test_export.py`, `test_flush.py` |
| D | Failure injection (Prove steps) | partly `test_events.py`, `test_commands.py` |
| E | Orchestrator state machine | `test_orchestration.py` |
| F | Deployment on the Pi | `test_env_template.py`, `test_shutdown_finalisation.py` |
| G | Acceptance: one full experiment | — |
| H | Triage table | — |
| I | Regression gate + coverage map | all |
| J | Abort and rollback | — |
| K | Results template (per role) | — |

**One board, four roles.** A single ESP32-S3 is reflashed to act as each node in turn:
`od_sensor → aerator → mixer → pump`. §0.1 is the rotation and the per-role build table — read
it first. Two of the four roles (`od_sensor`, `pump`) have **no real ESP32-S3 environment**,
so on this board they can only run `-virtual`; that is a recorded gap, not a pass.

**Legend:** `[ ]` untested · `[P]` pass · `[F]` fail · `[S]` skipped (record why)

**Size:** 79 numbered control rows plus 152 standalone checkboxes across 13 sections.
**8 checks are destructive**: §B2 (power-cut for the Last Will), §B7 (publish a malformed
envelope), and §D1–D6. Every other step is non-destructive against a running system, and each
destructive step says how to restore (§J).

**Sections B, C and G repeat per role** — written once, run four times. The per-role tracking
blocks in §K exist for exactly that reason.

---

## 0. Preconditions

### 0.1 One board, four roles — the rotation

**There is one ESP32-S3 on the bench and it must act as all four nodes, one at a time, by
reflashing.** The plan is therefore a **rotation**, not a single pass:

```
od_sensor  →  aerator  →  mixer  →  pump        (then §G acceptance on the pair below)
```

Each stop is a full build → flash → §B pass → record, then reflash to the next role. **Never
two roles online at once**: presence, LWT and `node_id` are per-role, and a stale node still
publishing is indistinguishable from the new one.

> **Sequencing rule.** Before reflashing, confirm the previous role is gone from
> `aqua/presence/#` (its LWT fires on power-down). If it is still there, the board has not
> rebooted into the new firmware — reflash again rather than continuing.

#### Which env to build per role

This is the first thing to get right, because the four nodes do **not** share a board target.
Two of them have **no real ESP32-S3 environment at all** — only a virtual one:

| Role | `platformio.ini` dir | Default env | Board | Real ESP32-S3 env? | Env to flash on this board |
|---|---|---|---|---|---|
| `od_sensor` | `nodo-sensor-od` | `lora32` | `esp32dev` (LilyGO) | **No** | `virtual-s3` only |
| `aerator` | `nodo-aerador` | `esp32-s3` | `esp32-s3-devkitc-1` | **Yes** | `esp32-s3` (real), `esp32-s3-virtual` |
| `mixer` | `nodo-mezclador` | `esp32-s3` | `esp32-s3-devkitc-1` | **Yes** | `esp32-s3` (real), `esp32-s3-virtual` |
| `pump` | `nodo-bomba` | `esp32dev` | `esp32dev` | **No** | `esp32-s3-virtual` only |

**Read that table carefully — it defines what can actually be tested on this board:**

- **`aerator` and `mixer`** are genuinely ESP32-S3 nodes. They can be tested **both ways**:
  `-virtual` (simulated peripheral) and real hardware. Their real peripherals are an ODrive S1
  and an ESC respectively.
- **`od_sensor` and `pump`** target a classic ESP32 in production. On this S3 board they can
  only run the **`-virtual`** flavour. That still exercises everything this plan cares about —
  the real serialiser, the protocol, presence, commands, the whole gateway chain — but it does
  **not** validate their real peripherals. Record that as a gap, not a pass.

```bash
# od_sensor and pump — virtual only on this board
cd /root/aqua/nodo-sensor-od && /root/.platformio/penv/bin/pio run -e virtual-s3 -t upload
cd /root/aqua/nodo-bomba    && /root/.platformio/penv/bin/pio run -e esp32-s3-virtual -t upload

# aerator and mixer — virtual, then real
cd /root/aqua/nodo-aerador   && /root/.platformio/penv/bin/pio run -e esp32-s3-virtual -t upload
cd /root/aqua/nodo-aerador   && /root/.platformio/penv/bin/pio run -e esp32-s3 -t upload
cd /root/aqua/nodo-mezclador && /root/.platformio/penv/bin/pio run -e esp32-s3-virtual -t upload
cd /root/aqua/nodo-mezclador && /root/.platformio/penv/bin/pio run -e esp32-s3 -t upload
```

#### Per-role facts the rotation needs

| | `od_sensor` | `aerator` | `mixer` | `pump` |
|---|---|---|---|---|
| Peripheral | OPTOD probe + DS3231 | ODrive S1 → M8325s | T-200 via SNR8503 | peristaltic head |
| OTA hostname | — | `aquacontrol-odrive.local` | `mixer-t200.local` | — |
| Topic | `aqua/tlm/od_sensor` | `aqua/tlm/aerator` | `aqua/tlm/mixer` | `aqua/tlm/pump` |
| MQTT user | `od_sensor` | `aerator` | `mixer` | `pump` |
| Node id | `a0000000-…-000000000001` | `b0000000-…-000000000002` | `c0000000-…-000000000003` | `d0000000-…-000000000004` |
| Experiment rate | 0.2 Hz (5 s) | 5.0 Hz | 0.3333 Hz (3 s) | 0.3333 Hz (3 s) |
| Idle rate | 0.0167 Hz (1/min) | 0.0167 Hz (1/min) | 0.05 Hz (20 s) | 0.05 Hz (20 s) |
| Key telemetry | `od_do_mg_l`, `od_sat_pct`, `od_temp_c`, `pwr_battery_v`, `rtc_health` | `drv_rpm`, `drv_vbus_v`, `drv_ibus_a`, `drv_torque_nm`, `ctl_mode` | `drv_rpm`, `drv_rad_s`, `drv_duty_pct`, `drv_stall` | `pump_dosed_ml`, `pump_rpm`, `pump_mode`, `pump_dose_completed` |
| Command verbs | `start_monitor`, `stop_monitor`, `set_sampling_rate`, `manual_sample`, `set_sleep_cycle`, `start_experiment`, `stop_experiment`, `set_state`, `sleep` | `set_speed`, `start`, `stop`, `emergency_stop`, `clear_estop`, `set_state` | `start_mixer`, `stop_mixer`, `set_state`, `stop_experiment` | `dose`, `force_on`, `force_off`, `stop`, `emergency_stop`, `set_state`, `stop_experiment` |
| E-STOP verb (§A3) | — (sensor, not an actuator) | `emergency_stop` | `stop_mixer` | `force_off` |

**Cross-check every verb against the firmware before judging a failure** — this table is a
summary, the source of truth is the `action == "…"` chain in each node:

```bash
grep -n 'action == "\|strcmp(action' /root/aqua/nodo-*/src/*.cpp
```

**Roles are not interchangeable on the wire.** A control that is correct for the aerator
(`set_speed`) is *rejected* by the mixer and the pump. An ack of `success: false` after a
control means the verb is wrong for the role currently flashed — see §H.

- [ ] Rotation order agreed and recorded here: **________**
- [ ] Roles that can only run `-virtual` on this board noted: **od_sensor, pump**

### 0.2 Pick a backend — three environments, run them in order

Do **not** jump to the Pi. Each environment adds one thing that can fail, so a failure early
is unambiguous.

| Env | Backend | How | Proves |
|---|---|---|---|
| **1** | Mock gateway (no broker, no DB) | `tools/mock-gateway.py` | The console alone. No node. |
| **2** | Real gateway + **real Mosquitto** + Postgres | §B0 | The protocol, ingest, persistence, a real node |
| **3** | The Pi | `deploy/install.sh` | Production |

**Environment 2 needs a real broker, and `tools/dev-stack.py` is not one.** That tool starts
an embedded `amqtt` broker on loopback plus its own virtual nodes — useful for dashboard
iteration, but an ESP32-S3 on the LAN cannot reach it, and it bypasses the Mosquitto config
this environment exists to exercise. See §B0.

### 0.3 Build order: virtual first, then real — per role

The `-virtual` build runs the **real control logic** against a **simulated peripheral**
(`../VIRTUAL_NODES.md`). Flash it first: it proves MQTT, encoding, presence and the whole
gateway chain with no motor under load, so a failure there is the stack, not the mechanism.

For `od_sensor` and `pump`, `-virtual` is **all there is** on this board (§0.1) — their real
peripherals only exist on a classic ESP32.

**Reflash helper.** Put this in your shell once; it applies the right env per role and is the
only thing that should ever write this board's flash during the rotation.

```bash
# Usage: reflash <role> [env]
reflash() {
  local role="$1" env="${2:-}"
  local dir pw
  case "$role" in
    od_sensor) dir=/root/aqua/nodo-sensor-od; env="${env:-virtual-s3}"      ;;  # no real S3 env
    aerator)   dir=/root/aqua/nodo-aerador;   env="${env:-esp32-s3}"        ;;
    mixer)     dir=/root/aqua/nodo-mezclador; env="${env:-esp32-s3}"        ;;
    pump)      dir=/root/aqua/nodo-bomba;     env="${env:-esp32-s3-virtual}" ;;  # no real S3 env
    *) echo "unknown role: $role" >&2; return 2 ;;
  esac

  pw=$(sudo grep "^$role=" /etc/aqua/mqtt-node-credentials | cut -d= -f2)
  [ -n "$pw" ] || { echo "no credential for $role in /etc/aqua/mqtt-node-credentials" >&2; return 2; }

  echo "== flashing $role as $env =="
  PLATFORMIO_BUILD_FLAGS="-DAQUA_MQTT_HOST=\"192.168.137.114\" \
-DAQUA_MQTT_PORT=1883 -DAQUA_MQTT_USER=\"$role\" -DAQUA_MQTT_PASSWORD=\"$pw\"" \
    /root/.platformio/penv/bin/pio run -C "$dir" -e "$env" -t upload 2>&1 \
    | grep -E 'SUCCESS|FAILED|Hash of data verified'
  unset pw
}
```

> **The quoting is load-bearing.** `shlex.split` strips quotes; the wrong form fails with
> `too many decimal points in number` **and** leaks the credential into the compiler error.
> Single quotes outside, `\"` inside, exactly as above. The helper also avoids leaving `$pw`
> in `PLATFORMIO_BUILD_FLAGS` for the next shell.

> **Never run a real and a virtual node with the same `DEVICE_KEY` simultaneously.** The
> backend cannot tell them apart and telemetry interleaves. See `VIRTUAL_NODES.md` §Safety.
> This is also why the rotation is strictly one role at a time.

Per-role sequence:

```bash
reflash od_sensor                       # virtual only
reflash aerator esp32-s3-virtual && reflash aerator esp32-s3
reflash mixer   esp32-s3-virtual && reflash mixer   esp32-s3
reflash pump                            # virtual only
```

- [ ] `od_sensor` flashes as `virtual-s3` and boots
- [ ] `aerator` flashes as `-virtual`, then as `esp32-s3` (real), and boots
- [ ] `mixer` flashes as `-virtual`, then as `esp32-s3` (real), and boots
- [ ] `pump` flashes as `esp32-s3-virtual` and boots
- [ ] Each flash: `pio device monitor -b 115200` shows a boot banner with no reset loop

### 0.4 Record the baseline — per role

Fill one block per role as the rotation reaches it. Every later step compares against these.

| Reading | How | od_sensor | aerator | mixer | pump |
|---|---|---|---|---|---|
| Env flashed | §0.1 table | ______ | ______ | ______ | ______ |
| `boot_id` | first telemetry envelope | ______ | ______ | ______ | ______ |
| Measured rate (Hz) | §B1 `timeout 30 mosquitto_sub … \| wc -l` | ______ | ______ | ______ | ______ |
| Declared vs measured OK? | §0.1 rates | ______ | ______ | ______ | ______ |
| Presence seen | `aqua/presence/#` | ______ | ______ | ______ | ______ |
| LWT fires on power-down | §B2 | ______ | ______ | ______ | ______ |
| Command ack `success:true` | §B3 | ______ | ______ | ______ | ______ |
| Unknown verb → `false` | §B3 honest-ack | ______ | ______ | ______ | ______ |
| CSV/rollup written | §C1 | ______ | ______ | ______ | ______ |

Rotation-level (fill once):

| Reading | Value | How |
|---|---|---|
| Gateway schema version | ______ | `curl -s …/api/roles \| jq .schema_version` |
| Broker IP | ______ | `hostname -I` on the broker host |
| Test suite count | 324 | `pytest -q` |

---

## A. Environment 1 — the console, with no hardware

**Why this environment exists:** every previous regression was caught here. A component that
only works with a real node is a component that was never tested — and this is the only
environment where a broken control can be clicked safely, with nothing attached.

### A1. Start the mock

```bash
cd /root/aqua/aqua-station/web && npm run build          # mock serves web/out/
cd /root/aqua/aqua-station
pkill -f mock-gateway.py; sleep 1
PYTHONPATH=$PWD/gateway nohup gateway/.venv/bin/python tools/mock-gateway.py \
  --root $PWD/web/out --port 3111 > /tmp/mock-gw.log 2>&1 &
sleep 2
curl -s http://localhost:3111/api/roles | head -c 200
```

Open `http://localhost:3111/`.

> **Harness quirk, not a bug:** `/api/stream` (SSE) never closes, so a headless browser
> waiting on `load` hangs. Load → `reload()` → wait. The 200 ms RTC clock also keeps the page
> "unstable" for Playwright; click header buttons with `{ force: true, noWaitAfter: true }`.

| # | Expected | [ ] |
|---|---|---|
| 1 | Title `AquaSCADALab` + a state pill (`REPOSO` when idle) | [ ] |
| 2 | RTC clock ticks every ~200 ms, format `HH:MM:SS.mmm` | [ ] |
| 3 | Clock renders `--:--:--.---` on **first paint**, then fills — not a hydration error | [ ] |
| 4 | KPI ribbon: DO, saturation, temperature, aerator tiles populated | [ ] |
| 5 | Chart titled with the `1.0 Hz Sensor` badge | [ ] |
| 6 | Right column shows tabs **Ensayo · Actuadores · Sensor · Diagnóstico** | [ ] |
| 7 | Footer reads `en vivo` (not `conectando`) | [ ] |
| 8 | Browser console has **no** errors (font-preload warnings are benign) | [ ] |

### A2. Every control, one row at a time

**Rule:** after each row, check the browser console. A control that works but logs a React
warning is a **fail** — the warning is the next bug's early warning.

#### Header

| # | Control | Action | Expected | [ ] |
|---|---|---|---|---|
| 1 | **Iniciar Ensayo** | Click from idle | New experiment created **and** started; pill → `EXP ACTIVO`; phase list appears | [ ] |
| 2 | **Pausar** | Click while active | Paused; `Detener` still enabled | [ ] |
| 3 | **Reanudar** | Click | Resumes; phase timer continues from where it stopped (no jump) | [ ] |
| 4 | **Detener** | Click | → `REPOSO`; experiment id clears | [ ] |
| 5 | **SD: ACTIVO** | Click | → `SD: INACTIVO`; a `set_config{sd_logging:false}` goes to `od_sensor` | [ ] |
| 6 | **Modo** dropdown | Each option | `Experimento Activo`, `Reposo`, `Anulación Manual`; pill follows | [ ] |
| 7 | **PARADA DE EMERGENCIA** | Click | Per-role fan-out — see §A3 | [ ] |
| 8 | Node count badge | — | Updates as nodes appear/disappear | [ ] |

#### Ensayo tab

| # | Control | Action | Expected | [ ] |
|---|---|---|---|---|
| 9 | **Caso de Ensayo** dropdown | Each of 3 protocols | Phase cards rebrand; `Archivo destino:` changes | [ ] |
| 10 | Phase card header | Click to expand/collapse | `desc`, params, mode footer render | [ ] |
| 11 | **AUTO / HÍBRIDO / MANUAL** | Click each | Footer text changes; selection persists across a re-render | [ ] |
| 12 | Number field (*DO meta de corte*) | Type `6.5` | Persists across re-render; sent as a phase param | [ ] |
| 13 | Number field | Enter garbage (`abc`) | Rejected or coerced — never `NaN` into the payload | [ ] |
| 14 | Segmented select | Switch options | Dependent field relabels; other values kept | [ ] |
| 15 | Toggle switch | Flip | Visual state flips; no console error | [ ] |
| 16 | **Disparar Acción** (MANUAL) | Click | Sends `aerator set_state ACTIVE_EXPERIMENT` | [ ] |
| 17 | **Forzar Avance** (MANUAL) | Click | The active phase advances by exactly one | [ ] |
| 18 | **Pausar Ensayo / Detener** (footer) | Click | Same semantics as the header equivalents | [ ] |
| 19 | **Descargar** dropdown | Open with no experiment | Disabled, with an explanation | [ ] |
| 20 | **Descargar** dropdown | Open with an experiment | Export options appear | [ ] |
| 21 | Export grid + gzip toggle | Change + confirm | Request carries the grid; gzip changes the extension | [ ] |

#### Actuadores tab

| # | Control | Action | Expected | [ ] |
|---|---|---|---|---|
| 22 | Aerator **power** | On | `set_speed` sent; amber `bg-amber-500` when on | [ ] |
| 23 | Aerator **power** | Off | `stop` sent; toggle returns to grey | [ ] |
| 24 | Aerator **Dosificar**? | — | Aerator has no dosing control — if one appears, that is a bug | [ ] |
| 25 | Mixer **power** | On / Off | `start_mixer` / `stop_mixer`; teal `bg-teal-500` | [ ] |
| 26 | Pump **Dosificar** | Click | Sends `pump dose {target_ml: <field>}`; the entered volume is what is sent | [ ] |
| 27 | Pump **Purgar Tubería** | Click | Sends `pump force_off {}` (a stop, not a dose) | [ ] |
| 28 | "N Nodos Listos" badge | — | Count = roles present in the registry (aerator/mixer/pump) | [ ] |

#### Sensor / Diagnóstico tabs

| # | Control | Action | Expected | [ ] |
|---|---|---|---|---|
| 29 | Sensor tab | Open | Live DO/temp fields, correct units and precision | [ ] |
| 30 | Diagnóstico tab | Open | Gateway health, backlog, schema version | [ ] |

#### Chart and panels

| # | Control | Action | Expected | [ ] |
|---|---|---|---|---|
| 31 | Windows `10m 1h 24h 7d Todo` | Click each | X-axis rescales; no empty-plot flicker | [ ] |
| 32 | **⚙️ Personalizado** | Set value+unit, apply | Custom window applies; s/m/h/d all work | [ ] |
| 33 | **t - t₀** / **Hora** | Toggle | Relative ↔ clock (Lima) | [ ] |
| 34 | **Configurar Ejes** | Set min/max, apply | Both Y axes honour the manual range | [ ] |
| 35 | **Configurar Ejes** | Set min > max | Rejected or auto-swapped — never an inverted axis | [ ] |
| 36 | **Hito** | Click | Composer opens; see §A4 | [ ] |
| 37 | Chart legend | Click a series | Series hides/shows; legend is authoritative | [ ] |
| 38 | DO series | — | Drawn as a **filled `Area`**, not a bare line | [ ] |
| 39 | RPM series | — | Dashed line (`strokeDasharray="3 3"`) | [ ] |
| 40 | Milestone guides | With hitos present | Violet vertical `ReferenceLine`s, not an inline list | [ ] |
| 41 | Multivariable inspector | Hover | Tooltips resolve with correct units | [ ] |
| 42 | Bitácora panel | — | `t=MM:SS` badge + `RTC HH:MM:SS` per hito | [ ] |
| 43 | Levels panel | Wait 30 s | Refresh fires; values update | [ ] |
| 44 | Events table | Filter severity/scope/type | Rows filter; empty state explains itself | [ ] |
| 45 | Events table | With a scoped experiment | Header says `acotado al ensayo` | [ ] |
| 46 | Health panel | — | Backlog and service rows render | [ ] |
| 47 | Browser resize | Narrow to phone width | Layout reflows; nothing overlaps or clips | [ ] |

- [ ] **A2 complete** — every row above checked

### A3. E-STOP — the safety-critical path

This is where the refactor changed **behaviour**, not appearance, so test it against the
protocol rather than a screenshot.

**The bug it fixes:** the previous code sent *one* verb to *all* nodes. The mixer and pump
reject verbs they do not implement, so the impeller kept spinning through an E-STOP.

Expected fan-out (source of truth: `web/src/app/page.tsx`):

| Role | On E-STOP | On re-arm |
|---|---|---|
| `aerator` | `emergency_stop` | `clear_estop` |
| `mixer` | `stop_mixer` | `set_state {state: IDLE}` |
| `pump` | `force_off` | `set_state {state: IDLE}` |

Cross-check each verb against the firmware's own handler before judging:

```bash
grep -n 'action == "' /root/aqua/nodo-*/src/*.cpp
```

```bash
curl -s http://localhost:3111/api/commands | python3 -m json.tool | head -40
```

- [ ] Three commands, one per role, each with the **role-specific** verb
- [ ] No command uses a verb that role does not implement
- [ ] Pressing E-STOP twice does not duplicate the fan-out
- [ ] The button shows a latched state until re-armed
- [ ] Re-arm sends the three **clear** verbs
- [ ] After re-arm, the aerator accepts a `set_speed` again (latch really cleared)

### A4. Hito round-trip

Two separate bugs made this look broken: the mock did not persist milestones, and the
frontend never re-fetched. Both are fixed — confirm **both halves**.

| # | Step | Expected | [ ] |
|---|---|---|---|
| 1 | Click **Hito** with no run | Disabled; tooltip says to start a run | [ ] |
| 2 | Start a run, click **Hito** | Composer opens | [ ] |
| 3 | Enter label + detail | Fields accept input | [ ] |
| 4 | Switch `kind` across all four | sulfite=purple, step=amber, winkler/note=sky | [ ] |
| 5 | Submit | `POST …/milestones` returns 201 | [ ] |
| 6 | — | A **violet vertical guide** appears on the DO chart | [ ] |
| 7 | — | The bitácora gains the row **without a manual refresh** | [ ] |
| 8 | Submit an empty label | Rejected client-side or 422 — never a blank hito | [ ] |
| 9 | Reload the page | The hito is still there (persisted, not just local) | [ ] |

### A5. Invariants that must hold throughout

Properties, not clicks. Check at the end of §A and again in §G.

- [ ] No control leaves a spinner running indefinitely
- [ ] No button both looks enabled and silently does nothing
- [ ] Destructive controls (Detener, E-STOP) are visually distinct from benign ones
- [ ] The page works at 100 % and 150 % zoom
- [ ] Reloading mid-experiment restores server state, not stale local state

---

## B. Environment 2 — real gateway, a real node

> **This section runs four times — once per role in the rotation (§0.1).** Set `$ROLE` to the
> role currently flashed, complete §B, record it in §0.4, then reflash to the next role.
> Every check below is written to be role-agnostic; the per-role values come from the tables in
> §0.1 and §B3.

```bash
export ROLE=od_sensor      # then: aerator, mixer, pump  — in rotation order
```

### B0. Stand up a real broker + gateway

The node must reach **Mosquitto**, so this environment cannot use the loopback broker inside
`tools/dev-stack.py`. Either install Mosquitto on this box, or run the gateway on the Pi and
skip to §F. The only requirement is that the broker has the `aerator` / `mixer` users that
`/etc/aqua/mqtt-node-credentials` records, and that the board can reach it.

```bash
# deploy/mosquitto/aqua.conf is a conf.d drop-in, installed to /etc/mosquitto/conf.d/
sudo cp deploy/mosquitto/aqua.conf /etc/mosquitto/conf.d/
sudo systemctl restart mosquitto
sudo systemctl status mosquitto --no-pager | head -5

# The gateway against that broker
cd /root/aqua/aqua-station
gateway/.venv/bin/python -m aqua_gateway.main &
sleep 3
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

The broker must listen on the LAN (`listener 1883 0.0.0.0`); the shipped config already does.

- [ ] `mosquitto` active, listening on `0.0.0.0:1883`
- [ ] `/healthz` returns 200 with `flush_backlog_s`, `database`, `live_subscribers` present
- [ ] Gateway log shows an MQTT connect, no auth error

### B1. The node reaches the broker

```bash
mosquitto_sub -h 127.0.0.1 -t 'aqua/presence/#' -v     # retained presence
mosquitto_sub -h 127.0.0.1 -t "aqua/tlm/$ROLE" -v | head -3
```

- [ ] Presence shows `{"online":true,…}` for **exactly** the connected node
- [ ] That presence is **retained** — subscribe again 30 s later and it is still delivered immediately
- [ ] Telemetry arrives, and the **rate matches** `roles.yaml` `expected_rate_hz`
- [ ] The envelope carries `v:2`, `role`, `node_id`, `exp`, `seq`, `t_dev_ms`, `t_src`, `boot_id`, `samples`
- [ ] `role` and `node_id` match the topic (a mismatch is a reject + `protocol_error`)
- [ ] `seq` increments by exactly 1 per message

> **A wrong rate is a real bug, not cosmetic.** The rollup divides by `expected_rate_hz` to
> compute downtime: a declared rate the firmware does not produce reports a healthy run as
> mostly down (the mixer showed ~56 s of downtime per minute when it declared 5 Hz).

Measure it rather than eyeballing:

```bash
timeout 30 mosquitto_sub -h 127.0.0.1 -t "aqua/tlm/$ROLE" -v | wc -l
# aerator experiment ≈ 150 in 30 s (5 Hz); mixer ≈ 10 (0.33 Hz)
```

- [ ] Measured count within ±20 % of the declared rate

### B2. Presence: Last Will & Testament

**Prove** the will fires. With `mosquitto_sub -t 'aqua/presence/#' -v` running:

1. Cut the board's power (or `Ctrl-C` its monitor if it is a `-virtual` build).
2. Wait **≤ 90 s** (keepalive 60 s + margin).

- [ ] `aqua/presence/<node_id>` → `{"online":false,"reason":"lwt"}`
- [ ] The dashboard greys the node out **without** waiting a 60 s heartbeat
- [ ] A `node_offline` event appears in `/api/events`
- [ ] **Exactly one** role is present — the one flashed. No other role is publishing (`aqua/presence/#` lists only `$ROLE` plus the gateway)
- [ ] Power the board back on → `node_online`, and its `boot_id` **incremented**

### B3. Commands reach the node, and the ack is honest

**Use the verb for the role currently flashed.** A verb from another row is *rejected* — that
rejection is correct behaviour, not a bug.

| Role flashed | Valid test command | Note |
|---|---|---|
| `od_sensor` | `set_sampling_rate {"interval_sec":5}` | sensor is not an actuator; no E-STOP |
| `aerator` | `set_speed {"speed_percent":20}` | 0–600 RPM envelope |
| `mixer` | `start_mixer {"target_rpm":800}` | a bare start floors at `MIXER_DEFAULT_RPM` (600) |
| `pump` | `dose {"target_ml":10}` | volume-oriented, not speed |

```bash
# Substitute the row for the role currently flashed.
curl -s -X POST http://127.0.0.1:8000/api/commands -H 'content-type: application/json' \
  -d "{\"role\":\"$ROLE\",\"action\":\"<ACTION>\",\"target\":<TARGET>,\"deadline_s\":30,\"requested_by\":\"operator:test\"}" \
  | python3 -m json.tool
```

Worked examples for the two real-hardware roles:

```bash
# Aerator
curl -s -X POST http://127.0.0.1:8000/api/commands -H 'content-type: application/json' \
  -d '{"role":"aerator","action":"set_speed","target":{"speed_percent":20},"deadline_s":30,"requested_by":"operator:test"}' \
  | python3 -m json.tool

# Mixer
curl -s -X POST http://127.0.0.1:8000/api/commands -H 'content-type: application/json' \
  -d '{"role":"mixer","action":"start_mixer","target":{"target_rpm":800},"deadline_s":30,"requested_by":"operator:test"}' \
  | python3 -m json.tool
```

In another terminal:

```bash
mosquitto_sub -h 127.0.0.1 -t 'aqua/ack/#' -v
```

- [ ] An `aqua/ack/<role>` message arrives with `success: true`
- [ ] Telemetry's `drv_rpm_target` moves to match, then `drv_rpm` follows
- [ ] A `command_acknowledged` event exists in `/api/events`
- [ ] The command row in `/api/commands` shows `acknowledged`, not `pending`

**Now the honest-ack test.** Send a verb the firmware does not implement:

```bash
curl -s -X POST http://127.0.0.1:8000/api/commands -H 'content-type: application/json' \
  -d '{"role":"pump","action":"clear_estop","target":{},"deadline_s":30,"requested_by":"operator:test"}'
```

- [ ] The ack returns `success: false` with `unsupported_action`
- [ ] The command row is `rejected`, not `acknowledged`

> **This is the most important assertion in §B.** A node that acknowledges a verb it never
> performed is worse than one that admits it cannot — the log then claims a safety action
> happened that did not.

### B4. The console drives the real node

Repeat the **§A2 table**, watching `mosquitto_sub -t 'aqua/cmd/#' -v` in a second terminal
while clicking.

**Because one board is reflashed per role, the console itself does not change between stops.**
Only the role's own controls can succeed; the others may still be *clickable* but their
command will be rejected by a node that is not on air. That is expected — but see the last
check, which is the real one.

- [ ] Every control that claims to send a command produces **exactly one** command
- [ ] No control sends a verb the **currently flashed** role rejects (every ack is `success: true`)
- [ ] The values on screen match the node's telemetry, not optimistic local state
- [ ] A control pressed twice in a row does not silently swallow the second press
- [ ] **No control claims success for a role that is not on air.** A button that reports OK while
      no node is present is a false-success bug — the most dangerous kind here

### B5. Clock and time sources

```bash
curl -s http://127.0.0.1:8000/api/live | python3 -m json.tool | head -30
curl -s 'http://127.0.0.1:8000/api/events?type=clock_offset_exceeded' | python3 -m json.tool
```

- [ ] `t_src` is `ntp` or `espnow` — **not** `monotonic` (lowest trust)
- [ ] `t_dev_ms` is Unix epoch ms UTC, and within a few seconds of the gateway's clock
- [ ] No `clock_offset_exceeded` during a steady run

Compare device time against the gateway directly:

```bash
# The difference between t_dev_ms and the gateway's arrival stamp is the offset.
curl -s http://127.0.0.1:8000/api/live \
  | python3 -c 'import json,sys,time; d=json.load(sys.stdin); print("now_gw_ms", int(time.time()*1000)); print(json.dumps(d)[:400])'
```

- [ ] Device and gateway clocks agree to within the documented tolerance

### B6. Telemetry gaps

```bash
curl -s 'http://127.0.0.1:8000/api/events?type=telemetry_gap_detected' | python3 -m json.tool
```

- [ ] **No** `seq` gaps during a steady run
- [ ] After a deliberate 20 s unplug, a `telemetry_gap_detected` event appears
- [ ] After replug, `telemetry_gap_recovered` appears and `seq` resumes without repeats
- [ ] A duplicate batch (same `boot_id`,`seq`) is dropped, not double-counted

### B7. Malformed input is rejected, not absorbed

**Prove** the validator. Publish a deliberately broken envelope:

```bash
# role does not match the topic
mosquitto_pub -h 127.0.0.1 -t 'aqua/tlm/aerator' -q 1 -m \
  '{"v":2,"role":"mixer","node_id":"c0000000-0000-0000-0000-000000000003","seq":1,"t_dev_ms":1791548022123,"t_src":"ntp","boot_id":1,"samples":[{}]}'
```

- [ ] A `protocol_error` event is recorded naming the mismatch
- [ ] The gateway does **not** crash, stall, or write the sample
- [ ] Live `/api/live` still updates afterwards

Repeat once with an unknown major version (`"v":99`):

- [ ] Rejected as an unknown major, with a `protocol_version_mismatch` or `protocol_error`

---

## C. Persistence, archive, export

> **Runs once per role as well** (§0.1). Cheapest to do at the end of each §B pass while the
> role is still flashed, rather than reflashing to check an archive later.

### C1. Two logging tiers, and only two

```bash
sudo -u postgres psql -d aqua -c "SELECT role, COUNT(*), MIN(bucket) FROM telemetry_rollups GROUP BY role;"
ls -la /data/telemetry/$ROLE/ 2>/dev/null || sudo ls -la /var/lib/aqua-gateway/archive/$ROLE/
```

- [ ] 1-minute rollups exist for the role, one row per minute per role
- [ ] A CSV is **open and growing** for today
- [ ] Rollup row count is roughly `elapsed_minutes`, not `elapsed_seconds` (no per-sample write)

### C2. CSV integrity

```bash
tail -3 /data/telemetry/$ROLE/$(date +%Y-%m-%d).csv 2>/dev/null \
  || sudo tail -3 /var/lib/aqua-gateway/archive/$ROLE/$(date +%Y-%m-%d).csv
head -1 /data/telemetry/$ROLE/$(date +%Y-%m-%d).csv 2>/dev/null \
  || sudo head -1 /var/lib/aqua-gateway/archive/$ROLE/$(date +%Y-%m-%d).csv
```

- [ ] The header has **exactly one** timestamp column
- [ ] Every row's field count matches the header
- [ ] Timestamps are monotonically increasing
- [ ] Nulls are **skipped, not zeroed** (a hole must not become `0`)

### C3. Rotation and compression

```bash
curl -s -X POST http://127.0.0.1:8000/api/archive/rotate | python3 -m json.tool
sqlite3 /var/lib/aqua-gateway/manifest.db \
  "SELECT path, rows, closed_at, flushed_at FROM files ORDER BY closed_at DESC LIMIT 5;"
```

- [ ] Rotation closes files for any **completed** day and leaves today open
- [ ] Closed files are gzipped, and the gzip round-trips to the same row count
- [ ] The manifest records a sha256 and a row count per file
- [ ] An `archive_file_closed` event is emitted

### C4. Export

```bash
EXP=$(curl -s 'http://127.0.0.1:8000/api/experiments?limit=1' | python3 -c 'import json,sys; print(json.load(sys.stdin)["experiments"][0]["id"])')
echo "experiment: $EXP"

# NOTE: grid_seconds and compress are QUERY parameters, not a JSON body.
curl -s -X POST "http://127.0.0.1:8000/api/experiments/$EXP/export?grid_seconds=1&compress=true" \
  | python3 -m json.tool
curl -s "http://127.0.0.1:8000/api/experiments/$EXP/exports" | python3 -m json.tool
```

- [ ] An export bundle is produced and listed by `/exports`
- [ ] The wide dataset is **time-aligned** on the chosen grid
- [ ] The event overlay is merged into the same bundle
- [ ] Gaps are filled by last-observation-carry-forward, and filled points are distinguishable
- [ ] Re-running the export yields **identical columns** (it is derived, not stored at write time)
- [ ] `grid_seconds=0` (or `> 60`) is rejected with 422, not silently coerced
- [ ] `GET /api/experiments/<unknown>/exports` returns an empty list, not a 500

---

## D. Failure injection

Each step says how to restore. Do these on **environment 2**, never on the Pi during a real run.

### D1. Clock uncertainty

**Prove** that a node without a synced clock is flagged rather than trusted.

- [ ] With `t_src: "monotonic"`, a `clock_uncertainty_high` event is emitted
- [ ] The export marks those rows as low-trust
- [ ] Raw archive files are **never rewritten** — offsets are applied at export time

### D2. Node reboot mid-run

Power-cycle the board during an experiment.

- [ ] `boot_id` increments
- [ ] `seq` restarts from the new boot's counter without being read as a gap
- [ ] `node_reconnected` (not a bare `node_online`) is emitted
- [ ] The experiment continues; the dashboard does not drop the run

### D3. Broker restart

```bash
sudo systemctl restart mosquitto
```

- [ ] The gateway reconnects automatically
- [ ] QoS 1 + `clean_session=false` means a command issued while the node was away is delivered on return
- [ ] A command older than its `deadline_s` is **refused**, not executed (see `test_commands.py::TestExpiry`)

### D4. Database unavailable

```bash
sudo systemctl stop postgresql
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/experiments
sudo systemctl start postgresql
```

- [ ] `/healthz` reports the DB as unavailable rather than hanging
- [ ] `/api/experiments` returns **503**, not a 500 and not an empty list
- [ ] Telemetry keeps being archived to CSV (only the DB tier degrades)
- [ ] After restart, the gateway recovers with no restart of its own

### D5. Disk pressure

- [ ] A `disk_usage_warning` fires at the documented threshold
- [ ] `disk_usage_critical` fires next, and retention stops deleting
- [ ] `retention_blocked_unflushed` fires if files are old but unarchived
- [ ] **Nothing unflushed is ever deleted** — this is the whole point of the retention design

### D6. Flush failure

Stop or break the object-storage credentials (see `docs/RUNBOOK.md` §"Flush failed").

- [ ] `flush_failed` is emitted with a reason in the manifest's `last_error`
- [ ] `flush_backlog_s` climbs but **nothing is lost**
- [ ] Restoring credentials and re-running `aqua-flush.service` yields `uploaded=1 verified=1`
- [ ] Verification is idempotent — running it twice does not double-upload

---

## E. Orchestrator state machine

### E1. Transitions

```bash
for s in IDLE ACTIVE_EXPERIMENT MANUAL_OVERRIDE; do
  curl -s -X POST http://127.0.0.1:8000/api/state -H 'content-type: application/json' \
    -d "{\"state\":\"$s\",\"experiment_id\":null}" | python3 -m json.tool; echo
done
curl -s http://127.0.0.1:8000/api/state | python3 -m json.tool | head -30
```

- [ ] Each transition is accepted and reflected in `/api/state`
- [ ] An invalid state string is rejected with 422 or 400 — the engine raises
      `ValueError("unknown state …; valid: ['ACTIVE_EXPERIMENT', 'IDLE', 'MANUAL_OVERRIDE']")`,
      so the accepted set is exactly those three
- [ ] A `state_change` event is emitted per transition
- [ ] `MANUAL_OVERRIDE` emits `manual_override_enabled`, and leaving it emits `..._disabled`

### E2. Phase control

```bash
curl -s http://127.0.0.1:8000/api/recipes | python3 -m json.tool | head -40
curl -s -X POST 'http://127.0.0.1:8000/api/state/phase-mode?phase_id=p2_acq&mode=manual' | python3 -m json.tool
curl -s -X POST 'http://127.0.0.1:8000/api/state/phase-mode?phase_id=p2_acq&mode=bogus'
```

- [ ] Every shipped recipe validates (mirrors `test_orchestration.py::test_all_shipped_recipes_validate`)
- [ ] Every phase has a timeout (mirrors `test_every_recipe_phase_has_a_timeout`)
- [ ] Mode changes affect **that phase only**; others keep theirs
- [ ] An invalid mode returns **400**, not a silent success

### E3. MANUAL mode genuinely waits

This was a real bug: MANUAL mode was not implemented in the orchestrator, so it advanced on
its own.

```bash
curl -s -X POST http://127.0.0.1:8000/api/state -H 'content-type: application/json' \
  -d '{"state":"MANUAL_OVERRIDE","experiment_id":null}'
curl -s http://127.0.0.1:8000/api/state | python3 -c 'import json,sys; print(json.load(sys.stdin).get("awaiting_confirm"))'
```

- [ ] The phase **does not advance** on its own; `awaiting_confirm` is populated
- [ ] `POST /api/state/confirm` clears it and advances exactly one phase
- [ ] The timeout still fires if the operator never confirms (a stuck run is not a safe run)

### E4. Pause / resume fidelity

- [ ] `pause` then `resume` round-trips without losing `t_rel_s`
- [ ] The paused span is **excluded** from the relative timer
- [ ] `experiment_paused` / `experiment_resumed` events are emitted
- [ ] Pausing twice does not corrupt the timer

### E5. Phase advance conditions

- [ ] A DO-threshold phase advances when the threshold is met (respecting the min-elapsed gate)
- [ ] A **missing metric never advances** a phase (mirrors `test_missing_metric_never_advances_a_phase`)
- [ ] A timeout advances the phase and is recorded as a timeout, not a success
- [ ] `setpoint_reached` respects its tolerance, not exact equality

---

## F. Environment 3 — the Pi

Only after §A, §B and §C are clean. Follow `docs/DEPLOYMENT.md` §4–5 exactly: reflash **one
node at a time**, `nodo-aerador` **last** (it is the node whose 1 Hz poll caused the original
CPU overrun, so it has the most to gain and the most to lose).

### F1. Stack health

```bash
systemctl status aqua-gateway mosquitto caddy postgresql --no-pager
systemctl list-timers 'aqua-*' --no-pager
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

- [ ] All four services active; no failed units
- [ ] Both timers (`aqua-rotate`, `aqua-flush`) active
- [ ] `/healthz` `flush_backlog_s` low

### F2. The three numbers

From `docs/RUNBOOK.md` — check these before anything else.

- [ ] `flush_backlog_s` < 90000
- [ ] Idle SD writes **< 10 MB/day**
- [ ] No `seq` gaps during an experiment

```bash
a=$(awk '{print $7}' /sys/block/mmcblk0/stat); sleep 60
b=$(awk '{print $7}' /sys/block/mmcblk0/stat)
echo "$(( (b-a) * 512 )) bytes in 60s (~$(( (b-a) * 512 * 1440 / 1024 / 1024 )) MB/day)"
```

- [ ] Measured write rate in single-digit MB/day

### F3. Memory budget

```bash
systemctl status aqua-gateway --no-pager | grep Memory
ps -o rss= -C postgres | awk '{s+=$1} END {printf "postgres: %.0f MB\n", s/1024}'
free -m
```

- [ ] PostgreSQL ~65 MB, gateway ~80 MB, Mosquitto ~10 MB, Caddy ~20 MB, OS ~185 MB
- [ ] Total ≈ **360 MB of 905 MB**

### F4. Dashboard is served statically

- [ ] `https://aqua-station.local/` returns 200
- [ ] No Node runtime listening on the Pi
- [ ] Caddy proxies `/api` and `/events` on the same origin (relative URLs keep working)

### F5. Graceful shutdown

```bash
sudo systemctl restart aqua-gateway
journalctl -u aqua-gateway -n 40 --no-pager
```

- [ ] The open CSV is flushed and closed on shutdown
- [ ] No partial last line in the archive
- [ ] On restart, the gateway appends to today's file rather than truncating it

### F6. Soak

- [ ] 72 h at the real cadence (3 h every other day)
- [ ] No `seq` gaps
- [ ] `flush_backlog_s` stays low throughout
- [ ] Idle writes stay low throughout
- [ ] Die temperature under 70 °C, no new throttle bits (`vcgencmd get_throttled`)

---

## G. Acceptance — one complete experiment, end to end

The final test. **One run, start to finish**, with the browser open and
`mosquitto_sub -t 'aqua/#' -v` logging to a file.

### G0. Which roles must be on air

**A single board cannot run this section alone.** A meaningful experiment needs both a sensor
(DO source) and an actuator (the step), and the rotation leaves only one role flashed at a
time. So §G is run with the board on **one** role and the others supplied as **virtual nodes**:

| Role | How it is provided in §G | Why |
|---|---|---|
| `aerator` | **The board** (`esp32-s3`, real) | the actuator under test |
| `od_sensor` | virtual node (`tools/virtual-nodes.py`) | supplies DO(t) — cannot share the board |
| `mixer` | virtual (optional) | only if the protocol uses mixing |
| `pump` | virtual (optional) | only for a dosing protocol |

```bash
# Board flashed as aerator (real). Provide the rest virtually:
cd /root/aqua/aqua-station
gateway/.venv/bin/python tools/virtual-nodes.py --roles od_sensor --host 127.0.0.1
```

> **Different `DEVICE_KEY` is mandatory here.** The board and the virtual nodes must be
> distinct identities, or presence and telemetry interleave (`VIRTUAL_NODES.md` §Safety).
> Using a virtual sensor alongside a real actuator is expected and safe; using the *same key*
> twice is not.

- [ ] Board flashed as `aerator` (`esp32-s3`, real)
- [ ] Virtual `od_sensor` online and the dashboard shows both roles
- [ ] Only **one** real node is on air (the board)

### G1. The run

| # | Step | Expected | [ ] |
|---|---|---|---|
| 1 | Choose a protocol in **Caso de Ensayo** | Phases and their params load | [ ] |
| 2 | Press **Iniciar Ensayo** | Experiment created **and** started; `experiment_created` then `experiment_started` | [ ] |
| 3 | Let phase 1 complete | `experiment_phase_change` fires; the card shows COMPLETADA | [ ] |
| 4 | Add a **Hito** when the step begins | Violet guide appears; page did not need a refresh | [ ] |
| 5 | Watch the DO curve | Filled area, correct rate, no gaps | [ ] |
| 6 | Force one phase to **MANUAL** | It waits for **Forzar Avance**; nothing advances on its own | [ ] |
| 7 | **Forzar Avance** | Advances exactly one phase | [ ] |
| 8 | **Pausar** mid-phase, wait 30 s, **Reanudar** | `t_rel_s` excludes the paused span | [ ] |
| 9 | Press **Detener** | Run ends; `experiment_aborted` or `..._completed` | [ ] |
| 10 | Export the experiment | Bundle written and listed; grid honoured | [ ] |
| 11 | Check the events timeline | Ordering is coherent: created → started → phases → hito → ended | [ ] |
| 12 | Check the archive | A CSV for the day, correct header, monotonic timestamps | [ ] |
| 13 | Re-run the regression gate (§I) | All pass | [ ] |
| 14 | Re-check §A5 invariants | All hold | [ ] |

**Acceptance criteria:** all 14 rows pass, and the exported bundle reprocesses to the same
values shown on screen. If the export disagrees with the chart, the export is right — the
chart is a derived view.

### G2. What §G does and does not prove

- **Proves:** the orchestrator, the console, the archive, the export and the command path all
  work together over a real run, with at least one **real** node in the loop.
- **Does not prove:** the real peripherals of `od_sensor` and `pump`. Those roles have no real
  ESP32-S3 environment (§0.1); it proves they behave correctly as *virtual* nodes only. Record
  that gap rather than counting it as coverage.

---

## H. Triage — what a failure tells you

Read the layer, not the symptom.

| Symptom | Look here first |
|---|---|
| Nothing on the dashboard, footer says `conectando` | SSE, not the app — `curl -s http://127.0.0.1:8000/api/live` |
| Node never appears | Broker auth — `[code:135] Not authorized` is the **credential**, not the app |
| Telemetry arrives, dashboard stale | The SSE fan-out, not ingest — compare `/api/live` with `/api/samples` |
| Button does nothing | Browser console, then `/api/commands` — did a row appear? |
| Command sent, node ignores it | `aqua/ack/#` — a `success:false` means the **verb**, not the network |
| Node online but silent | LWT cannot see this. Check `telemetry_gap_detected` and the node's SD log |
| Chart empty but data flows | Client-side filter or time window, not the gateway |
| Rollup says downtime but node looked up | `expected_rate_hz` vs the firmware's real cadence |
| Export disagrees with the chart | The **export** is authoritative; the chart is derived |
| Colour looks wrong | A component using raw `slate-*`/`cyan-*` instead of `scada-*`/`telemetry-*` |
| Hito appears then vanishes on reload | It was never persisted — check the `POST` returned 201 |

---

## I. Regression gate

Run before every merge, and at the end of §G.

```bash
cd /root/aqua/aqua-station/gateway && .venv/bin/python -m pytest tests/ -q   # expect 324 passed
cd /root/aqua/aqua-station/web && npm run typecheck && npm run build          # expect clean
```

**There is no web test framework.** `typecheck + build` is the gate. Any component whose only
verification is "it looked right once" is untested — the §A2 table is what substitutes for it,
and it is the reason that table is exhaustive rather than illustrative.

### Coverage map — what the automated suite already covers

| Area | Test file |
|---|---|
| Commands: validation, expiry, supersession, safety actions | `test_commands.py` |
| Event rules, hysteresis, event stream | `test_events.py` |
| Orchestrator: recipes, conditions, timeouts, rate math | `test_orchestration.py` |
| Rollups: column mapping, buckets, dosing counters, declared cadence | `test_rollup.py` |
| Archive: reverse iteration, windows, byte budget, timezone | `test_archive_reader.py`, `test_archive_block_behaviour.py` |
| Rotation and compression | `test_rotate_compression.py` |
| Flush and remote verification | `test_flush.py` |
| Retention and pruning | `test_prune.py` |
| Export: grid, resample, wide build | `test_export.py` |
| Experiment id generation | `test_experiment_ids.py` |
| Registry and role shapes | `test_registry_seed_and_shapes.py` |
| Config template | `test_env_template.py` |
| Shutdown and finalisation | `test_shutdown_finalisation.py` |
| Sample timestamps and unknown fields | `test_sample_time_and_unknowns.py` |

**What no test covers, and therefore only this document does:** every browser control, the
SSE fan-out as rendered, and the physical behaviour of the node. Those three are exactly
§A, §B and §G.

---

## J. Abort and rollback

If a step in §B–§F leaves the system in a bad state, restore in this order. Each line is
independent; stop as soon as the system is healthy again.

### J1. Stop a runaway run

```bash
# 1. The console: PARADA DE EMERGENCIA (per-role verbs, see §A3)
# 2. If the UI is unreachable, go straight to the broker:
#    Note: `force_off` is handled by BOTH nodo-aerador and nodo-bomba, so it is not a
#    pump-exclusive verb — but it is still the correct one for the pump here.
for r in aerator mixer pump; do
  case $r in
    aerator) a=emergency_stop ;;
    mixer)   a=stop_mixer ;;
    pump)    a=force_off ;;
  esac
  curl -s -X POST http://127.0.0.1:8000/api/commands -H 'content-type: application/json' \
    -d "{\"role\":\"$r\",\"action\":\"$a\",\"target\":{},\"deadline_s\":30,\"requested_by\":\"operator:abort\"}"
done
```

- [ ] `/api/state` reports `IDLE`
- [ ] The mixer's `drv_running` is false and `drv_rpm` is decaying to 0
- [ ] The aerator's `drv_rpm_target` is 0

### J2. Reset the orchestrator

```bash
curl -s -X POST http://127.0.0.1:8000/api/state -H 'content-type: application/json' \
  -d '{"state":"IDLE","experiment_id":null}'
curl -s http://127.0.0.1:8000/api/state | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["state"], d.get("awaiting_confirm"), d.get("experiment_id"))'
```

- [ ] `IDLE`, no `awaiting_confirm`, no `experiment_id`
- [ ] A stale `MANUAL_OVERRIDE` does not survive the reset

### J3. Mark an abandoned experiment

A run stopped mid-flight is **aborted**, not completed. Do not delete it — the partial data
is real and the archive may already hold it.

```bash
curl -s -X POST http://127.0.0.1:8000/api/commands -H 'content-type: application/json' \
  -d '{"role":"aerator","action":"stop_experiment","target":{},"deadline_s":30,"requested_by":"operator:abort"}'
# The experiment row should end as aborted / ended_at set, never left 'active' forever.
```

- [ ] The experiment's `status` is no longer `active`
- [ ] Its `ended_at` is set
- [ ] `experiment_aborted` appears in the event stream

### J4. Recover the archive and DB tiers

```bash
sudo systemctl restart postgresql mosquitto
sudo systemctl restart aqua-gateway
sleep 3
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
```

- [ ] `/healthz` `database.available` is true
- [ ] The open CSV was flushed and closed, not truncated (§F5)
- [ ] Rollups resume; no duplicate rows for minutes already rolled up

### J5. Re-flash a bricked node

```bash
cd /root/aqua/nodo-$ROLE
/root/.platformio/penv/bin/pio run -e esp32-s3 -t upload
/root/.platformio/penv/bin/pio device monitor -b 115200
```

- [ ] Boot banner appears, `boot_id` incremented from the last known value
- [ ] The node reappears in `aqua/presence/#` as `online:true`

---

## K. Results template

Copy per **role**, not per session. **Record the failures, not just the passes** — a plan that
only ever passes is not being run honestly. The rotation means §B and §C repeat four times, so
one block per role is the only way the results stay comparable.

```text
Session:      ____-__-__  __:__
Board:        one ESP32-S3, reflashed per role
Environment:  1 (mock) | 2 (dev box) | 3 (Pi)
Operator:     __________________

Baseline (§0.4)   schema_version: ______   broker ip: ______

┌──────────────────────────── ROLE: od_sensor ───────────────────────────┐
| env flashed: virtual-s3 (only S3 option)        real peripheral: n/a    |
| A1 bench start ......... [ ]   B1 presence & rate ..... [ ]            |
| A2 controls (47) ....... [ ]   B2 LWT ................. [ ]            |
| A3 E-STOP (sensor: n/a) [ ]    B3 commands & acks ..... [ ]            |
| A4 Hito ................ [ ]   B5 clock ................ [ ]           |
| A5 invariants .......... [ ]   B6 gaps ................. [ ]           |
| C1 two tiers ........... [ ]   C2 CSV integrity ........ [ ]           |
| rate (Hz) measured/declared: ____/0.2      boot_id: ____               |
| failures: ____________________________________________________         |
└────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────── ROLE: aerator ─────────────────────────────┐
| env flashed: esp32-s3-virtual then esp32-s3 (REAL)                     |
| A2 controls (47) ....... [ ]   B1 presence & rate ..... [ ]            |
| A3 E-STOP = emergency_stop [ ] B2 LWT ................. [ ]            |
| B3 commands & acks ..... [ ]   B3 honest-ack .......... [ ]            |
| B4 console→node ........ [ ]   B5 clock ................ [ ]           |
| B6 gaps ................ [ ]   B7 malformed reject ..... [ ]           |
| C1 two tiers ........... [ ]   C3 rotate/compress ...... [ ]           |
| rate (Hz) measured/declared: ____/5.0      boot_id: ____               |
| failures: ____________________________________________________         |
└────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────── ROLE: mixer ──────────────────────────────┐
| env flashed: esp32-s3-virtual then esp32-s3 (REAL)                     |
| A2 controls (47) ....... [ ]   B1 presence & rate ..... [ ]            |
| A3 E-STOP = stop_mixer . [ ]   B2 LWT ................. [ ]            |
| B3 commands & acks ..... [ ]   B3 honest-ack .......... [ ]            |
| B4 console→node ........ [ ]   B5 clock ................ [ ]           |
| B6 gaps ................ [ ]   B7 malformed reject ..... [ ]           |
| C1 two tiers ........... [ ]   C3 rotate/compress ...... [ ]           |
| rate (Hz) measured/declared: ____/0.333    boot_id: ____               |
| failures: ____________________________________________________         |
└────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────── ROLE: pump ───────────────────────────────┐
| env flashed: esp32-s3-virtual (only S3 option)  real peripheral: n/a   |
| A2 controls (47) ....... [ ]   B1 presence & rate ..... [ ]            |
| A3 E-STOP = force_off .. [ ]   B2 LWT ................. [ ]            |
| B3 commands & acks ..... [ ]   B3 honest-ack .......... [ ]            |
| B4 console→node ........ [ ]   C1 two tiers ........... [ ]            |
| C4 export .............. [ ]                                           |
| rate (Hz) measured/declared: ____/0.333    boot_id: ____               |
| failures: ____________________________________________________         |
└────────────────────────────────────────────────────────────────────────┘

Run-level (once)
  D1..D6 failure injection  [ ]     E1..E5 orchestrator ..... [ ]
  F1..F6 Pi deployment .... [ ]     G  acceptance (§G0–G2) ... [ ]
  I  regression gate ...... [ ]

Gaps recorded, not closed (§G2)
  od_sensor real peripheral — not testable on an S3 board   [ ]
  pump real peripheral      — not testable on an S3 board   [ ]

Failures
  1. §____ role ____ step ____ : expected ______________________
     observed ______________________  layer (use §H): __________
  2. ...

Skipped
  §____ role ____ reason ____________________

Evidence
  mosquitto log: /tmp/aqua-mqtt-______.log
  gateway log:   /tmp/aqua-gw-______.log
  screenshots:   ______________________

Gate
  pytest: ____ passed, ____ failed
  typecheck: clean | errors
  build:     clean | errors

Docs drift found (§H, last row)
  ______________________________________________________
```

### What "done" means

The plan is complete when **all four roles have been through §B and §C**, **§G passes 14/14**,
the regression gate is green, and every failure has either been fixed or recorded in
`PENDING-TASKS.md` with a reproduction.

Two gaps are **expected and must be recorded rather than closed** (§G2): the real peripherals
of `od_sensor` and `pump` cannot be tested on an ESP32-S3 board, because neither has a real S3
environment. The template's "Gaps recorded" block exists so that gap stays visible in the
results instead of quietly absent from them.

A green gate with §G skipped is not a finished test — it is a finished *unit* test.
