# Deployment

Target: **Raspberry Pi 3 Model B+ / Debian 13 (trixie) / aarch64**, verified on the
actual board: 4 cores, 905 MB RAM, 29.2 GB SD, `eth0` down, `wlan0` up, 51.5 °C
idle, `vcgencmd get_throttled` = **0x50000**.

---

## 0. Before you run anything

Four things must be true first. `deploy/install.sh --check` reports all of them and
changes nothing.

### 0.1 Expand the root filesystem — reclaim ~14 GiB

The card was flashed from a 16 GB image onto a 32 GB card, and the root partition
was never grown:

| | Bytes | GiB |
|---|---|---|
| Card total | 31,325,683,712 | 29.17 |
| `mmcblk0p1` (boot) | 536,870,912 | 0.50 |
| `mmcblk0p2` (root) | 15,386,279,936 | **14.33** |
| **Unallocated** | **15,402,532,864** | **14.35** |

**~49% of the card is unusable.** This must be fixed first, because free space is
what retention keys off.

```
sudo raspi-config --expand-rootfs
sudo reboot
```

Free space goes from 9.7 GB to ~24 GB. Do this before anything else — it is one
command and it changes every capacity calculation downstream.

### 0.2 Replace the power supply

`vcgencmd get_throttled` returned **0x50000**: bit 16 (under-voltage **has
occurred**) and bit 18 (throttling **has occurred**).

This is a more serious risk than SD wear. A brown-out can corrupt a running
PostgreSQL instance and can brick a card mid-write. Telemetry from this board says
the PSU is marginal. Use a **5.1 V / 2.5 A** supply, and consider a small UPS —
the workload is a 24/7 logger and aeration supervisor.

### 0.3 Set the timezone

The board is on **Europe/London**. The project is **America/Lima (UTC-5)**.

Rotation, retention and the flush timer all key off local midnight. Left
unfixed, every archive file would be misfiled by five hours and every per-day
aggregate would be wrong. `install.sh` sets this; verify with:

```
timedatectl | grep 'Time zone'
```

### 0.4 Plug in Ethernet

`eth0` reports `carrier=0, operstate=down` — nothing is connected. Four ESP32 nodes
and the Pi currently share one 2.4 GHz AP. Wiring the Pi removes that contention,
cuts jitter, and makes the whole LAN more reliable. The RPi 3B+ does ~300 Mbps over
Ethernet (USB2-limited), which is orders of magnitude more than this workload needs.

---

## 1. Install

```bash
git clone <repo> /opt/aqua-station-src
cd /opt/aqua-station-src
sudo ./deploy/install.sh --check   # report only, change nothing
sudo ./deploy/install.sh           # do it
```

The script is idempotent. It performs, in order:

| Step | What |
|---|---|
| preflight | Verifies arch, RAM, rootfs, power quality, free space |
| packages | `postgresql-17`, `mosquitto`, `caddy`, plus Python venv and `chrony` |
| host hygiene | Timezone; journald pinned to RAM; rsyslog disabled |
| directories | `/data/telemetry/<role>`, `/var/lib/aqua-gateway`, `/etc/aqua` |
| postgres | Applies the 1 GB tuning, creates the database, runs migrations |
| mosquitto | LAN listener, per-node credentials, persistence |
| gateway | venv, editable install, copies units |
| caddy | Config plus the dashboard build, if present |

### Redeploying (`deploy.sh`)

`install.sh` is for first setup and dependency changes. To push new gateway code or a new
dashboard build after that, use the much lighter `deploy.sh` — it moves files and restarts the
gateway, and does **no** apt/pip/venv/database work:

```bash
cd /opt/aqua-station-src
sudo ./deploy/deploy.sh           # sync code + web, restart aqua-gateway
sudo ./deploy/deploy.sh --no-web  # gateway only
sudo ./deploy/deploy.sh --check   # report only, change nothing
```

The gateway is installed editable, so new `.py` is live the moment the service restarts —
only a `pyproject.toml` change needs `install.sh` again. The dashboard is built on the laptop
(the Pi has no Node) and rsynced in; `deploy.sh` copies the existing `web/out/`.

| services | Enables and starts everything, arms the timers |
| verify | Ports, health endpoint, disk, **and a 30 s SD write-rate baseline** |

**Write down the MQTT credentials it prints.** They are generated once and are not
stored anywhere else.

---

## 2. Post-install configuration

### 2.1 Supabase Storage credentials

```bash
sudo -e /etc/aqua/gateway.env
```

Set `AQUA_BACKUP__ENDPOINT_URL`, `AQUA_BACKUP__ACCESS_KEY_ID` and
`AQUA_BACKUP__SECRET_ACCESS_KEY`.

The endpoint is **S3-compatible**, not the PostgREST URL:

```
https://<project-ref>.supabase.co/storage/v1/s3
```

These are **Storage S3 access keys**, and they are not the project API keys. The
`anon` and `service_role` keys under *Settings → API* cannot sign an S3 request:
the endpoint rejects them with `403 Forbidden` and an **empty** error message
(`An error occurred () when calling the PutObject operation`), which reads like a
network fault and is not one. Generate the keys under *Storage → S3 Access Keys*.

