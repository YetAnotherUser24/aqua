# Firmware patches

The four node repositories are **not** part of this repo and were deliberately not
moved. They keep building with PlatformIO exactly as before. This directory holds
the reviewable diffs that adopt the schema-v2 MQTT protocol.

## Why patches rather than a rewrite

The firmware changes are small and mechanical:

| Change | Scope |
|---|---|
| Replace HTTPS polling with an MQTT client | `cloud_worker.cpp` in each node |
| Send the v2 envelope | the JSON builder |
| Rename telemetry fields to schema v2 | the JSON builder |
| `t_dev_ms` (Unix epoch ms UTC) + `t_src` replaces the ambiguous timestamps | the JSON builder |
| Drop `WiFiClientSecure` / `setInsecure()` | the HTTP path |
| Subscribe to `aqua/cmd/<role>`, publish `aqua/ack/<role>` | command handling |
| Publish LWT presence on connect | MQTT client setup |
| Accept ESP-NOW `TIME` packets | time sync |
| Buffer events to SD and replay | uses the existing store-and-forward path |

The control logic does not change. PID keeps running on-node at 20 Hz; ESP-NOW
peer-to-peer DO exchange is untouched. This system is supervisory only, so nothing
in the patches is on the control path.

## Apply order

**One node at a time, with the old HTTP paths still live.** The gateway keeps the
previous endpoints available during migration, so a node that is reflashed and then
misbehaves can be reverted without taking the station down.

```
1. nodo-sensor-od      # the least coupled node, and the one with the RTC.
                       # Its DS3231 is the deployment's persistent time authority —
                       # get the TIME broadcast right before anything else.
2. nodo-bomba          # simple, low rate, easy to verify
3. nodo-mezclador      # note: speaks rad/s and duty, NOT PWM percent
4. nodo-aerador        # last: it is the one whose 1 Hz poll caused the CPU overrun,
                       # so it is also the one with the most to gain. Verify against
                       # the virtual bench before flashing.
```

## Verifying without hardware

`tools/mqtt-simulator.py` replaces `scripts/simulate-esp32.js` and
`scripts/virtual-bench/`. It publishes schema-v2 envelopes for every role so the
gateway can be exercised end to end before a single node is reflashed:

```
python tools/mqtt-simulator.py --role aerator --rate 5 --experiment EXP-DEMO-01
```

## What must not regress

The previous system lost 5 Hz fidelity for an extended period because a missing
archive table failed quietly — the note in the old `telemetryArchive.ts` says the
silence was the problem, not the missing table. The same class of bug is guarded
twice here:

- the gateway routes unknown fields to `unknown/` rather than dropping them
- `telemetry_gap_detected` fires on `seq` discontinuity, so a node that stops
  sending is visible within one batch instead of never

If a patch makes either of those silent, the patch is wrong.

## Per-node notes

### nodo-sensor-od
- Owns the DS3231. Must broadcast `TIME` over ESP-NOW **and** publish `aqua/time`
  when the broker is reachable.
- Keep event buffering on SD and replay on reconnect — it already does this for
  measurements in `measurement_storage.cpp`.

### nodo-aerador
- Currently polls `GET /api/commands/pending` every 1.0–1.5 s. **This is the fix
  that matters most**: it was 77% of the node's traffic and almost every response
  was `has_node: false`.
- Already carries `measured_do` / `water_temp` from ESP-NOW; these become
  `peer_do_mg_l` / `peer_temp_c` and are explicitly non-authoritative.

### nodo-mezclador
- Reports `target_rad_s`, `actual_rad_s`, `commanded_duty`. The old shared schema
  assumed PWM microseconds, which never described this device.

### nodo-bomba
- Volume-oriented: `pump_dosed_ml`, `pump_target_ml`, dose completion flags.
- `ML_PER_REV` is a calibration constant, not telemetry. The value in force must be
  recorded in the experiment manifest or a dose cannot be reconstructed later.

## Status

| Patch | Node | State |
|---|---|---|
| `01-nodo-sensor-od.patch` | nodo-sensor-od | **bench-verified end to end**; builds `-e virtual`, `-e lora32`, `-e virtual-s3` |
| `02-nodo-bomba.patch` | nodo-bomba | builds `-e esp32dev` and `-e esp32dev-virtual`; **not run against a broker** |
| `03-nodo-mezclador.patch` | nodo-mezclador | not started |
| `04-nodo-aerador.patch` | nodo-aerador | not started |

The apply order above assumes the cloud layer lives in `cloud_worker.cpp`. It does not,
in either node done so far: nodo-sensor-od's is `wifi_http.cpp`, and nodo-bomba's is
inline in `main.cpp`. **Expect the file names to differ per node; the shape of the change
does not.** Locate the cloud layer before planning the diff, rather than assuming a
filename.

### What patch 01 changed

Nine files. The design goal was that the migration be small enough to review in one
sitting and revertible by flipping one macro.

| File | Change |
|---|---|
| `src/aqua_link.{h,cpp}` | new. The v2 transport, ~330 lines |
| `lib/aqua_protocol/` | new. Vendored copy of this repo's envelope serialiser |
| `platformio.ini` | add `knolleary/PubSubClient` to `env:lora32`'s `lib_deps` |
| `src/config.h` | `AQUA_TRANSPORT_MQTT` plus broker settings |
| `src/main.cpp` | one call to `aquaLinkLoop()` per loop iteration |
| `src/wifi_http.{h,cpp}` | `wifiHttpHandleMqttCommand()` plus five shims |

The shims are the trick. Rather than deleting the ~450-line legacy HTTPS path,
`queueTelemetrySample`, `flushTelemetryBulk`, `telemetryQueueCount`,
`publishToAquaControl` and `pollPendingCommands` each gain a few lines at the top
that divert to `aqua_link` when `AQUA_TRANSPORT_MQTT` is set. The original bodies
stay below an `#else`, untouched. That is what makes the revert a compile-time
switch rather than a reflash of older firmware.

`wifiHttpHandleMqttCommand()` reshapes the MQTT command into the legacy response
envelope and hands it to the existing `processTelemetryResponse()` dispatcher. A
second dispatcher would have been two copies of the same action semantics, free to
drift apart.

Two defects surfaced during bench verification and are fixed in this revision:

- **Each sample now carries its own `t_dev_ms`.** A batch spans time, so a single
  envelope timestamp cannot describe every sample in it. At the 5 Hz experiment rate
  the node packs five readings per second and all five shared the envelope's
  timestamp — `/api/samples` showed five points on one millisecond, and the plot drew
  a single dot. The format reserves `t_dev_ms` inside a sample object for exactly this,
  and `rows_from_samples()` already preferred it when present; the firmware was simply
  not emitting it.
- **The envelope now carries the real experiment id.** It was hardcoded to `null`, so
  an experiment was indistinguishable from a monitor run in the archive — the same
  fields at the same rate, with nothing to tell them apart afterwards. The legacy path
  sent the sentinel `"idle"` here, which is worse than null: it invents an experiment
  to group rows under. `aquaLinkSetExperiment()` holds the id and the envelope sends
  `null` when there is none.

