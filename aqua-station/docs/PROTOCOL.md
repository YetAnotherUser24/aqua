# Protocol contract — schema version 2

This document is **the contract** between firmware and gateway. Node repositories implement it;
`gateway/aqua_gateway/schema/roles.yaml` declares it; the gateway validates against it.

Changing anything here requires a `v` bump and a compatibility note. Quiet drift is how the
previous system lost 5 Hz fidelity without anyone noticing.

---

## 1. Transport

**MQTT 3.1.1 over plain TCP on the local network.** Not TLS: all nodes share one AP with the
broker, so TLS would cost ~40–60 KB RAM and 1–3 s per handshake per node and protect nothing
that the LAN does not already provide. The previous firmware used `WiFiClientSecure` with
`setInsecure()` — full TLS cost, zero validation. Dropping it is strictly better.

| Parameter | Value |
|---|---|
| Broker | `aqua-station` LAN address |
| Port | `1883` |
| Client ID | `<role>-<node_short_id>` e.g. `odrive-b000` |
| Keepalive | 60 s |
| Clean session | **false** (persistent — commands survive deep sleep) |
| Username / password | per-node, configured in Mosquitto password file |
| **Last Will** | `aqua/presence/<node_id>` → `{"online":false,"reason":"lwt"}` (QoS 1, retained) |
| On connect | publish `aqua/presence/<node_id>` → `{"online":true,...}` (QoS 1, retained) |

**QoS policy**

| Direction | Topic | QoS | Retained |
|---|---|---|---|
| Telemetry | `aqua/tlm/<role>` | 1 | no |
| Commands | `aqua/cmd/<role>` | 1 | no |
| Command ack | `aqua/ack/<role>` | 1 | no |
| Presence | `aqua/presence/<node_id>` | 1 | **yes** |
| Time | `aqua/time` | 1 | yes |
| Events | `aqua/event/<role>` | 1 | no |

QoS 1 (at-least-once) is correct here: telemetry is idempotent by `(node_id, seq)`, and duplicates
are dropped by the gateway rather than tolerated as double-counted samples.

---

## 2. Time — one meaning, no heuristics

The old format was ambiguous: `rtc_timestamp_ms` could mean *Unix epoch ms* **or** *ms since
2000-01-01*, resolved by a magic threshold (`EPOCH_MS_THRESHOLD` in the previous
`src/lib/bulk.ts`). That class of ambiguity is banned here.

> **`t_dev_ms` is ALWAYS Unix epoch milliseconds, UTC. Always.**

`t_src` declares where that clock came from:

| `t_src` | Meaning |
|---|---|
| `ntp` | Synced from the Pi's SNTP server |
| `espnow` | Set from an ESP-NOW TIME packet (peer-to-peer) |
| `rtc` | Read from a local battery-backed RTC (DS3231) |
| `monotonic` | Derived from uptime only — **lowest trust**, flagged in exports |

Retired fields: `seconds_since_2000`, `rtc_timestamp_ms` (unqualified), `datetime` strings.
See `docs/TIME-SYNC.md` for the authority chain and fallbacks.

---

## 3. Envelope

Every telemetry message, on every role topic, uses one envelope:

```json
{
  "v": 2,
  "role": "aerator",
  "node_id": "b0000000-0000-0000-0000-000000000002",
  "exp": "EXP-2026-10-09-01",
  "seq": 48213,
  "t_dev_ms": 1791548022123,
  "t_src": "espnow",
  "rssi": -61,
  "boot_id": 7,
  "samples": [ { "...": "role-specific" } ]
}
```

