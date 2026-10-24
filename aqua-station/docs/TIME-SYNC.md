# Time synchronization

For a **combined multi-node dataset**, time alignment is a correctness requirement, not a nicety.
The analysis in `scripts/sysid_and_tune.m` and `advanced_thesis_tuning.m` identifies dynamics from
cross-correlations between DO, airflow and dosing. Unknown per-column time skew would quietly
invalidate exactly those results.

## 1. The problem

- Each node has an independent clock.
- **The Raspberry Pi 3 has no hardware RTC** — `timedatectl` reports `RTC time: n/a`. On power loss
  it returns with no idea of the time until it reaches the internet.
- The nodes must keep working when the AP, the broker, or the internet is unavailable. Aeration
  control must not depend on network time.

## 2. Authority chain

The deployment already contains a better time source than the Pi: **the OD sensor's DS3231**, which
is battery-backed and survives power cuts. It becomes the persistent reference for the mesh.

```mermaid
flowchart TB
  NTP["Internet NTP<br/>systemd-timesyncd"]
  RTC["nodo-sensor-od DS3231<br/>battery-backed<br/>= persistent authority"]
  PI["aqua-station<br/>SNTP server for the LAN"]
  MESH["mixer · pump · aerator"]
  NTP -->|"when available"| PI
  PI -->|"SNTP at boot"| MESH
  RTC -->|"MQTT aqua/time (retained)"| PI
  RTC -->|"ESP-NOW TIME packet"| MESH
  PI -.->|"fallback: fake-hwclock + monotonic"| PI
```

| Tier | Source | Available when |
|---|---|---|
| 1 | Internet NTP via `systemd-timesyncd` | Internet reachable |
| 2 | **Sensor DS3231 over ESP-NOW** | No internet, no WiFi — peer-to-peer only |
| 3 | Sensor DS3231 over MQTT (`aqua/time`) | ESP-NOW down, WiFi up |
| 4 | Pi `fake-hwclock` persisted + monotonic uptime | Reboot, nothing reachable |
| 5 | Node's own clock, `t_src = monotonic` | Everything else down; flagged in exports |

Tiers 2 and 3 duplicate the same information over two transports on purpose. ESP-NOW survives a
broker or AP outage; MQTT is more reliable when the AP is up but the peers are out of ESP-NOW
range. This mirrors the existing ESP-NOW DO path in `nodo-aerador`, so the mechanism is already
proven in this system.

**Recommendation: fit a DS3231 to the Pi (~$3), the same part already used on the sensor.** It
removes the last dependency on internet for time and makes tier 4 unnecessary. Given the board's
`throttled=0x50000` history (under-voltage has occurred, and power cuts are plausible), this is
cheap insurance for an always-on logger.

## 3. Protocol

One timestamp, one meaning — **`t_dev_ms` is always Unix epoch milliseconds, UTC** — plus `t_src`
naming the clock that produced it (`ntp` | `espnow` | `rtc` | `monotonic`).

This retires the previous ambiguity where `rtc_timestamp_ms` could mean epoch ms *or* ms since
2000-01-01, disambiguated by a magic threshold. Guessing is not a protocol.

`aqua/time` (retained, QoS 1):
```json
{ "t_dev_ms": 1791548030000, "t_src": "ntp", "uncertainty_ms": 5 }
```

ESP-NOW `TIME` packet carries the identical payload.

## 4. Offset measurement and correction

The gateway records, for every message, both:

- `t_dev_ms` — what the node believed
- `t_arr` — when the gateway received it

It maintains a per-`(node_id, boot_id)` estimate of:

- **offset** — the signed difference between node clock and gateway clock
- **drift rate** — offset change per hour, via linear fit over a sliding window
- **uncertainty** — spread of recent residuals

Estimation is only meaningful on **idle traffic**, because transit jitter during a burst inflates
residuals. So offsets are computed from heartbeats and idle batches, and *applied* to experiment
windows.

| Condition | Event |
|---|---|
| offset above threshold on first sync | `clock_resynced` |
| offset drifts beyond threshold mid-run | `clock_offset_exceeded` *(warning)* |
| uncertainty above threshold | `clock_uncertainty_high` |
| `t_src = monotonic` on experiment data | flagged in bundle manifest |

**Raw archive data is never rewritten.** `t_dev_ms` is stored exactly as the node reported it, and
correction is applied at **export** time. This keeps the archive an honest record of what the node
believed, while exports carry the corrected view *and* the correction that was applied.

## 5. What the export records

`manifest.json` in each experiment bundle captures, per node:

- measured offset applied, and its uncertainty
- drift rate over the window
- `t_src` history (did it change mid-experiment?)
- count of samples flagged uncertain

Without this, an aligned dataset cannot be audited after the fact — and a suspicious result could
not be distinguished from a clock artefact.

## 6. Node-side requirements

- SNTP sync at boot when the LAN is reachable; fall back to ESP-NOW, then to the local RTC.
- Accept and apply ESP-NOW `TIME` packets at any time, but **rate-limit** application so a spurious
  packet cannot step the clock under a running control loop. Prefer slewing over stepping during a
  PID cycle.
- **Never** let a time update jump the clock backwards mid-experiment; record the correction and
  keep `seq` monotonic regardless.
- Persist the last known good offset so a rebooted node starts approximately aligned.