Both are covered by regression tests: `testPerSampleTimestamp()` in the protocol suite
(verified to fail when the instant is narrowed to 32 bits) and
`gateway/tests/test_sample_time_and_unknowns.py`.

### Verification performed

- `pio run -e virtual` and `pio run -e lora32` both SUCCESS.
- Rebuilt with `-DAQUA_TRANSPORT_MQTT=0`: **also SUCCESS**. RAM returns to the pre-patch
  value exactly in both `virtual` and `lora32`; flash sits 344 B / 328 B above it for the
  two shared-handler fixes described below, not for anything transport-related (see the
  table below). The kill-switch does genuinely compile the transport out.
- The patch was applied to a fresh `git clone` of the node repo and built there, so
  it is self-contained and does not depend on the working tree it was taken from.
- All 12 emitted field names were checked by hand against
  `gateway/aqua_gateway/schema/roles.yaml`, including units, precisions and the
  `legacy_divisor` used to convert each raw reading.

Footprint after all fixes in this revision:

| Env | Pre-patch baseline | Kill-switch (`-DAQUA_TRANSPORT_MQTT=0`) | Patched |
|---|---|---|---|
| `virtual` | 58,884 B RAM / 1,160,257 B flash | 58,884 / **1,160,601** | 66,476 / 1,147,701 |
| `lora32` | 59,016 B RAM / 1,175,273 B flash | 59,016 / **1,175,601** | 66,616 / 1,162,657 |

RAM returns to the baseline **exactly** in both cases, and flash is 344 B (`virtual`) /
328 B (`lora32`) above it. That residue is deliberate and worth naming, because the
earlier revision of this file claimed the revert was byte-for-byte and it no longer is:

- the unsupported-action branch in `onSensorCommandReceived` costs ~244 B, and
- the explicit "started without an experiment id" log costs ~100 B.

Both live in the **shared** command handler, not behind `AQUA_TRANSPORT_MQTT`, because both
fix defects that the legacy HTTPS path has too. Measured by rebuilding with the branch
removed (1,160,357) and subtracting. So the switch still compiles the *transport* out
completely — the ~7.5 KB of RAM that is the envelope buffer and batch, and all of
`aqua_link` — but the firmware is not byte-identical to its pre-patch self, and saying it was
would have hidden two intended changes inside a build-size number.

Each baseline was measured by building an **unpatched `git clone`** of the node repo with the
same toolchain, rather than trusting a figure recorded earlier — an earlier revision of this
file attributed the `virtual` numbers to `lora32`, which made the kill-switch look like it
overshot by 15 KB. Note also that `PLATFORMIO_BUILD_FLAGS` must be set on *every* build in a
comparison; omitting it silently produces an MQTT-on binary that looks like a huge regression.

Flash goes *down* by ~13 KB with the transport enabled: the MQTT path compiles out the
legacy HTTPS bulk-post code, and is smaller than it. RAM goes *up* by ~7.5 KB, which is
the static 8 KB envelope buffer — this node allocates nothing on the heap, so it is a
compile-time cost, not a runtime one.

### Still outstanding for patch 01

- ~~**Not yet run against the broker.**~~ Superseded — see *Bench verification of
  patch 01* below. The host-side conformance suite still does not compile
  `aqua_link.cpp`, because it depends on `PubSubClient` and `WiFi.h` — and it cannot. The
  *rules* that file used to hold (the deadline decision, the clock extrapolation, the
  plausibility threshold) have since moved into the library and **are** covered on the host;
  what remains device-only is the transport glue itself. See *Cross-cutting fixes applied to
  all four nodes*.
- ~~**The DS3231 `aqua/time` broadcast is not implemented.**~~ **Implemented.** This node is the
  deployment's time authority, and it now publishes `aqua/time` from the DS3231 — retained,
  every 30 s, tagged `t_src: "rtc"` — and pushes the same epoch milliseconds to the mesh in its
  ESP-NOW packet. See *Cross-cutting fixes applied to all four nodes*.
- **Corrected claim (verified, not argued).** An earlier revision said presence is published
  once per connection, so "a client that connects after the node does will wait for the next
  reconnect to learn it is alive". That is not what happens. Presence is published **retained**,
  and a fresh subscriber receives all four node topics within ~50 ms of subscribing — measured
  with a throwaway client, not inferred from the code.

  Retained presence plus the retained will is the correct design and needs no periodic
  republish: the offline direction is the will's job, the online direction is what retention is
  for. The original note read as a defect because it described the behaviour of a
  non-retained topic.

### What patch 02 changed

Eight files, same shape as patch 01. Two things about this node were not what the
plan predicted.

**The cloud layer is not `cloud_worker.cpp`.** It is inline in `main.cpp`.
`src/cloud_worker.cpp` and `src/esp32s3_main.cpp` are **dead**: no
`build_src_filter` compiles them, and `cloud_worker.cpp` cannot compile anyway — it
references `API_TELEMETRY`, which `config.h` does not define (it defines
`API_TELEMETRY_BULK`). Nobody noticed because both are excluded from every build.
They are left untouched; deleting firmware in a migration patch is how a migration
becomes an incident.

**The command semantics were extracted, not duplicated.** `main.cpp` applies
commands in an inline if/else chain inside the cloud task. The MQTT path needed the
same semantics, and copying them would have produced two definitions of what `dose`
means. They moved into `applyCommand(cmdId, action, target)` and both transports
call it. This is the one place patch 02 is a refactor rather than a shim.

| File | Change |
|---|---|
| `src/aqua_link.{h,cpp}` | new. The v2 transport |
| `src/pump_state.h` | new. `PumpMode` + `PumpState`, moved out of `main.cpp` |
| `lib/aqua_protocol/` | new. Vendored envelope serialiser |
| `platformio.ini` | `PubSubClient` in `lib_deps`; `+<aqua_link.cpp>` in **all three** `build_src_filter` lines |
| `src/config.h` | `AQUA_TRANSPORT_MQTT` plus broker settings |
| `src/main.cpp` | `applyCommand()` extracted; three shims; `pumpHandleMqttCommand()`; `aquaLinkLoop()` in the task |

Two platformio details worth remembering, because both cost time here:

- This repo lists sources explicitly (`+<main.cpp> +<pump_bus_real.cpp>`), it does not
  glob. A new `.cpp` under `src/` is **silently not compiled** unless it is added to
  every env's filter. It fails at link time as an undefined reference, which reads
  like a missing declaration rather than a missing build entry.
- `PumpState` lived inside `main.cpp`, so the transport could not see the type it had
  to serialise. It moved to `pump_state.h` rather than being mirrored, because a
  mirrored struct is the kind that drifts into a silently missing column.

`pump_mode` is sent as a string (`idle` / `dose` / `manual`), not a number, because
the schema declares it an enum and the registry serialises enums by name.

