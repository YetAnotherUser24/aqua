# Data formats and retention

Two tiers, deliberately:

| Tier | Storage | Holds | Cost per GB |
|---|---|---|---|
| Operational | PostgreSQL 17 | devices, commands, events, experiments, 1-minute rollups | higher |
| Archive | Files on SD → Cloudflare R2 | full-fidelity telemetry CSV & experiment bundles | Free tier (10 GB, $0 egress) |

A relational row costs ~200 B effectively (tuple header + columns + index + WAL share) and dirties
an 8 KB page on every write. A CSV row costs ~85 B and appends sequentially. For an immutable,
time-ordered log — which is what telemetry is — files are the correct structure and tables are a
2–3× more expensive emulation of one.

## 1. On-Pi archive layout

```
/data/telemetry/
├── od_sensor/2026-10-09.csv          # open, hot, uncompressed
├── od_sensor/2026-10-08.csv.gz       # rotated at midnight, compressed
├── aerator/2026-10-09.csv
├── mixer/2026-10-08.csv.gz
├── pump/2026-10-09.csv
├── events/2026-10.csv                # unified event stream, monthly
├── unknown/2026-10-09.jsonl          # side channel: fields the registry doesn't know
└── .manifest.db                      # SQLite: flush/verify state per file
```

Rotation, compression and pruning all run in **`America/Lima` (UTC-5)**, not the host timezone.
The Pi was previously on `Europe/London`; deploy fixes this, but the code must not depend on the
host being right.

## 2. Telemetry CSV

One file per role per day. Header row, then one row per sample, in **device time order**.

```
t_dev_ms,t_src,seq,drv_rpm,drv_rpm_target,drv_vbus_v,drv_ibus_a,drv_power_w,drv_torque_nm,drv_iq_a,drv_running,drv_status,ctl_mode,ctl_failsafe,peer_do_mg_l,peer_temp_c
1791548022123,espnow,48213,412.50,420.00,24.10,3.42,82.42,0.31,3.39,1,0,pid,0,7.874,23.05
```

Rules:

- **Column order comes from `roles.yaml`**, so a schema change is a registry change.
- `t_dev_ms` is *device* time (Unix epoch ms UTC), preserved as recorded — never rewritten. Clock
  correction is applied at **export** time so the raw archive stays faithful to what the node
  believed.
- Booleans are `0`/`1`. Enums are their string names. Empty cells mean "not reported".
- Rows are appended in arrival order, which may differ slightly from `t_dev_ms` order after a
  replay. The export sorts.
- **A buffer is released on size OR age** (`AQUA_ARCHIVE__MAX_BUFFER_AGE_S`, default 300 s),
  whichever comes first, and **every release is `fsync`ed**.

  Size alone made the exposure a property of the sampling rate: 64 KB is a fixed *number of
  bytes*, so at the 5 Hz experiment cadence it is 3.1 minutes (`od_sensor` 3.9, `aerator` 3.6,
  `mixer` 5.4, `pump` 6.0) while the od_sensor's real 0.2 Hz stretched the same block past
  **90 minutes**. The cap is what turns that back into a duration. A role that stops sending is
  released too, by the gateway's maintenance sweep — the size and age checks in the write path
  only run when a row arrives, so a quiet node's part-full buffer would otherwise sit in RAM
  indefinitely.

- **The `fsync` is what makes this one number instead of two.** A release on its own only moves
  rows out of the gateway's memory and into the kernel's page cache: a reader sees them at once,
  but the card does not have them until the kernel writes them back — up to
  `vm.dirty_expire_centisecs` (3000 = 30 s on this station). Left unsynced, that second window
  sits *outside* the cap, so the cap bounds the wrong thing. **Both** release paths sync, so the
  power-cut exposure is exactly `min(block fill, cap)` with no page-cache interval on top.
  Measured, not assumed: appending 1 MB without `fsync` moved `Dirty` from 12 kB to 1012 kB and
  back to 20 kB within 35 s.

  The cost is why the cap is 300 s rather than tighter: a release is one write *and* one commit,
  measured at **7.75 ms per 64 KB block against 0.14 ms unsynced**. At 60 s an idle-cadence role
  released 1440 times a day; at 300 s it releases 288, for the same bound.

  A graceful stop loses nothing in either stage: `close_all()` flushes *and* syncs.

## 3. Manifest — `.manifest.db` (SQLite)

```
file(path PK, role, day, rows, bytes, sha256, closed_at, compressed_at,
     flushed_at, archive_key, verified, verify_attempts, last_error)
```

`flushed_at` and `verified` are the fields that make retention safe. Nothing in the retention path
may delete a file without both.