Create the bucket named by `AQUA_BACKUP__BUCKET` (default `aqua-archive`) in the
dashboard first — the flush uploads into an existing bucket and never creates
one. Set `AQUA_BACKUP__REGION` to the region shown beside the keys; for a project
whose pooler host is `aws-0-us-west-2.pooler.supabase.com` that is `us-west-2`.

Two things that cost time when setting this up, both of which fail with a message
that points somewhere other than the cause:

* **The project's API keys are not interchangeable with its S3 keys.** The new-style
  `sb_publishable_…` and `sb_secret_…` keys are not accepted as a Bearer token by the
  Storage API either — creating a bucket with one returns `Invalid Compact JWS`. Only
  *Storage → S3 Access Keys* work for the flush, and only the dashboard (or SQL) can
  create the bucket until they exist.
* **The direct Postgres host is IPv6-only.** `db.<ref>.supabase.co` has no A record, so
  a host without an IPv6 route fails with `Network is unreachable` — for admin SQL use
  the IPv4 pooler instead, remembering that a pooled username carries the project ref:

  ```
  postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
  ```

  The project's region is also its IPv6 prefix: an address in `2600:1f18::/32` is
  `us-east-1`. `AQUA_DATABASE_URL` still points at loopback — the Pi's own Postgres is
  the operational database, and the project is used for the durable archive.

Verify the keys before trusting the timer, since the error above is unhelpful:

```bash
/opt/aqua-station/gateway/.venv/bin/python - <<'PY'
import boto3
from botocore.config import Config
s3 = boto3.client("s3",
    endpoint_url="https://<project-ref>.supabase.co/storage/v1/s3",
    region_name="us-west-2",
    aws_access_key_id="<access-key-id>",
    aws_secret_access_key="<secret>",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
print(s3.list_objects_v2(Bucket="aqua-archive", MaxKeys=1))
PY
```

Then restart and dry-run the flush:

```bash
sudo systemctl restart aqua-gateway
sudo systemctl start aqua-flush.service
journalctl -u aqua-flush -n 50 --no-pager
```

A successful flush logs `uploaded=1 ... verified=1`; a failed one exits non-zero
and records the reason in the manifest (`last_error`), which is what the runbook
reads.

### 2.2 Node credentials

`deploy/install.sh` creates one broker credential per node and, unlike earlier
versions, records them:

```
/etc/aqua/mqtt-node-credentials        # root:root, mode 0600
```

Mosquitto keeps only password hashes, so this file is the **only copy** — back it
up somewhere that is not the SD card. Each node needs its own credential compiled
into its firmware; it cannot fetch one later. The installer verifies that the
broker password file really contains all five users before it finishes, because a
user silently missing there is otherwise discovered as
`[code:135] Not authorized` on a node, which points at the wrong end of the link.

To reissue the gateway's own credential (the only one also stored on the Pi):

```bash
NEW=$(head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | head -c 24)
sudo mosquitto_passwd -b /etc/mosquitto/passwd gateway "$NEW"
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd   # see below — mosquitto_passwd as root does not
sudo sed -i "s|^AQUA_MQTT__PASSWORD=.*|AQUA_MQTT__PASSWORD=$NEW|" /etc/aqua/gateway.env
sudo sed -i "s|^gateway=.*|gateway=$NEW|" /etc/aqua/mqtt-node-credentials
sudo chmod 0600 /etc/aqua/mqtt-node-credentials   # sed -i writes a new file; restate the mode
sudo systemctl restart aqua-gateway
```

Use `mosquitto_passwd -b`, **never** `-b -c`: `-c` overwrites the entire file and
deletes every other node's credential.

`mosquitto_passwd` run by root leaves the file owned by `root`, and the broker then
warns on every start that a future version will refuse to load it:

```
Warning: File /etc/mosquitto/passwd owner is not mosquitto. Future versions will
refuse to load this file. To fix this, use `chown mosquitto /etc/mosquitto/passwd`.
```

The installer gets this right; rotating a credential by hand is what silently reverts
it, so the `chown` above is part of the recipe, not optional tidying.

### 2.3 Dashboard

The dashboard is a **static export**. Build it on your laptop, not on the Pi — a
Next.js build on 905 MB will OOM.

```bash
cd web && npm ci && npm run build   # produces web/out/
```

Then either copy `web/out/` to `/srv/aqua-web/` on the Pi, or re-run
`./deploy/install.sh`, which rsyncs it if present. **No Node runtime is needed on
the Pi** — Caddy serves the files, the browser does all rendering.

### 2.4 Time source

The Pi has **no hardware RTC** (`RTC time: n/a`). On power loss it has no idea of
the time until NTP answers.

Recommended: fit a **DS3231 module** (~$3) — the same part the OD sensor already
uses. Failing that, the sensor's battery-backed RTC serves as the deployment's
reference over ESP-NOW. See `docs/TIME-SYNC.md`.