### Verification performed for patch 02

- `pio run -e esp32dev` and `-e esp32dev-virtual` both SUCCESS.
- Rebuilt with `-DAQUA_TRANSPORT_MQTT=0`: **SUCCESS**, and that state is what the
  pre-patch firmware was.
- Applied to a fresh clone and built there.
- All 7 emitted field names checked against `roles.yaml`.

Footprint, env `esp32dev-virtual`:

| | RAM | Flash |
|---|---|---|
| MQTT on | 59,284 B | 836,101 B |
| MQTT off (legacy TLS) | 51,448 B | **991,397 B** |

**The MQTT build is 155 KB smaller.** Dropping `WiFiClientSecure`/`HTTPClient` and
the TLS stack costs less than the MQTT client adds. That was not the expected result —
the migration was justified on latency and traffic, not size — and it matters on a
node whose partition table is 1 MB per slot.

### Still outstanding for patch 02

- ~~Same as patch 01: **not yet run against a broker.**~~ Superseded — see *Bench
  verification of patch 02* below, which ran against a broker: connect, presence, the full
  command surface, an 80 s outage, and `GET /api/samples` cross-checked against what the node
  emitted. What that run did **not** reach is the real pump hardware — see the note below.
- The three legacy `#if !AQUA_TRANSPORT_MQTT` guards and the two self-closing shims
  are not stylistically uniform. Both forms compile; leave them until a node is
  actually reverted, then tidy.

## Bench verification of patch 01 (2026-10-09)

Patch 01 was run against the dev stack with real hardware, not simulated. The firmware
was built from a **fresh clone with only this patch applied**, so what ran on the board
is what the patch produces — not a working tree that had drifted from it.

```
ESP32-S3 (env: virtual-s3, sensor_sim)
  -> Windows Mobile Hotspot
  -> relay on the Windows side of the WSL boundary
  -> amqtt dev broker (loopback only)
  -> gateway -> rollup_1min -> SSE -> dashboard
```

What was confirmed working:

| Behaviour | Evidence |
|---|---|
| MQTT session holds indefinitely | 45 s with one connection and no reconnects |
| Schema-v2 envelopes accepted | gateway archives and rolls them up |
| Timestamps are UTC and current | `2026-10-09T16:02:00Z`, 5-min buckets populated |
| Field mapping | all 12 fields present, e.g. `od_do_mg_l 7.177`, `od_sat_pct 90.3` |
| Repeated envelopes | one per flush, every 5 s, not just the first |
| Commands reach the node | `POST /api/commands` -> `start_monitor` executed on the node |
| Acks settle the outbox | `command_acknowledged`, row marked acknowledged |
| Presence and LWT | `node_online` / `node_offline` on connect and drop |
| Dashboard renders live | 1/4 nodes live, od_sensor green, clock `rtc`, offset 1.1 s, -70 dBm |

### Four bugs this run found, and why they matter

None of these were visible from reading the code, and the first two produced *silence*
rather than errors — the same failure mode as the archive bug that motivated this
migration.

1. **`EnvelopeBuilder` was single-use.** `begin()` guarded on `began_` and never cleared
   it, so with a long-lived builder — which is the only sensible way to use it — the node
   published **exactly one envelope and then went quiet for as long as it ran**. The host
   test built a fresh builder for every case, so it tested the class and never the usage.
   Fixed, with a reuse test verified to fail 8 checks against the old behaviour.
2. **`t_dev_ms` was five hours early.** The node's clock is local; schema v2 defines
   `t_dev_ms` as UTC, and the legacy path added `LOCAL_TZ_OFFSET_SEC` to convert. The
   transport dropped it. Every value was correct and every envelope parsed, but each
   sample was filed five hours in the past, so every "last hour" query returned nothing
   while the data sat on disk.
3. **The flush path failed silently.** Five `return false` branches with no logging, so
   "never ran", "envelope overflowed" and "publish refused" were indistinguishable from
   outside. Every branch now logs a reason. That instrumentation is what found (1) and (2).
4. **`onMessage` discarded the topic**, making a message arriving on an unexpected topic
   indistinguishable from one with a malformed payload. Those have completely different
   fixes, and the topics are now logged.

### Gateway bugs found by the same run

These are in this repo, not the firmware, and all three were invisible on an empty
database — which is the state every fresh install starts in.

1. **The `nodes` table was never populated.** `001_core.sql` created it and nothing ever
   wrote to it. `control_commands.node_id` is a foreign key to it, so **every operator
   command returned a 500**. The dashboard, telemetry and presence all looked healthy.
2. **`GET /api/commands` disagreed with `POST /api/commands`** about what a command looks
   like: the list path returned raw rows, so `target` arrived as a JSON string and
   `superseded` — computed at issue time, not a column — was absent. `command.superseded
   .length` in the UI then threw and took the page down.
3. **`LiveCache` never recorded `schema_version`**, so it stayed 0 and the dashboard told
   the operator that working, migrated firmware had not been migrated.

---

## Second bench run (2026-10-09, later): the command and plot paths

The first run proved the transport. This one drove every command the node accepts
and read the result back out of the archive, which is the only way to see the
figures an operator actually gets.

| Check | Result |
|---|---|
| Envelope `t_dev_ms` | correct epoch |
| Per-sample `t_dev_ms` in a 5-sample batch | 5 distinct values, 1001 ms apart |
| Experiment id on the wire | `EXP-2026-10-09-01` on every batch |
| Rollup rows for that experiment | filed under the real id |
| `GET /api/samples?range=10m` | 308 points, 308 distinct |
| `GET /api/history?range=10m` (what the chart used) | 1 point |
| `start_monitor`, `set_sampling_rate` | acknowledged |
| Unknown action | **failed** (previously acknowledged) |
| `start_experiment` with an unknown id | 400 naming the experiment (was 500) |

Five more defects, all of which had to be found on hardware because each one is
*silent on the host*:

1. **Epoch milliseconds saturated to `INT32_MAX`.** `formatNumber` emitted
   precision-0 numbers through a 32-bit `long`. On the ESP32 that cast clamps, so
   every per-sample `t_dev_ms` was exactly `2147483647` — 1970 — while the envelope
   timestamp on the same message was correct. The samples filed under 1970 and every
   "last hour" query returned nothing, with no log line anywhere. The host test
   could not catch it: `long` is 64 bits there, so the same code is correct. Fixed
   as 64-bit plus a `static_assert`, which is the guard that actually fires.
2. **`exp` was dropped in the node's command adapter.** v2 carries the experiment at
   the envelope level; the legacy handler reads `experiment_id` inside the payload.
   The adapter copied `action` and `target` and silently omitted the id, so *every*
   run was labelled `exp_default` and monitor data and experiment data shared a
   group in the archive — the exact outcome the field exists to prevent.
