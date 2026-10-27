# Runbook

Operational procedures for the AquaControl station. Read the first section before
the first real experiment.

---

## The three numbers that matter

Check these first, always. Everything else is secondary.

| Number | Where | Healthy | Means |
|---|---|---|---|
| `flush_backlog_s` | `/healthz` | < 90000 (1 day) | Age of the oldest **unflushed** archive file. With a 14-day window this is the only warning before data loss becomes permanent. |
| Idle SD writes | `diskstats`, see below | **< 10 MB/day** | Was ~330 MB/day before the journald fix. A regression here is the earliest sign of a config change re-enabling file logging. |
| `seq` gaps | `telemetry_gap_detected` events | none during an experiment | A gap means samples were lost, which is different from the Pi being down. |

```bash
# The SD wear check
a=$(awk '{print $7}' /sys/block/mmcblk0/stat); sleep 60
b=$(awk '{print $7}' /sys/block/mmcblk0/stat)
echo "$(( (b-a) * 512 )) bytes in 60s  (~$(( (b-a) * 512 * 1440 / 1024 / 1024 )) MB/day)"
```

---

## Flush failed

**Symptom:** `systemctl --failed` shows `aqua-flush.service`; a `flush_failed`
event is on the dashboard; `flush_backlog_s` is climbing.

**Nothing is lost yet.** Retention will not delete anything unflushed, so the local
window simply grows. **You have days, not minutes** — but the clock is running:
once the disk fills, ingestion stops.

```bash
journalctl -u aqua-flush -n 100 --no-pager
```

| Cause | Fix |
|---|---|
| Credentials wrong or expired | Correct `/etc/aqua/gateway.env`, then `systemctl start aqua-flush` or run `aqua-r2-sync` |
| R2 authentication failure | Verify R2 API token has *Object Read & Write* permissions in Cloudflare Dashboard — see `docs/DEPLOYMENT.md` §2.1 |
| No internet | Check campus Wi-Fi / portal cautivo (`aqua-captive-url`). |
| Bucket missing | Bucket `aqua-station-r2` must exist in Cloudflare R2. |
| Custom domain access blocked | Check `data.aqua-station.live` has `enabled: true` in R2 custom domains settings. |
| Verification failing repeatedly | `archive_verify_failed` events. **Do not** delete local files. Investigate: a mismatch means the upload is corrupt, not that the data is bad. |

**Check the backlog:**
```bash
sqlite3 /var/lib/aqua-gateway/manifest.db \
  "SELECT path, day, rows, datetime(closed_at,'unixepoch','localtime') AS closed,
          verify_attempts, last_error
   FROM files WHERE closed_at IS NOT NULL AND flushed_at IS NULL
   ORDER BY closed_at LIMIT 20;"
```

**Force a sync / flush** (safe to re-run; verification is idempotent):
```bash
aqua-r2-sync
sudo systemctl start aqua-flush.service
```

---

## Disk filling up

**Symptom:** `disk_usage_warning` then `disk_usage_critical`; `retention_blocked_unflushed`.

**Do not delete archive files by hand.** A file not in the manifest, or deleted
while unverified, is data loss that nothing can detect afterwards.

```bash
df -h /
sqlite3 /var/lib/aqua-gateway/manifest.db \
  "SELECT COUNT(*), SUM(bytes)/1024/1024 AS mb FROM files WHERE deleted_at IS NULL;"
```

Diagnose in this order:

1. **Is the flush working?** If not, fix that first — everything else is downstream.
2. **Is retention running?** `systemctl list-timers 'aqua-*'`, then
   `journalctl -u aqua-rotate -n 50`.
3. **Is `retention_blocked_unflushed` firing?** That means files are old enough but
   not archived. Correct behaviour; fix the flush.
4. **Is something else filling the disk?** `du -sh /var/log /var/lib/*`. If
   `/var/log` is large, journald or rsyslog was re-enabled.
5. **Only then**, lower `AQUA_RETENTION_DAYS` — and understand that you are
   deliberately shrinking the safety window.

---

## A node is offline

**Symptom:** `node_offline` event, dashboard shows the node greyed out.

This comes from **MQTT Last Will & Testament** — the broker reports the drop
immediately when the TCP connection dies. It is not a 60-second heartbeat guess.

```bash
mosquitto_sub -h 127.0.0.1 -t 'aqua/presence/#' -v     # retained state of all nodes
mosquitto_sub -h 127.0.0.1 -t 'aqua/tlm/#' -v          # live traffic
journalctl -u mosquitto -n 50 --no-pager
```

| Observation | Meaning |
|---|---|
| LWT fired | The node's TCP connection died — power, WiFi, or crash |
| Connected but no telemetry | LWT cannot see this. Check `telemetry_gap_detected` and the node's own SD logs. |
| Repeated `node_reconnected` | Marginal WiFi. Check `rssi` in the payload and the `link_quality_degraded` events. |

**Important:** the control loop is peer-to-peer over ESP-NOW and does not depend on
this stack. A node going offline from the broker does **not** stop aeration control.
It only stops the monitoring record.