---

## 3. Verification

```bash
# Services and timers
systemctl status aqua-gateway mosquitto caddy postgresql --no-pager
systemctl list-timers 'aqua-*' --no-pager

# Health — flush_backlog_s is the number that matters
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool

# Memory: expect ~360 MB total across the stack
systemctl status aqua-gateway --no-pager | grep Memory
ps -o rss= -C postgres | awk '{s+=$1} END {printf "postgres: %.0f MB\n", s/1024}'
free -m

# The SD wear check: target under 10 MB/day idle
a=$(awk '{print $7}' /sys/block/mmcblk0/stat); sleep 60
b=$(awk '{print $7}' /sys/block/mmcblk0/stat)
echo "$(( (b-a) * 512 )) bytes written in 60s"
```

Expected budget: PostgreSQL ~65 MB, gateway ~80 MB, Mosquitto ~10 MB, Caddy ~20 MB,
OS ~185 MB → **~360 MB of 905 MB**.

**Idle write rate is the regression to watch.** Before the journald fix it was
~330 MB/day. After, it should be single-digit MB/day. If it climbs back, something
re-enabled file logging.

---

## 4. Firmware

Nodes are reflashed **one at a time**, with the old HTTP endpoints still available
as a fallback. Order and rationale: `firmware-patches/00-README.md`.

Exercise the gateway before touching hardware:

```bash
python tools/mqtt-simulator.py --role aerator --rate 5 --experiment EXP-DEMO-01
```

### 4.1 Identify the board first

**Two node roles build for ESP32-S3** — `nodo-aerador` (ODrive S1 + M8325s) and
`nodo-mezclador` (T-200 + SNR8503). They share no telemetry fields and no command verbs, so
which build to flash is not a detail. On the bench: the aerator has an ODrive S1 and an
INA226 current shunt; the mixer is a bare ESP32-S3 with PWM + FG to an ESC and a knob.
Full comparison table: `docs/TEST-PLAN.md` §0.1.

### 4.2 Virtual build first, then real

The `-virtual` environment runs the **real control logic** against a **simulated
peripheral**, so it proves the MQTT and telemetry path with no motor under load. Flash it
first; a failure there is the stack, not the mechanism.

```bash
export ROLE=aerator                                  # or: mixer
export PW=$(grep "^$ROLE=" /etc/aqua/mqtt-node-credentials | cut -d= -f2)
# AQUA_MQTT_HOST defaults to aqua-station.local (mDNS resolved dynamically).
# Inyect -DAQUA_MQTT_HOST only if overriding the target broker IP explicitly.
export PLATFORMIO_BUILD_FLAGS="-DAQUA_MQTT_PORT=1883 -DAQUA_MQTT_USER=\"$ROLE\" -DAQUA_MQTT_PASSWORD=\"$PW\""

cd /root/aqua/nodo-$ROLE
/root/.platformio/penv/bin/pio run -e esp32-s3-virtual -t upload   # simulated peripheral
/root/.platformio/penv/bin/pio run -e esp32-s3 -t upload           # real hardware
```

Quote the build flags exactly like that — single quotes outside, `\"` inside.
`shlex.split` strips quotes; the wrong form fails with `too many decimal points in number`
*and* leaks the credential into the compiler error. Redirect the build to a file and grep
for `SUCCESS|FAILED|Hash of data verified` rather than printing it.

**Never run a real node and a virtual node with the same `DEVICE_KEY` at the same time.**
The backend cannot distinguish them, so presence and telemetry would interleave.

### 4.3 Then run the test plan

`docs/TEST-PLAN.md` is the procedure that proves the node actually works: presence, Last
Will, real commands and their acks, the orchestrator state machine, persistence, and the
console's full control surface. Stage B is this box with `tools/dev-stack.py`; stage C is
the Pi.

---

## 5. Cutover

1. Run both systems in parallel for a few days; compare counters.
2. Reflash `nodo-aerador` last — it is the node whose 1 Hz poll caused the CPU
   overrun, so it is also the one with the most to gain.
3. Soak 72 h at the real experiment cadence (3 h every other day).
4. Confirm: no gaps in `seq`, `flush_backlog_s` stays low, idle writes stay low,
   temperature under 70 °C, no new throttle bits.
5. Decommission Vercel. Remove the Supabase project's database role — keep Storage,
   which is now the archive.

---

## 6. Known constraints

| Constraint | Note |
|---|---|
| SD-only storage | Two prior risks: power quality and a generic white-label card. Both are mitigated by the nightly verified flush, not eliminated. |
| Generic `SD32G` card | Endurance unverified. The archive is not the only copy once flushed. |
| Dev AP is a Windows hotspot | `192.168.137.x` is the ICS subnet. Deployment uses a real router. Reserve a static IP either way. |
| Rootfs `/` is SD-backed | Deliberate: zram handles swap, `/tmp` is tmpfs, logs are in RAM, and the DB syncs asynchronously. |
| No TimescaleDB | Not needed at these volumes; addable in place later if that changes. |