3. **An unknown action was acknowledged as applied.** No final `else` in the
   dispatch chain meant `success` stayed true, so the gateway recorded a green
   "acknowledged" for an action the firmware never performed. A mistyped action now
   reports `failed`. An acknowledgement that can mean nothing makes the whole channel
   untrustworthy, including for the commands that do work.
4. **`start_experiment` was impossible to issue.** `control_commands.experiment_id`
   is a foreign key and nothing in the gateway ever inserted into `experiments`, so
   the table was permanently empty. Every attempt raised
   `ForeignKeyViolationError`, which is not a `CommandError`, so it escaped the
   handler as a bare `500 Internal Server Error`. Added `POST /api/experiments`, and
   the reference is now checked up front, turning the 500 into a 400 that names the
   problem.
5. **Rollup downtime was measured against a hardcoded minute.** With the 15 s bucket
   the dev stack uses, an expected count of 75 produced "37 s of downtime" — a figure
   larger than the bucket it described. Worse, `samples_expected == 0` was reported as
   a *full* minute of outage, and idle cadence (1/min) rounds to 0 in any bucket
   shorter than a minute. A node delivering 57 samples was written out as 60 s down,
   so a **monitor run would have shown as a dead line** — in the mode an operator
   watches to check sensor integrity.

Also fixed: the reserved per-sample `t_dev_ms` was being stored as a live *metric*,
so the node's clock was published through `/api/live` as though a sensor had measured
it. The rollup never had that problem because it is driven by `METRIC_SPECS`; only
the live cache reads the row's keys.

Coverage added: `testPerSampleTimestamp` (verified to fail 5 checks when the instant
is narrowed), `test_sample_time_and_unknowns.py`, `test_experiment_ids.py`, and the
two narrow-bucket downtime cases (verified to fail when the old formula is restored).

**The unattributable-experiment path is now closed, on both sides.** Deleting the
`exp_default` sentinel stopped the firmware *inventing* an experiment id, but a client
that sent `start_experiment` with no id at all still got an experiment started, and its
samples went out with `exp: null` — landing in the archive's "no experiment" group
beside monitor data. That is the same grouping confusion the sentinel caused, only
quieter, so the id is now required rather than defaulted:

- The **gateway** refuses it in `CommandOutbox.issue`. The existing reference check only
  ran when an id was present, so omitting one skipped validation entirely and the command
  was queued and published. It now raises `missing_experiment_id`, which
  `/api/commands` maps to a 400. Covered by
  `test_start_experiment_without_an_id_is_refused`, verified to fail when the guard is
  removed.
- The **firmware** refuses it too, because an HTTP or BLE client reaches the node without
  passing through the gateway. It logs the reason and acknowledges `success: false` with
  `error: "missing_experiment_id"`, the same shape the unknown-action case uses, rather
  than starting a run nothing can be attributed to.

Measured against the running stack: no id is `400 missing_experiment_id`, an unknown id is
still `400 unknown_experiment`, `start_monitor` without an experiment is still `200`, and
`start_experiment` with a real id is still `200`. The firmware half is **build-verified
only** — no board was attached. The neighbouring `strncpy` calls are now explicitly
terminated, matching what the `set_state` path already did: without it an id longer than
the buffer leaves the tail of the previous run's id in place and produces a name that is a
concatenation of two experiments.

**A bucket spanning a mode change no longer holds a mismatched pair.** The cause was narrower
than the symptom suggested. `_Bucket.experiment_id` is set when the bucket is opened, and
`_samples_expected` reads it at flush time to choose the experiment or idle cadence — so within
a single write the two always agreed. They could only disagree when the same `(role, bucket)`
is written **twice**, and the upsert refreshed the aggregates, `samples`, `samples_expected`,
`downtime_s` and `uncertain_frac` while leaving `experiment_id` at its first-write value. The
row was then half from one flush and half from the other, with nothing in it to say so.

Reaching that conflict is ordinary rather than exotic: an envelope that arrives after its
minute was flushed re-creates the bucket and writes the same key again, and the aerator's
25-sample batches make a late arrival a matter of one bad second. Measured on the dev stack's
Postgres, writing a bucket as idle and then again as an experiment:

| | `experiment_id` | `samples_expected` |
|---|---|---|
| before | `None` | `300` — the experiment count, under no experiment |
| after | `EXP-PROBE-01` | `300` |

The fix is the rule, not just the column: every inserted column except the conflict key is now
refreshed, and a test asserts that invariant so the next column added cannot fail the same way.
It is verified to fail against the pre-fix clause. That test exists because the rollup's other
tests use a fake database which records rows but never runs the SQL — `ON CONFLICT` was
untested there, and a mistyped column name would have surfaced only in production.

### Still outstanding

- Stale `deadline_ms` was genuinely not enforced by this patch. **Superseded:** all four
  nodes check it now — see *Cross-cutting fixes applied to all four nodes*, which also
  records the fail-open defect found on the way (a 256-byte probe buffer too small for the
  envelope, so `deserializeJson` failed and the check disabled itself without a word).

---

## Bench verification of patch 02 (2026-10-09): the pump

The pump node is an `esp32dev`, and the bench board is an ESP32-S3. The repo already had
`[env:esp32-s3-virtual]` — S3 board plus the virtual pump driver — so no throwaway env was
needed this time. It exercises the transport, the command surface and the batch/ack paths; it
does **not** exercise the real peristaltic head, the SSR or the AS5600, which is what
`pump_bus_real.cpp` is for.

| Check | Result |
|---|---|
| Boot, WiFi, MQTT connect, presence | online, `schema_version: 2`, `t_src: ntp` |
| All 7 declared fields | present and correctly named |
| `dose` → `dose_started` / `dose_completed` | acknowledged, event carries the experiment id |
| `force_on` / `force_off` / `emergency_stop` / `stop` | acknowledged |
| `set_state` IDLE / ACTIVE_EXPERIMENT / MANUAL_OVERRIDE | acknowledged |
| `stop_experiment` | acknowledged (was rejected) |
| Unknown action | **rejected** |
| Broker outage, 80 s | **no resets**; one envelope of **17 samples, 17 distinct timestamps** |
| `GET /api/samples?role=pump` | 122 points, 122 distinct |

### Seven defects, four of which no amount of reading would have found

1. **The patch was stale.** The protocol fixes from the sensor work (`integer64`, the 64-bit
   `formatNumber`) had been synced into this node's `lib/aqua_protocol/` but the patch file
   predated them, so applying it produced firmware with the `INT32_MAX` timestamp bug. Caught
   by grepping the patch for `integer64` before trusting it.

2. **`config.h` silently rejected every build-time override.** Only `AQUA_TRANSPORT_MQTT` was
   `#ifndef`-guarded; the broker settings were plain `#define`s. The block's own comment said
   "every one of these is overridable with `-D`", and it was not: guarding the outer macro
   skips the *block*, not the settings inside it, so `-DAQUA_MQTT_HOST=...` lost to the file
   every time. This cost nothing visible until a bench run needed a different broker, which is
   exactly the purpose the comment claims. Every setting now has its own guard, and the fix is
   proved by grepping the ELF: the override build contains `192.168.137.1` and not
   `aqua-station.local`, and `strings` shows the reverse for the default build.