| Field | Required | Notes |
|---|---|---|
| `v` | ✅ | Envelope/schema version. Gateway rejects unknown majors. |
| `role` | ✅ | Must match the topic. Mismatch → reject + `protocol_error` event. |
| `node_id` | ✅ | Canonical UUID. Topic/field mismatch → reject. |
| `exp` | ➖ | Experiment id, or `null` when idle. Never `"backend_resolved"` (retired). |
| `seq` | ✅ | Per-node monotonic counter, **increments by 1 per message**, survives reboot via `boot_id`. Gaps → `telemetry_gap_detected`. Duplicates → dropped. |
| `t_dev_ms` | ✅ | Unix epoch ms UTC. |
| `t_src` | ✅ | See table above. |
| `rssi` | ➖ | dBm. Used for link-quality events. |
| `boot_id` | ✅ | Increments per boot. `(boot_id, seq)` is the idempotency key. |
| `samples` | ✅ | Array, 1..`MAX_BATCH` (200). |

**The gateway always records arrival time too** (`t_arr`), independently of `t_dev_ms`. The
difference is the clock offset, tracked per `(node_id, boot_id)` — see `docs/TIME-SYNC.md`.

---

## 4. Field naming convention

Consistent prefixes make the schema self-describing and keep the registry readable:

| Prefix | Domain |
|---|---|
| `t_` | time |
| `od_` | dissolved-oxygen probe |
| `drv_` | motor drive / actuator (generic across ODrive, ESC, pump motor) |
| `pump_` | pump-specific (dosing) |
| `pwr_` | power / battery |
| `rtc_` | real-time clock |
| `dev_` | device status |
| `ctl_` | controller mode / controller state |
| `peer_` | value received from **another node** — informational, not authoritative |

Suffixes carry units: `_ms, _v, _a, _w, _c, _pct, _ml, _rpm, _rad_s, _nm, _dbm`.
Scaled values carry the unit; the matching device-native value carries `_raw`.

**Raw/scaled pairs are kept deliberately.** `_raw` preserves calibration provenance and lets you
re-derive scaled values if a calibration constant changes; the scaled value is what analysis uses.

---

## 5. Role schemas

`roles.yaml` is the source of truth. Summarised here for review.

### 5.1 `od_sensor` — topic `aqua/tlm/od_sensor`

The authoritative source for dissolved oxygen and water temperature.

| Field | Unit | Notes |
|---|---|---|
| `od_do_mg_l` | mg/L | scaled; authoritative DO |
| `od_do_raw` | — | device-native |
| `od_sat_pct` | % | saturation |
| `od_sat_raw` | — | device-native |
| `od_temp_c` | °C | water temperature |
| `od_temp_raw` | — | device-native |
| `od_aux1_raw` | — | spare channel, purpose TBD |
| `od_aux2_raw` | — | spare channel, purpose TBD |
| `pwr_battery_v` | V | logger battery |
| `rtc_temp_c` | °C | DS3231 die temperature (drift indicator) |
| `rtc_health` | — | battery/oscillator status |
| `dev_status` | — | device status bitfield |

Renamed from: `do_milli_mg_l`, `do_sat_deci_pct`, `water_temp_centi`, `param3_centi`,
`param4_centi`, `battery_mv`/`battery_v`, `rtc_temp_centi`, `status`.

### 5.2 `aerator` — topic `aqua/tlm/aerator`

ODrive S1 driving the M8325s. Source: the `SharedTelemetry` struct.

| Field | Unit | Notes |
|---|---|---|
| `drv_rpm` | rpm | actual |
| `drv_rpm_target` | rpm | setpoint |
| `drv_vbus_v` | V | bus voltage |
| `drv_ibus_a` | A | bus current |
| `drv_power_w` | W | |
| `drv_torque_nm` | N·m | estimate |
| `drv_iq_a` | A | q-axis current |
| `drv_running` | bool | was `is_on` |
| `drv_status` | — | was `status_code` |
| `ctl_mode` | enum | `pid` \| `fuzzy` \| `manual` (was `0\|1\|2`) |
| `ctl_failsafe` | bool | was `failsafe_active` |
| `peer_do_mg_l` | mg/L | **DO echoed from the sensor over ESP-NOW** |
| `peer_temp_c` | °C | **temperature echoed the same way** |