---

## Clock problems

**Symptom:** `clock_offset_exceeded` or `clock_uncertainty_high` events; aligned
data looks shifted.

The Pi has **no hardware RTC**. On power loss it loses the time until NTP answers.

```bash
timedatectl                              # host clock
journalctl -u chrony -n 30 --no-pager    # NTP sync state
```

Mitigations, in order of preference:
1. **Fit a DS3231 to the Pi** (~$3, same part the OD sensor uses).
2. Confirm the sensor is broadcasting `aqua/time` and ESP-NOW `TIME` packets.
3. Confirm nodes resync at experiment start.

**Raw archive files are never rewritten.** Offsets are recorded per
`(node_id, boot_id)` and applied at **export** time, so the raw data stays an honest
record and a correction can be audited or redone.

---

## PostgreSQL issues

```bash
sudo -u postgres psql -d aqua -c "SHOW shared_buffers;"       # expect 48MB
sudo -u postgres psql -d aqua -c "SELECT pg_database_size('aqua')/1024/1024 AS mb;"
systemctl status postgresql --no-pager | grep Memory
```

**Memory looks like ~200 MB instead of ~65 MB:** the tuning overlay is not being
read. Confirm `/etc/postgresql/17/main/conf.d/10-aqua.conf` exists and
`include_dir 'conf.d'` is active in `postgresql.conf`.

**Out of connections:** `max_connections` is 12 by design. Only the gateway, the
flush job and an occasional `psql` session should connect. A leak in the gateway's
pool would show as connections climbing — `SELECT count(*) FROM pg_stat_activity;`.

---

## Gateway won't start

```bash
systemctl status aqua-gateway --no-pager
journalctl -u aqua-gateway -n 100 --no-pager
```

| Message | Cause |
|---|---|
| `missing directory …` | `deploy/install.sh` was not run, or `/data/telemetry` is missing |
| `not writable: …` | Ownership wrong; `chown -R aqua:aqua /data/telemetry /var/lib/aqua-gateway` |
| `manifest inconsistency` | Bookkeeping disagrees with reality. It logs and continues — pruning is paused, ingestion is not. See below. |
| `backup enabled but storage credentials are missing` | Fill in `/etc/aqua/gateway.env`, or set `AQUA_BACKUP__ENABLED=false` |

**Manifest inconsistency** means rows claim to be archived without the proof
(`sha256`, `archive_key`) to back it. **Never "fix" this by marking them verified.**
Query the affected rows, re-verify against the remote object, and only then correct
the manifest:

```bash
sqlite3 /var/lib/aqua-gateway/manifest.db \
  "SELECT path, flushed_at, verified, sha256, archive_key FROM files
   WHERE (flushed_at IS NOT NULL AND (sha256 IS NULL OR archive_key IS NULL))
      OR (verified = 1 AND flushed_at IS NULL);"
```

---

## Restoring from the archive

The Pi is a rolling buffer; **Cloudflare R2 is the durable edge archive**.

```
raw/<role>/YYYY-MM-DD.csv.gz
telemetry/<role>/YYYY-MM-DD.csv
exports/<exp_id>/bundle.tar.gz
nodo-bomba/
```

```bash
# Direct HTTP download from edge portal (no AWS credentials required)
curl -O https://data.aqua-station.live/exports/EXP-2026-10-26-03/bundle.tar.gz
tar xzf bundle.tar.gz

# Rebuild a local window after an SD card replacement via S3 API
aws s3 sync s3://aqua-station-r2/raw/ /data/telemetry/ \
  --endpoint-url https://2466a556724af7cbd00d3c3197a35de2.r2.cloudflarestorage.com
```

A bundle contains the **combined, time-aligned wide dataset** plus `events.csv` and
`manifest.json`. The manifest records the clock offset applied per node and the
`ML_PER_REV` in force — without those, a dose cannot be reconstructed.

**Rebuild a bundle** at any time from the raw archive:
```bash
python tools/export-experiment.py --experiment EXP-2026-10-09-01 --out ./bundles
```

---

## Scheduled jobs

| Timer | When (America/Lima) | Does |
|---|---|---|
| `aqua-rotate.timer` | 00:05 | Close, checksum, gzip yesterday's files |
| `aqua-rotate.timer` | 00:35 | Apply retention, now that the manifest is up to date |
| `aqua-flush.timer` | 03:00 | Upload unflushed files, verify, mark flushed |

Two rotation passes, deliberately: retention must never race the rotation that
makes a file eligible. All three use `Persistent=true`, so a missed run (Pi was off)
happens on the next boot rather than being skipped.

```bash
systemctl list-timers 'aqua-*' --no-pager
```

---

## Escalation order

When something is wrong and you are short on time:

1. **Is the flush working?** Everything downstream is safe if it is.
2. **Is the disk filling?** Check `flush_backlog_s` and `df -h /`.
3. **Is the control loop affected?** It is not — it is peer-to-peer on the node. Say
   this out loud before panicking about the server.
4. Everything else can wait until after the experiment.