3. **The node never acknowledged anything.** `s_mqtt.publish(topic, (const uint8_t*)payload,
   /*retained=*/false)` — PubSubClient overloads on the payload pointer type:
   `publish(topic, const char*, bool)` reads the third argument as `retained`, while
   `publish(topic, const uint8_t*, unsigned int)` reads it as the length. The cast selects the
   *length* overload, so the ack was published with **length 0**. It compiles, it reads
   correctly, the intent is even in a comment the compiler ignores, and the gateway drops the
   empty payload on JSON decode — so every command sat at `published` forever. The sensor was
   spared only because it passes a `char*`. Visible symptom before the fix: a `dose` that had
   demonstrably executed was later written down as `superseded`.

4. **A broker outage rebooted the node, discarding the batch.** `rst:0xc (RTC_SW_CPU_RST)`
   twice, ten seconds apart, across a 50 s outage, with the sequence counter restarting each
   time. The sensor configures a 60 s task watchdog explicitly; the pump configured none, so
   the Arduino default (5 s, idle tasks watched) applied, and `PubSubClient::connect` then
   busy-waited up to its 15 s default socket timeout for a CONNACK that never came. A broker
   that accepts the TCP connection and answers nothing starves core 0's idle task, the
   watchdog fires, and the node reboots — repeatedly, because it retries. **Store-and-forward
   keeps the batch in RAM, so a reset discards exactly the data an outage makes worth
   keeping.** Fixed by configuring the same 60 s watchdog the sensor uses, and bounding the
   socket timeout at 5 s so the reconnect cannot stall for 15 s in the first place.

5. **`stop_experiment` was rejected**, so the orchestrator's end-of-run transition would fail
   on the pump and nowhere else, reading as a broken node rather than one with nothing to
   stop.

6. **`set_state MANUAL_OVERRIDE` was rejected.** Only IDLE and ACTIVE_EXPERIMENT were handled.
   For a pump both mean the same thing — stop dosing, operator drives — so rejecting it made
   the orchestrator's transition to manual mode fail on this role alone.

7. **No per-sample `t_dev_ms`, and no experiment attribution at all.** Ten of the pump's twelve
   properties depend on these two; the events hardcoded `"experiment_id":null` and the
   envelope never called `setExperiment`, so a dose was recorded against no experiment and a
   batch spanning an outage collapsed onto one instant. Measured after the fix: an 80 s outage
   produced **one envelope carrying 17 samples with 17 distinct timestamps**, deltas ~5002 ms.

### A gateway defect this run found, affecting every role

Issuing a command while an earlier one for the same role was still pending returned a bare
**500**. `control_commands.superseded_by` is a foreign key back to its own table, and
`_persist` ran the supersede `UPDATE` **before** the `INSERT` that creates the row it points
at. Postgres raises `ForeignKeyViolationError`, which is not a `CommandError`, so it escaped
`/api/commands`'s handler. It only fires when the earlier command has not been acknowledged
yet — i.e. when an operator changes their mind a second later — which is why it read as
intermittent. Found by issuing a `dose` a few seconds after a `set_state`.

The two statements are now in the other order. That is safe because `_SUPERSEDE_SQL` already
excludes the new id (`id <> $1`); without that guard the new command, being `queued` and a
superseding action, would retire itself. The existing test that asserted the old order was
asserting the bug, and now asserts the order that can work.

### Still outstanding for patch 02

- Verified on the virtual pump driver, **not** the real head. `test/host/test_pump_bus.cpp`
  does exist, but it compiles `pump_sim.cpp` — the *device model* — so it does not narrow this
  gap: it checks deadband, duty-to-rpm scaling and encoder accumulation against a simulated
  pump. Unverified against hardware: `pump_bus_real` itself, the SSR output, the AS5600 over
  I²C, and the dose volume actually delivered. `platformio.ini` selects `pump_bus_real.cpp` for
  the `esp32dev` environment, which is the one that ships — so the code that runs in production
  is the code no test has executed.

  **Blocked on hardware.** It needs a peristaltic head with the SSR and AS5600 wired to an ESP32
  dev board. Only the aerator's board is attached to this machine, so no statement about the
  pump anywhere in this file is hardware-verified. That is a limit of the bench, not a result.
- **Resolved — the declared cadence now describes the firmware.** `roles.yaml` declared
  `expected_rate_hz: {experiment: 5.0, idle: 0.0167}` for the pump, and the mixer declared the
  same. The firmware publishes every `TELEMETRY_INTERVAL_MS` (**3000 ms**) while running and
  every **20000 ms** idle — `config.h` for the interval, `cloud_worker.cpp` for the idle branch —
  which is 0.333 Hz and 0.05 Hz. The rollup divides by the declared rate, so at 5.0 Hz a healthy
  running pump was written out with **~56 s of downtime per 60 s bucket**, in the very mode an
  operator watches to confirm the system is alive.

  The identical value on all four roles is what settled the direction: `0.0167` is one sample a
  minute, and neither the pump's nor the mixer's idle is a minute. That is copy-paste, not three
  deliberate targets. The two roles now declare `{0.3333, 0.05}`; the sensing nodes are
  untouched, because their 5 Hz is real — the aerator's `TELEMETRY_SAMPLE_INTERVAL_MS` is 200.
  A test pins all four, since the failure mode is a number that looks plausible.

  If 5 Hz *was* the intent for the mixer and pump, then the firmware is what should change and
  the registry should move with it. These numbers describe what the nodes send; the 5 Hz target
  in `docs/ARCHITECTURE.md` and `docs/DATA-FORMAT.md` is a statement about the sampling nodes.
- Stale `deadline_ms` was still not enforced by this patch, as in patch 01. **Superseded:**
  all four nodes check it now — see *Cross-cutting fixes applied to all four nodes*.

## Bench verification of patch 03 (2026-10-09): the mixer

Patch 03 was run on real hardware, from a **fresh clone with only this patch applied**, so
what ran on the board is what the patch produces.

```
ESP32-S3 (env: esp32-s3-virtual, injected bus + T-200 simulator)
  -> Windows Mobile Hotspot (board at 192.168.137.19)
  -> relay on the Windows side of the WSL boundary (192.168.137.1:1884)
  -> amqtt dev broker (loopback only)
  -> gateway -> archive -> dashboard
```

The relay had to be restarted through WSL interop, and the broker address is a build-time
override. `PLATFORMIO_BUILD_FLAGS` is passed through `shlex.split`, which **strips quotes**:
`-DAQUA_MQTT_HOST="192.168.137.1"` arrives at the compiler as a numeric literal and fails
with `too many decimal points in number`. The quotes must be escaped —
`PLATFORMIO_BUILD_FLAGS='-DAQUA_MQTT_HOST=\"192.168.137.1\" -DAQUA_MQTT_PORT=1884'` — and
the override was confirmed in the ELF rather than assumed: the binary contains
`192.168.137.1` and does **not** contain `aqua-station.local`.

