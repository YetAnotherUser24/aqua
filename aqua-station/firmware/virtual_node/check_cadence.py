#!/usr/bin/env python3
"""
Assert the virtual node's publishing cadence matches what the station declares.

Three bugs of the same shape shipped in this simulator, and none of them were visible by
reading it:

  1. Telemetry was stamped by re-reading the wall clock at the moment the payload was
     assembled, instead of from the simulated device clock. Every stamp therefore advanced
     by the *tick period*, not by the sampling interval, so consecutive od_sensor stamps
     differed by ~4 ms and the gateway -- correctly measuring the interval between the
     node's own stamps -- reported 250 Hz for a 0.2 Hz sensor.
  2. `sensorIntervalS` defaulted to 1.0 while the real node samples every 5 s
     (`nodo-sensor-od/src/main.cpp`, `experimentIntervalMs = 5000`) and `roles.yaml`
     declares 0.2 Hz for the role.
  3. The mixer and pump were published on every tick at 5 Hz, though both firmwares push
     one envelope per 3 s running / 20 s idle and `roles.yaml` declares 0.3333 / 0.05.

The gateway was right in all three cases. The lesson is that "the dashboard shows the wrong
rate" can mean the signal is wrong, so this checks the signal -- the stamps actually on the
wire -- against `roles.yaml`, rather than checking that some number rendered.

Usage:  ./virtual_node --hz 5 | python3 check_cadence.py            # read from stdin
        python3 check_cadence.py --role od_sensor                   # subscribe itself
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ROLES_YAML = (
    REPO
    / "aqua-station"
    / "gateway"
    / "aqua_gateway"
    / "schema"
    / "roles.yaml"
)
if not ROLES_YAML.exists():  # running from inside aqua-station/
    ROLES_YAML = REPO / "gateway" / "aqua_gateway" / "schema" / "roles.yaml"

#: Tolerance on a measured rate. A batch boundary can land either side of the window edge,
#: so an exact match is not the right bar -- but anything off by more than this is a real
#: discrepancy, not sampling noise. The originals were out by 100x and 15x.
TOLERANCE = 0.10


def declared(role: str) -> dict[str, float]:
    roles = yaml.safe_load(ROLES_YAML.read_text())["roles"]
    return roles[role]["expected_rate_hz"]


def envelopes(lines) -> list[dict]:
    """
    Parse the three shapes this can be fed.

    ``mosquitto_sub -v`` emits ``<topic> <payload>`` with a *space*, the simulator's own
    ``TEL<TAB><topic><TAB><payload>`` uses tabs, and a bare payload has neither. Splitting on
    whichever separator is present matters: treating a ``topic {json}`` line as a bare payload
    makes ``json.loads`` fail on the topic name, and every envelope is then dropped in silence
    -- which surfaced as every role reporting SKIP, i.e. as if the node were quiet rather than
    as if the parser were wrong.
    """
    out = []
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith("TEL\t"):
            _, _, payload = line.split("\t", 2)
        elif "\t" in line:
            continue
        else:
            # ``-v`` output: topic and payload separated by a single space. The payload is JSON
            # and always starts with '{', so split on the first one rather than on the space --
            # that way a topic containing a space cannot truncate the payload.
            brace = line.find("{")
            payload = line[brace:] if brace > 0 else line
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return out


def measure(envs: list[dict]) -> tuple[float, float]:
    """
    Return ``(sample_rate_hz, stamp_interval_ms)`` from a role's envelopes.

    The rate is samples per second, not envelopes per second: the aerator batches 5 samples
    into each 5 s push and a batch is a sampling strategy, not a slower sample rate. Getting
    this wrong makes a correct 5 Hz node look like a 1 Hz one -- which is the mistake that
    made an earlier reading of this very simulator say 1 Hz where the truth was 25 samples
    under one timestamp.

    The interval is the median gap between consecutive *sample* stamps, so a single late
    envelope does not move it. Note that ``n`` stamps cover ``n-1`` intervals -- dividing by
    ``n`` overstates the rate by ``n/(n-1)``, which is 50% on a three-sample capture.
    """
    stamps: list[int] = []
    for env in envs:
        samples = env.get("samples") or []
        inner = [
            s.get("t_dev_ms")
            for s in samples
            if isinstance(s, dict) and isinstance(s.get("t_dev_ms"), int)
        ]
        if inner:
            stamps.extend(inner)
        elif isinstance(env.get("t_dev_ms"), int):
            stamps.append(env["t_dev_ms"])

    if len(stamps) < 2:
        raise SystemExit(f"need at least 2 sample stamps, got {len(stamps)}")

    stamps.sort()
    gaps = sorted(stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1))
    median_gap = gaps[len(gaps) // 2]
    if median_gap <= 0:
        raise SystemExit(
            f"consecutive sample stamps are identical (gap {median_gap} ms).\n"
            "  This is the signature of stamping from the wall clock per tick rather than\n"
            "  from the simulated device clock: every stamp advances by the loop period,\n"
            "  so a node sampling every 5 s reports ~250 Hz."
        )
    span_s = (stamps[-1] - stamps[0]) / 1000.0
    # n samples cover n-1 intervals, so the count must be n-1. Using n inflates a short capture
    # by n/(n-1): with only 3 samples that is 50%, which is exactly how 0.05 Hz read as 0.0743
    # and got reported as a FAIL against a simulator that was behaving correctly.
    return (len(stamps) - 1) / span_s, float(median_gap)


def per_envelope_budget_s(rate: float, count: int) -> float:
    """
    Timeout for capturing ``count`` envelopes of a role declaring ``rate`` samples per second.

    This cannot be derived from the declared rate alone, and pretending otherwise cost three
    rounds of timeouts. The sensor declares 0.2 Hz but sends *5 samples per envelope*, so it
    pushes once per 25 s -- five times slower than 1/rate would suggest -- while the aerator
    declares 5 Hz, batches 5 per second, and pushes once per 5 s. The batch size is the missing
    factor, it differs per role, and `roles.yaml` does not record it.

    So budget one sample interval per *sample*, not per envelope, and add a full push cycle of
    slack. This over-waits for a role whose batch size is large, which is the correct direction
    to be wrong in: an over-generous ceiling costs wall-clock time, while one that is too small
    reports a healthy node as broken.
    """
    if rate <= 0:
        return 20.0 + 60.0 * count
    sample_interval_s = 1.0 / rate
    # One interval per envelope, plus a whole extra interval to absorb a batch boundary, plus
    # a fixed floor for broker connect and process startup.
    return 20.0 + (count + 1) * sample_interval_s * 5.0


def subscribe(
    role: str,
    host: str,
    port: int,
    user: str,
    password: str,
    count: int,
    timeout_s: float,
) -> list[str]:
    """Capture ``count`` envelopes of one role's telemetry from the broker.

    The timeout is passed in rather than computed here, because it depends on the role's
    declared rate and the batch size, which this function does not know. A ceiling that suits
    a 5 Hz aerator is five times too small for a 0.2 Hz sensor.
    """
    cmd = [
        "mosquitto_sub",
        "-h",
        host,
        "-p",
        str(port),
        "-u",
        user,
        "-P",
        password,
        "-t",
        f"aqua/tlm/{role}",
        "-C",
        str(count),
        "-v",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_s
    )
    if proc.returncode != 0:
        raise SystemExit(f"mosquitto_sub failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument(
        "--role",
        default=None,
        help="check one role by subscribing (else read stdin)",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user", default="gateway")
    ap.add_argument("--password", default="gwhunter2")
    ap.add_argument(
        "--count",
        type=int,
        default=0,
        help=(
            "envelopes to capture (0 = derive from the declared rate: enough to span "
            "~15 s of samples, never fewer than 3)"
        ),
    )
    ap.add_argument(
        "--mode",
        choices=("experiment", "idle"),
        default="experiment",
        help="which declared rate to compare against",
    )
    args = ap.parse_args()

    if args.role:
        rate = declared(args.role)[args.mode] or 0.05
        count = args.count
        if count <= 0:
            # Enough envelopes to span several sample intervals at the declared rate. Three is
            # the floor: `measure` needs two sample stamps and a third keeps the stat stable.
            count = max(3, int(round(rate * 15.0)))
        lines = subscribe(
            args.role,
            args.host,
            args.port,
            args.user,
            args.password,
            count,
            per_envelope_budget_s(rate, count),
        )
        envs = envelopes(lines)
        roles = [(args.role, envs)]
    else:
        envs = envelopes(sys.stdin)
        by_role: dict[str, list[dict]] = {}
        for env in envs:
            by_role.setdefault(env.get("role", "?"), []).append(env)
        roles = sorted(by_role.items())

    if not roles:
        raise SystemExit("no envelopes to check")

    expected_units = 1.0 if args.mode == "experiment" else 1.0
    failures = 0
    skipped = 0
    print(
        f"  {'role':<10} {'declared':>9} {'measured':>10} {'stamp gap':>10}  result"
    )
    for role, envs in roles:
        if len(envs) < 2:
            # A skip is not a pass. Reporting "all rates within 10%" while every role was
            # skipped is how a check that never ran is mistaken for a check that succeeded.
            skipped += 1
            print(
                f"  {role:<10} {'-':>9} {'-':>10} {'-':>10}  SKIP (need 2+ envelopes)"
            )
            continue
        rate, gap = measure(envs)
        want = declared(role)[args.mode]
        rel = abs(rate - want) / want if want else float("inf")
        ok = rel <= TOLERANCE
        failures += 0 if ok else 1
        print(
            f"  {role:<10} {want:>9} {rate:>10.4f} {gap:>9.0f}ms  "
            f"{'ok' if ok else f'FAIL ({rel * 100:.0f}% off)'}"
        )

    print()
    if failures:
        print(f"{failures} role(s) disagree with roles.yaml[{args.mode}]")
        return 1
    if skipped and skipped == len(roles):
        print(
            f"nothing measured: all {skipped} role(s) skipped. "
            "Capture more envelopes with a larger --count."
        )
        return 1
    if skipped:
        print(f"{skipped} role(s) skipped, the rest within {TOLERANCE:.0%}")
        return 0
    print(f"all rates within {TOLERANCE:.0%} of roles.yaml[{args.mode}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