## 4. Retention — flush-aware, space-aware, configurable

Deletion depends on **the archive holding the data**, never on the calendar alone. If the Pi is off
for a month, nothing is deleted, because nothing was flushed.

A local file becomes eligible **only when all of these hold**:

1. it is **closed** (never the file currently being written);
2. `flushed_at` is set — it exists in the archive;
3. `verified = 1` — sha256 and row count match on both ends;
4. **and** at least one of:
   - age > `retention_days`, or
   - retained bytes > `max_local_bytes`, or
   - free space < `min_free_bytes`.

If space is low and **nothing** is eligible, the gateway raises `retention_blocked_unflushed` at
`warning`, escalating to `critical` at 95% full, and refuses to delete. Fail loud, never silently.

| Setting | Default | Meaning |
|---|---|---|
| `retention_days` | 14 | Minimum age before a flushed file may go |
| `max_local_bytes` | 8 GiB | Soft ceiling for the archive directory |
| `min_free_bytes` | 2 GiB | Hard floor; triggers space-based pruning and alerts |
| `prune_mode` | `both` | `age` \| `space` \| `both` |
| `keep_open_file` | `true` | Never prune today's file (non-negotiable) |

PostgreSQL and TimescaleDB retention, if ever enabled, must be **≥** local retention — otherwise
you delete the queryable copy before the archive has it.

## 5. Cloudflare R2 archive layout

Object storage (S3-compatible API), not database rows. Cloudflare R2 offers 10 GB free tier storage with **zero egress fees**, enabling permanent, free public download of full-fidelity telemetry and historical bundles without worrying about bandwidth metering.

```
aqua-station-r2/
├── index.html                                 # 24/7 web portal & interactive Chart.js viewer
├── raw/<role>/YYYY-MM-DD.csv[.gz]             # raw sensor & actuator telemetry logs
├── telemetry/<role>/YYYY-MM-DD.csv            # uncompressed telemetry mirror
├── exports/<exp_id>/bundle.tar.gz             # aligned experiment bundles
├── exports/<exp_id>/telemetry.csv             # aligned wide CSV (browser-fetchable)
├── exports/<exp_id>_native/bundle.tar.gz      # native unresampled 5 Hz / 20 Hz bundle
└── nodo-bomba/                                # pump sysid, step response, and validation CSVs
```

Public edge access: `https://data.aqua-station.live` serves the static viewer and direct file downloads even when the Raspberry Pi gateway is powered off.

## 6. Experiment bundle

Built automatically at experiment end (via `_auto_export_and_upload()` in `engine.py`) or on demand via `POST /api/experiments/{id}/export`:

```
exports/<exp_id>/
├── manifest.json      # nodes, schema versions, clock offsets, calibration constants,
│                      # time range, participating roles, gateway version
├── telemetry.csv      # WIDE, time-aligned across all roles on a common grid
├── events.csv         # unified event stream, this experiment only
├── raw_index.json     # archive keys of the per-role raw files (not copies)
└── bundle.tar.gz      # compressed bundle uploaded to Cloudflare R2 and downloadable via HTTP
```

`manifest.json` records the **clock offset applied per node** and the `ML_PER_REV` value in force.
Without those, a dose cannot be reconstructed and an aligned dataset cannot be audited.

### Alignment method

1. Load per-role raw CSV for the experiment window.
2. Shift each node's timestamps by its measured offset (from `t_arr − t_dev_ms` tracking).
3. Resample onto a common grid (default 1 s; configurable down to the native 200 ms).
4. Join left-to-right, preserving gaps as empty cells rather than interpolating silently.
5. Mark columns whose source node had `t_src = monotonic` or an offset above threshold.
6. Prefix per-role columns with the role, except `od_*`, which is authoritative and unprefixed.

**`od_*` from `od_sensor` wins.** `peer_do_mg_l` / `peer_temp_c` on the aerator are that node's own
control-loop input, kept as cross-check columns. Never average them.

## 7. Volume model

Experiments run at most **3 hours every other day**; idle rate is configurable and should be set to
**1 minute** — idle at 1 Hz would be 75% of all volume while carrying the least information.

| Idle rate | Rows/day (avg) | Archive/yr raw | Archive/yr gz |
|---|---|---|---|
| 1 Hz | 108,000 | ~3.4 GB | ~400 MB |
| 10 s | 35,100 | ~1.1 GB | ~130 MB |
| **1 min** | **28,350** | **~880 MB** | **~100 MB** |

Against ~24 GB free after the rootfs expansion, storage is not a constraint. Spend the complexity
elsewhere.