What was confirmed working:

| Behaviour | Evidence |
|---|---|
| MQTT session through the relay | `[AQUA] mqtt 192.168.137.1:1884 as aqua-mixer` then `mqtt connected` |
| Schema-v2 telemetry | `aqua/tlm/mixer`, board envelopes carry 1 sample each at `seq 13-16` with a real varying RSSI (`-40/-31/-41`), distinct from the virtual node's 5-sample batches at `seq 5059+` |
| **Per-sample `t_dev_ms`** | every sample carries its own `t_dev_ms`, e.g. `1791592270475` |
| Field coverage | all eight declared `mixer_fields`, including `drv_stall` |
| Attribution is null, not a sentinel | `exp: null` on an unattributed run, never `"idle"` |
| Commands reach and are applied | gateway publishes `aqua/cmd/mixer`; the board acks with `detail:"applied"` |
| **Survives a broker outage** | relay killed for **95 s** with the broker unreachable: **0 resets** |
| Reconnects afterwards | telemetry resumes, `flush ok ... seq 64-69` |

### Three defects, and one of them was mine

1. **The watchdog was registered but never fed — the node rebooted every 60.3 seconds.**
   `esp_task_wdt_add(NULL)` in `setup()` registers `loopTask`, and nothing in `loop()`
   resets it, so the task watchdog fired on a fixed cycle whether or not the network was
   healthy. Measured: boots at 11.7 / 72.3 / 132.8 s, gaps `60.3, 60.3`, and
   `E (60237) task_wdt: - loopTask (CPU 1) ... Aborting`. This is **worse than the bug it
   was meant to fix**, because it reboots unconditionally. It survived review and two
   failed outage tests because every observation window was shorter than 60 s — a periodic
   fault only shows up in a window longer than its period. Fixed by feeding the watchdog at
   the top of `loop()`, with the idle tasks left on the watch list via `idle_core_mask`,
   which is what still catches the real fault: a core-0 task blocking long enough to starve
   idle0. Re-measured afterwards: **0 reboots and 0 `task_wdt` messages in 155 s**, then
   **0 resets across a 95 s broker outage**, and later **0 reboots across 215 s against a broker
   that accepts connections and never replies** — the case the 95 s test could not cover, since a
   refused connection returns immediately. That second test is described under *Still outstanding
   for patch 03*, where the gap it closes was recorded.

2. **The dev broker leaks retained messages across subscriptions.** A client subscribed to
   *only* `aqua/cmd/mixer` is delivered `aqua/presence/*`. Every leaked message carries
   `retain=True`; live command delivery is correctly filtered. So the node's log line
   `command had no action; ignored` was a **presence** payload arriving on the command
   subscription — the broker made a non-existent malformed command look real, and chasing
   it wasted a bench cycle. This is `tools/dev-broker.py` (amqtt), not the firmware. The
   Pi runs mosquitto, so production is unaffected; the node's behaviour was correct and
   defensive — it dropped a payload it could not act on and said so.

   **Fixed.** `_handle_client_session` walks the broker's *global* subscription registry and
   replays retained messages for every filter, without ever checking that the connecting session
   holds it — the comment above the loop describes exactly that condition and never tests it.
   The dev broker now supplies the missing condition, so a session is replayed only the filters
   it actually holds, which preserves the case the comment describes (a returning session with
   persisted subscriptions). Verified by A/B on spare ports: with a second session holding an
   unrelated filter, stock amqtt delivered *both* retained topics to a subscriber that asked for
   one, and the patched broker delivered only the requested one. Live afterwards, a subscriber to
   `aqua/cmd/mixer` receives nothing while a subscriber to `aqua/presence/#` still receives all
   four roles. amqtt 0.12.1 is the current release, so no upgrade carries this fix.

3. **Patches 01 and 02 do not apply to a clean clone.** Both were generated as
   `git diff HEAD` (uncommitted changes only), but each node repo carries 4-5 **unpushed**
   commits whose code forms the hunk context. Verified empirically: patch 02 fails
   `git apply --check` against a fresh clone of `nodo-bomba` (`platformio.ini:18`,
   `src/main.cpp:26`). Patch 03 is generated against the published base instead —
   `git diff origin/main` — which makes it self-contained. **01 and 02 should be
   regenerated the same way.**

A fourth, smaller one: `test/host/__pycache__/*.pyc` is tracked in the node repo and
`__pycache__` is not in `.gitignore`. It turned the first `git diff origin/main` patch into
a binary blob that `git apply` refused outright. The patch excludes the path; the tracked
bytecode is still there and should be removed.

### Note on flashing

A failed `esptool write_flash` erased the bootloader and application partitions and then
died mid-write, leaving the board crash-looping in ROM (`LoadProhibited`, then repeating
`IllegalInstruction`). The panic output breaks esptool's sync, so every upload method fails
— `pio upload`, `--no-stub`, 115200 and 460800 baud, compressed and uncompressed — until
the board is put into download mode by hand (hold **BOOT**, tap **RST**, release
**BOOT**). Worth knowing before the next bench session: a mid-write failure is not
recoverable from the host side.

### Still outstanding for patch 03

- `deadline_ms` was still not enforced by this patch, as in patches 01 and 02.
  **Superseded:** all four nodes check it now — see *Cross-cutting fixes applied to all
  four nodes*.
- **Corrected claim.** An earlier revision of this file said the mixer declares no
  `expected_rate_hz`. It does: `roles.yaml` line 101 declares
  `{experiment: 5.0, idle: 0.0167}`, identically to the other three roles. The error came from
  reading a truncated grep of the role block and treating the end of the excerpt as the end of
  the data — the same mistake that produced the `peer_temp_c` claim corrected above.

  What was actually wrong was the *registry*, not the firmware: the mixer pushes one sample every
  `TELEMETRY_INTERVAL_MS` (**3000 ms** running, 20000 ms idle), so it publishes ~0.33 Hz while
  the registry expected 5 Hz. The rollup divides by the declared rate, so most of every bucket
  was recorded as downtime. The pump had the identical mismatch. The sensor and aerator do not:
  both sample at 5 Hz and batch into envelopes, so they matched all along. **Both mismatches are
  now fixed** by making the registry describe what the nodes send — see *Still outstanding for
  patch 02*, which also records why the identical declaration on all four roles is what
  identified this as copy-paste rather than three deliberate targets.
