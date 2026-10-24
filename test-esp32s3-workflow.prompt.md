# Run the end-to-end test plan against the connected ESP32-S3

Execute `aqua-station/docs/TEST-PLAN.md` end to end. Work through the stages in order and
report a pass/fail table. **Do not skip straight to the Pi.**

## Context you need before starting

- **Two roles build for ESP32-S3** — `nodo-aerador` (ODrive S1 + M8325s) and
  `nodo-mezclador` (T-200 + SNR8503). They share no telemetry fields and no command verbs.
  Establish which board is connected **before** anything else, and say so explicitly in the
  report. On the bench: the aerator has an ODrive S1 and an INA226; the mixer is a bare
  ESP32-S3 with PWM+FG to an ESC and a potentiometer.
- Only the **aerator** has been confirmed as real hardware before now
  (`PENDING-TASKS.md` §6). If the board is the mixer, this is the first real mixer run —
  flag that.
- A `-virtual` build runs the real control logic against a simulated peripheral
  (`VIRTUAL_NODES.md`). Use it **first**: it validates the MQTT/telemetry chain with no
  motor under load. Never run a real and a virtual node on the same `DEVICE_KEY` at once.

## Stage A — console only, mock gateway

```bash
pkill -f mock-gateway.py
cd /root/aqua/aqua-station
PYTHONPATH=$PWD/gateway nohup gateway/.venv/bin/python tools/mock-gateway.py \
  --root $PWD/web/out --port 3111 > /tmp/mock-gw.log 2>&1 &
```

Exercises **every** control in the console with no hardware involved, including the full
A2 table (37 rows), the E-STOP per-role verb fan-out (A3), and the Hito round-trip (A4).

**Harness quirk:** `/api/stream` never closes, so a headless browser waiting on `load`
hangs. Load → `reload()` → wait → screenshot. This is the SSE design, not a bug.
The 200 ms RTC clock also keeps the page "unstable" for Playwright — click header buttons
with `{ force: true, noWaitAfter: true }`.

## Stage B — real gateway + the real node

```bash
gateway/.venv/bin/python tools/dev-stack.py
```

Then B1–B7: presence, LWT, real commands + acks, orchestrator state machine, persistence,
and gap/clock events. Keys to verify:

- `mosquitto_sub -t 'aqua/presence/#' -v` — exactly one node, `online:true`
- `mosquitto_sub -t "aqua/tlm/$ROLE" -v` — rate matches `roles.yaml` `expected_rate_hz`
- `mosquitto_sub -t 'aqua/cmd/#' -v` **while clicking** — one command per button
- `mosquitto_sub -t 'aqua/ack/#' -v` — every ack is `success: true`; an unknown verb must
  report `success: false` / `unsupported_action`, never a silent success

An ack of `success: false` means the **verb** is wrong for that node, not that the network
failed. Check it against the firmware's own `action == "..."` branches before blaming the UI.

## Stage C — the Pi

Only if A and B are clean. `docs/DEPLOYMENT.md` §4–5: one node at a time, `nodo-aerador`
**last**. Soak 72 h and confirm the three numbers from `docs/RUNBOOK.md`.

## Report back

1. **Which board** — role, board id, real or virtual build, backend stage reached.
2. **A pass/fail table** — every row of A2, plus A3 and A4.
3. **Failures with the layer named** — use the §4 triage table, not a guess.
4. **The regression gate:**

   ```bash
   cd /root/aqua/aqua-station/gateway && .venv/bin/python -m pytest tests/ -q
   cd /root/aqua/aqua-station/web && npm run typecheck && npm run build
   ```

5. **Documentation drift found** — anything in `docs/` that no longer matches behaviour.
   `docs/TEST-PLAN.md` §5 and the READMEs are the first places to check.

## Rules

- **Do not "fix" a failing test by relaxing the expectation.** Every expected result in the
  plan comes from the protocol contract (`docs/PROTOCOL.md`), `roles.yaml`, or the firmware's
  own command handlers — not from what the code happens to do.
- **A node that acknowledges a verb it never performed is the worst outcome.** Treat a
  `success: true` on an unhandled action as a failure.
- Nothing that sends a real motor command may be left running unattended.