> `peer_*` fields are **informational only**. The aerator receives them for its own control loop.
> In exports, `od_sensor` is authoritative for DO and temperature; the `peer_*` columns are
> retained as a cross-check. Never average the two.

Renamed from: `target_rpm`, `actual_rpm`, `vbus_voltage`, `ibus_current`, `power_watts`,
`torque_estimate`, `iq_measured`, `measured_do`, `water_temp`, `is_on`, `control_mode`,
`failsafe_active`, `status_code`.

### 5.3 `mixer` — topic `aqua/tlm/mixer`

T-200 via SNR8503 (ESC + FG tachometer). Note this role is specified in **rad/s and duty**, not
PWM microseconds — the previous shared schema assumed PWM percent, which never described this
device.

| Field | Unit | Notes |
|---|---|---|
| `drv_rpm` | rpm | from FG tachometer |
| `drv_rpm_target` | rpm | |
| `drv_rad_s` | rad/s | |
| `drv_rad_s_target` | rad/s | |
| `drv_duty_pct` | % | was `commanded_duty` |
| `drv_running` | bool | |
| `drv_status` | — | ESC fault word |
| `drv_stall` | bool | commanded but below the stall threshold |

### 5.4 `pump` — topic `aqua/tlm/pump`

Peristaltic dosing pump. Volume-oriented, not speed-oriented.

| Field | Unit | Notes |
|---|---|---|
| `pump_dosed_ml` | mL | accumulated this cycle |
| `pump_target_ml` | mL | dose setpoint |
| `pump_rpm` | rpm | |
| `pump_mode` | enum | `idle` \| `dose` \| `manual` |
| `pump_dose_completed` | bool | |
| `pump_dose_timed_out` | bool | |
| `drv_status` | — | fault word |

`ML_PER_REV` is a **calibration constant, not telemetry** — it belongs in config, and the value
used must be recorded in the experiment manifest so a dose can be reconstructed later.

---

## 6. Commands — topic `aqua/cmd/<role>`

```json
{
  "v": 2,
  "cmd_id": "0f2c...",
  "issued_ms": 1791548030000,
  "exp": "EXP-2026-10-09-01",
  "action": "set_speed",
  "target": { "drv_rpm": 420 },
  "deadline_ms": 1791548330000,
  "requested_by": "operator:web"
}
```

A node **must** acknowledge on `aqua/ack/<role>`:

```json
{ "v": 2, "cmd_id": "0f2c...", "node_id": "...", "t_dev_ms": 1791548030450,
  "result": "applied", "error": null,
  "applied": { "drv_rpm_target": 420 } }
```

`result` ∈ `applied` | `rejected` | `failed` | `expired`.

**Safety rules**

- `emergency_stop` is always accepted and never queued behind anything.
- A command past `deadline_ms` is refused, and the gateway raises `command_expired`. The previous
  system discovered the hard way that an override could be silently undone by a stale queued
  command — there is now an explicit `superseded_by` field so that resolution is recorded, not
  guessed.
- Persistent sessions mean a command issued while a node deep-sleeps is delivered on wake. The
  deadline is what prevents a days-old command from executing.

---

## 7. Time broadcast

**`aqua/time`** (retained, QoS 1) — published by the gateway:
```json
{ "t_dev_ms": 1791548030000, "t_src": "ntp", "uncertainty_ms": 5 }
```

**ESP-NOW `TIME` packet** — the sensor broadcasts the same payload peer-to-peer so nodes can
align with **no WiFi and no broker**. This is what keeps the mesh coherent when the AP is down;
it mirrors the existing ESP-NOW DO path in `nodo-aerador`.

---

## 8. Versioning and compatibility

- `v` is the **major** schema version. The gateway accepts the current major and one behind.
- Adding an **optional** field is non-breaking — the gateway routes unknown fields to the
  unknown-field side channel rather than dropping them silently.
- Renaming or removing a field is **breaking**: bump `v`.
- Firmware advertises its `v` in the envelope; the gateway records it per node and raises
  `protocol_version_mismatch` when it lags.