- **Resolved — the silent-broker case is measured, and the earlier note here was the gap.**
  It said the 60 s watchdog had only been exercised against a relay that *refuses* connections,
  which is the easy case because `connect()` returns at once, and that a blocking `connect` was
  therefore "not proven". It is now proven against a listener that accepts TCP and never sends a
  CONNACK, which is the shape that can starve the watchdog: each retry waits out the client's
  socket timeout instead of failing fast.

  The listener accepted **52 connections over 215 s** — one per retry, each held open rather
  than closed, since closing would send a FIN and restore the easy case. The node kept its 1 Hz
  control loop running throughout (**206 iterations**) and **never rebooted**: 0 boot banners,
  0 `task_wdt` messages. Restoring the real broker afterwards, it resumed publishing at once
  (`flush ok: 7017 bytes, 25 samples`) with no reset, so it had been retrying rather than wedged.
  The bound that matters is the client's 5 s socket timeout against the 60 s watchdog.

## Bench verification of patch 04 (2026-10-09): the aerator

Run on real hardware from a **fresh clone with only this patch applied**, over the same
path as patch 03 (board → Windows hotspot → relay → amqtt → gateway).

This is the node the apply-order notes single out, and the reason is visible in the code it
replaces: a TLS handshake on a 1.5 s command poll, tightening to 1 s in manual mode.

| Behaviour | Evidence |
|---|---|
| MQTT session | `[AQUA] mqtt 192.168.137.1:1884 as aqua-aerator` → `mqtt connected` |
| Batched telemetry | one envelope per 5 s holding **25 samples** (6235-6667 B), matching the node's 200 ms sampling over a 5 s window |
| **Per-sample `t_dev_ms`** | 25 **distinct** instants, exactly 200 ms apart, `t_src: ntp` |
| Field coverage | all 13 declared fields (12 driver/control plus `peer_temp_c`) and `t_dev_ms` |
| `ctl_mode` as an enum | sent as text `"manual"`, not an index |
| Attribution | `exp: null` on an unattributed run, never the legacy `"backend_resolved"` marker |
| Commands | `set_mode` → board ack `applied` |
| Unknown commands | an unrecognised action → board ack **`rejected`**, not silence |
| **Survives a broker outage** | relay killed for **95 s**: **0 resets**, then reconnected (seq 14-17) |

### Three defects this run found

1. **A stack buffer overflow that panicked the node every 5 seconds.** The transport was
   adapted from the mixer, and `buildFields` writes **13** entries for this role while the
   array in the flush was still the mixer's `[9]`. The copy ran off the end of a stack
   array and the crash landed on the first telemetry flush — the first moment the batch is
   serialised — so the node booted, flushed once, and died, endlessly. Fixed by naming the
   count (`kSampleFieldCount`) and sizing the array from it, so the two cannot drift.
   *This is the cost of adapting a file rather than writing it: a literal that was correct
   in its original role became wrong in the new one, and nothing in the build said so.*

2. **An unrecognised action was never acknowledged.** The command ladder ended without an
   `else`, so an action this node did not know took no branch and acknowledged nothing; the
   gateway held it pending until the deadline expired. Silence is indistinguishable from an
   offline node, and it is the harder of the two to diagnose. There is now a final `else`
   that acknowledges `success=false`.

3. **The clock was unanchored, so every sample stamped `t_dev_ms` 0.** The node's only time
   source was the DS3231 on the OD logger, relayed over ESP-NOW — which does not exist when
   the logger is out of radio range, and never exists on the bench in virtual mode. All 25
   samples of a batch therefore collapsed onto one instant, which is precisely the failure
   per-sample timestamps were introduced to prevent. Fixed with `configTime()` on connect
   plus a `gettimeofday` fallback.

Defect 3 is worth generalising: **a time source that is only reachable in the field cannot
be validated on the bench**, so the bench silently exercises the *absence* of time. Any node
whose clock comes from a peer needs its own fallback, or its telemetry is untestable.

### Also repaired

`platformio.ini` had a scrambled `[env:esp32dev-ota]` — `uploadAERADOR_VIRTUAL=1` where
`upload_flags =` belonged, with orphaned `--port=3232` / `--timeout=60` lines inside
`build_src_filter` — and `build_AERADOR_VIRTUAL=1` in `[env:lora32]`. A bad find/replace of
`-DODRIVE_VIRTUAL=1` appears to be the cause. Both sections are restored.

- **`peer_temp_c` was declared but never published.** `roles.yaml` lists it for the aerator
  (unit °C, precision 2, `legacy_source: water_temp`, `authoritative: false`) alongside its
  companion `peer_do_mg_l`, and `SharedTelemetry` carries the value — but nothing wrote it
  into the envelope, so the field existed in the schema and in RAM and reached neither the
  archive nor the dashboard. Both `peer_*` fields are informational: they arrive from the OD
  logger over ESP-NOW for this node's own control loop, and `od_sensor` stays authoritative
  for dissolved oxygen and water temperature.

  *An earlier revision of this file claimed `peer_*` was undeclared in `roles.yaml`. That was
  wrong — it came from reading a regex-truncated excerpt of the role block and treating the
  end of the excerpt as the end of the data. The declared-but-unpublished state is the harder
  bug of the two, because every layer looks correct in isolation.*

### Patch base

Patch 04 is a diff against **`2182892`** (the published `main` at the time of the migration),
not against `origin/main` as it stands now. Once the firmware commits were pushed, `origin/main`
moved past the migration, and `git diff origin/main` would have produced a patch containing only
the last few uncommitted lines rather than the whole change. The base is tagged
`pre-schema-v2-mqtt` in the node repo; regenerate with
`git diff pre-schema-v2-mqtt -- . ':(exclude)**/__pycache__/*'`.

Patches 01 and 02 needed the same treatment. They have it now: each was generated as
`git diff HEAD`, which captures only the *uncommitted* changes, while the hunks depend on the
node repos' unpushed virtual-bench commits. Both were committed, tagged and regenerated, and
each now applies clean to a fresh clone and builds.

### Patch bases

Every patch is a diff against a fixed commit, tagged `pre-schema-v2-mqtt` in its node repo.
Regenerate with the tag, never with a branch:

| Patch | Node | Base | Tag |
|---|---|---|---|
| 01 | `nodo-sensor-od` | `37f1e04` | `pre-schema-v2-mqtt` |
| 02 | `nodo-bomba` | `e1d6512` | `pre-schema-v2-mqtt` |
| 03 | `nodo-mezclador` | `ecc5956` | `pre-schema-v2-mqtt` |
| 04 | `nodo-aerador` | `2182892` | `pre-schema-v2-mqtt` |

The mixer's base is `ecc5956`, **not** its local branch tip. That repo's branch had four
commits on top of `origin/main` when patch 03 was generated, and the patch was diffed against
`origin/main` — so the base is the published commit, and the local commits are part of the
patch. Tagging the branch tip instead produced a patch that did not apply; the tag was
corrected after re-verifying.

All four bases are verified the same way: clone, check out the tag, `git apply --check`,
then build every environment including the `AQUA_TRANSPORT_MQTT=0` kill-switch.

### Cross-cutting fixes applied to all four nodes

These postdate the individual bench runs, so the *Still outstanding* lists under each patch
below still mention them. They are no longer open.

**Command deadlines are now enforced on the device.** The gateway stamps `deadline_ms` when
it issues a command; nothing on any node ever read it, so a command that arrived late —
queued while the session was down, or redelivered — was carried out exactly as if it had
just been requested. An operator's intent expires, and acting after it does is not neutral:
it moves an actuator for a reason nobody is waiting on any more.

Enforced in each transport's `onMessage`, on a rule that is now shared by all four nodes (see
below), and skipped when the device clock is unanchored (before 2020) — comparing an epoch
deadline against an unanchored clock would reject *every* command, a worse failure than the one
being fixed.

Bench-verified on the aerator, and the first attempt **did not work**:

| | |
|---|---|
| A 256-byte probe document | too small for the ~230-byte command envelope with its nested `target` |
| `deserializeJson` therefore failed | and the check returned `false` |
| Result | the whole feature silently disabled, with every layer still looking correct |

The probe is 768 bytes and a parse failure is now logged rather than swallowed. Re-verified:
a stale command is rejected (`expired 60328 ms ago`) and a fresh one is applied. The lesson
generalises — **a fail-open path that is silent is indistinguishable from a feature that
works**, and only a bench test with a deliberately expired command could tell them apart.

**The deadline and clock rules are now one implementation rather than four.** The check above
was written into each node's `aqua_link.cpp` as four near-identical copies, and they had
already drifted:

| | sensor | pump | mixer | aerator |
|---|---|---|---|---|
| plausibility threshold | 2020-01-01 | **2020-09-13** | 2020-01-01 | 2020-01-01 |
| clock used for the check | inline `gettimeofday` | `aquaLinkNowMs()` | `aquaLinkNowMs()` | `aquaLinkNowMs()` |

A clock reading 2020-05-01 was reported `monotonic` on the pump and `ntp` on the other three,
and the pump's own comment described the pre-2020 case it was not in fact implementing. The
256-byte probe that silently disabled the feature had to be corrected four times, which is how
one copy-paste bug becomes four.

The rule now lives in the vendored library — `aqua::deadlinePassed`, `aqua::extrapolateEpochMs`,
`aqua::plausibleEpochMs` and `aqua::kPlausibleEpochS` — so all four nodes get one definition and
the host test can reach it. `aqua_protocol.cpp` took no Arduino headers before and still takes
none, which is what makes that possible. Only the clock *source* stays per-node, because that
legitimately differs: the sensor owns a DS3231, the pump has nothing but SNTP.

**This is the gap the coverage note above described, now closed.** The conformance test compiled
the serialiser alone and could never reference `aqua_link.cpp`, which needs `PubSubClient.h` and
`WiFi.h`. The extracted functions are reachable from plain `g++`, and the suite went from 108
checks to 126. They cover both sides of every boundary, including the `uint32` uptime wrap at
49.7 days — where a *signed* difference produces an extrapolated clock that jumps **backwards**
by 49 days. The fail-safe guard was mutation-checked: replacing the plausibility test with a
bare comparison against zero fails the suite. That check is also what showed the zero form is
redundant for the deadlines the callers actually produce, so that test pins a rule against a
future caller rather than a live defect, and it says so.

The five copies of the library — the canonical one and the four vendored — are md5-identical.

**Bench-verified on the aerator**, which is the point of extracting a rule rather than trusting
the four rewritings: a fresh command with a deadline 2 minutes out was answered
`result: applied`, and one whose deadline had passed 10 minutes earlier was answered
`result: rejected, detail: deadline_expired` — the same pair of outcomes as before the
refactor. That second answer is only reachable when the node's clock is judged *plausible*, so
it also demonstrates on hardware that the plausibility test accepts a real epoch reading; the
acknowledgement carried `t_dev_ms: 1791605251576`, a real instant. A stale command published
directly to `aqua/cmd/aerator` is used for this, because the gateway stamps a deadline in the
future and so cannot produce a stale one.

**The mesh now consumes `aqua/time`.** `docs/TIME-SYNC.md` tier 3 had a publisher and no
subscribers: the topic was published by the gateway from its own clock — worthless exactly
when it is needed — and nothing on the mesh ever read it. Each node now subscribes, caches
the newest broadcast with the uptime it arrived at, and carries it forward on the monotonic
clock. Sample stamping and the deadline check share one `aquaLinkNowMs()`, which prefers the
node's own NTP clock and falls back to the broadcast, so a node with an AP and a broker but
no internet no longer degrades every sample to `monotonic`.

Inbound messages are also routed by topic now. `aqua/time` is a broadcast, not a command, and
handing it to the command handler had it report `no action` — which is what made the dev
broker's retained-message leak look like a malformed command rather than a message on the
wrong subscription.

Bench-verified on the aerator: `[AQUA] rx aqua/time (65 bytes)` immediately followed by
`time broadcast 1791601806522 ms (t_src=rtc)`, and the deadline check still rejects stale and
applies fresh after moving onto the shared clock.

**The ESP-NOW packet's timestamp was documented as the wrong units.** The struct declared
`rtc_timestamp_ms` as "milliseconds since 2000"; the sender has always written Unix epoch
milliseconds, and the reader has always taken it verbatim. **The wire was correct and the
comment was the defect** — and a comment describing the wrong units is an invitation for
someone to "fix" the reader into converting a value that is already correct, which is the
exact guess `TIME-SYNC.md` §3 retires. The field is now `t_dev_ms`, with the layout
unchanged so the two ends stay byte-compatible.

**Patches 01-04 are the only ones that ever applied to a clean clone** — see *Patch bases*.
The tracked `__pycache__/*.pyc` that broke patch 03's first generation is now untracked and
ignored in all four node repos, not just the mixer.

Verify any of them with:

```
git clone <node repo> /tmp/check && cd /tmp/check
git checkout <base>          # or: git checkout pre-schema-v2-mqtt
git apply --check <patch>
```

A branch is the wrong base because pushing the firmware moves the branch past the
migration, and the regenerated patch silently collapses to only the last few uncommitted
lines — 6.7 KB instead of 133 KB in the aerator's case — while still looking like a
plausible patch file.

### Where water temperature lives

`od_sensor` is authoritative for water temperature and publishes it as `od_temp_c`
(from `readings[0]`, 2 decimals). The aerator also receives it over ESP-NOW for its own
control loop and republishes it as `peer_temp_c` with `authoritative: false` — the same
arrangement as `peer_do_mg_l`. Two nodes carrying the quantity is intended; the `peer_`
prefix and the `authoritative` flag are what keep a dashboard from treating the copy as a
second source.

### Still outstanding for patch 04

- `deadline_ms` was not enforced by this patch, as in patches 01-03. **Superseded:** all four
  nodes check it now — see *Cross-cutting fixes applied to all four nodes*.
- The virtual nodes share the real nodes' protocol node ids, so both acknowledge every
  command. Acks from the real board are distinguishable because it is the only sender that
  sets `detail`; a test that does not filter on that will attribute the virtual node's
  behaviour to the hardware.
