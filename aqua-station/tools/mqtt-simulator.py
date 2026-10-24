#!/usr/bin/env python3
"""
MQTT simulator — exercises the gateway without touching hardware.

Replaces the previous ``scripts/simulate-esp32.js`` and ``scripts/virtual-bench/``.
Publishes schema-v2 envelopes for any role so the whole ingest path can be
verified before a single node is reflashed.

Usage:
    python tools/mqtt-simulator.py --role aerator --rate 5 --experiment EXP-DEMO-01
    python tools/mqtt-simulator.py --role od_sensor --rate 0.0167 --count 10
    python tools/mqtt-simulator.py --role pump --scenario dose
    python tools/mqtt-simulator.py --all --duration 60

Deliberately dependency-light: paho-mqtt only, so it runs anywhere.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    sys.exit("paho-mqtt is required:  pip install paho-mqtt")

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required:  pip install PyYAML")

SCHEMA_VERSION = 2
REGISTRY = (
    Path(__file__).resolve().parents[1]
    / "gateway"
    / "aqua_gateway"
    / "schema"
    / "roles.yaml"
)


@dataclass
class NodeClock:
    """
    Simulates a node's clock.

    Worth simulating deliberately: the gateway's clock-offset tracking and the
    export's alignment correction are only exercised when the simulated device clock
    actually drifts. A simulator with a perfect clock would hide the bugs it exists
    to find.
    """

    boot_id: int
    offset_ms: int
    drift_ppm: float
    _start: float = 0.0

    def __post_init__(self) -> None:
        self._start = time.time()

    def now_ms(self) -> int:
        elapsed_s = time.time() - self._start
        drift = elapsed_s * self.drift_ppm * 1e-6 * 1000.0
        return int((time.time() + self.offset_ms / 1000.0) * 1000 + drift)


# ---------------------------------------------------------------------------
# Per-role signal generators. Physically plausible shapes, because a constant
# value would not exercise the rollup aggregates or the threshold events.
# ---------------------------------------------------------------------------


def _od_sensor(t: float, phase: float) -> dict[str, Any]:
    do = 7.8 + 1.2 * math.sin(t / 900 + phase) + random.gauss(0, 0.02)
    temp = 23.0 + 1.5 * math.sin(t / 3600 + phase) + random.gauss(0, 0.01)
    return {
        "od_do_mg_l": round(do, 3),
        "od_do_raw": int(do * 1000),
        "od_sat_pct": round(do / 7.15 * 100, 2),
        "od_sat_raw": int(round((do / 7.15 * 100) * 10)),
        "od_temp_c": round(temp, 2),
        "od_temp_raw": int(temp * 100),
        "od_aux1_raw": 0,
        "od_aux2_raw": 0,
        "pwr_battery_v": round(4.15 + random.gauss(0, 0.01), 2),
        "rtc_temp_c": round(24.0 + random.gauss(0, 0.05), 2),
        "rtc_health": 0,
        "dev_status": 0,
    }


def _aerator(
    t: float, phase: float, *, running: bool = True
) -> dict[str, Any]:
    target = 420.0 if running else 0.0
    # First-order spin-up, matching the real machine's response.
    rpm = target * (1 - math.exp(-t / 2.0)) if running else 0.0
    rpm += random.gauss(0, 3.0)
    v = 24.1 + random.gauss(0, 0.05)
    i = (0.9 + 2.6 * (rpm / 600.0)) if running else 0.05
    return {
        "drv_rpm": round(max(0.0, rpm), 2),
        "drv_rpm_target": target,
        "drv_vbus_v": round(v, 2),
        "drv_ibus_a": round(i, 2),
        "drv_power_w": round(v * i, 2),
        "drv_torque_nm": round(0.28 + random.gauss(0, 0.01), 3),
        "drv_iq_a": round(i * 0.94, 2),
        "drv_running": running,
        "drv_status": 0,
        "ctl_mode": "pid",
        "ctl_failsafe": False,
        # Echoed from the sensor over ESP-NOW, for this node's own control loop.
        # Non-authoritative by contract — the export must never average it with od_*.
        "peer_do_mg_l": round(7.8 + 1.2 * math.sin(t / 900 + phase), 3),
        "peer_temp_c": round(23.0 + 1.5 * math.sin(t / 3600 + phase), 2),
    }


def _mixer(t: float, phase: float) -> dict[str, Any]:
    duty = 40.0 + 15.0 * math.sin(t / 120 + phase)
    rpm = max(0.0, (duty - 20.0) / 80.0 * 1000.0)
    return {
        "drv_rpm": round(rpm + random.gauss(0, 5), 1),
        "drv_rpm_target": round(rpm, 1),
        "drv_rad_s": round(rpm * 2 * math.pi / 60, 3),
        "drv_rad_s_target": round(rpm * 2 * math.pi / 60, 3),
        "drv_duty_pct": round(duty, 2),
        "drv_running": rpm > 1.0,
        "drv_stall": 0.0 < rpm < 50.0,
        "drv_status": 0,
    }


def _pump(t: float, phase: float, *, dosing: bool) -> dict[str, Any]:
    dosed = min(50.0, t * 8.0) if dosing else 0.0
    return {
        "pump_dosed_ml": round(dosed, 2),
        "pump_target_ml": 50.0 if dosing else 0.0,
        "pump_rpm": round(120.0 + random.gauss(0, 2), 2) if dosing else 0.0,
        "pump_mode": "dose" if dosing else "idle",
        "pump_dose_completed": dosing and dosed >= 50.0,
        "pump_dose_timed_out": False,
        "drv_status": 0,
    }


GENERATORS = {
    "od_sensor": _od_sensor,
    "aerator": _aerator,
    "mixer": _mixer,
    "pump": _pump,
}


def sample_times(base_ms: int, count: int, rate_hz: float) -> list[int]:
    """
    Per-sample device timestamps for one envelope, oldest first.

    Firmware stamps every sample with its own ``t_dev_ms`` (``AQUA_SAMPLE_TIME_FIELD``),
    because a batch spans time. Handing 25 genuinely different measurements the single
    envelope timestamp files them all under one instant, so the archive shows a burst of
    rows at one moment rather than the rate they were sampled at -- and comparing that
    against real firmware data reads as a mismatch that is purely the simulator's.

    Stepped backwards from the envelope time, so the batch covers the interval that just
    elapsed and no sample is stamped in the future.
    """
    step_ms = 1000.0 / max(rate_hz, 1e-6)
    return [base_ms - round((count - 1 - k) * step_ms) for k in range(count)]


def build_envelope(
    role: str,
    node_id: str,
    clock: NodeClock,
    seq: int,
    samples: list[dict[str, Any]],
    experiment: str | None,
    t_src: str,
    rssi: int | None,
    t_dev_ms: int | None = None,
) -> dict[str, Any]:
    """Construct a schema-v2 envelope exactly as firmware should."""
    envelope: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "role": role,
        "node_id": node_id,
        "exp": experiment,
        "seq": seq,
        "t_dev_ms": clock.now_ms() if t_dev_ms is None else t_dev_ms,
        "t_src": t_src,
        "boot_id": clock.boot_id,
        "samples": samples,
    }
    if rssi is not None:
        envelope["rssi"] = rssi
    return envelope


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish schema-v2 telemetry to the AquaControl broker"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default="gateway")
    parser.add_argument("--password", default=None)
    parser.add_argument(
        "--role", choices=sorted(GENERATORS), help="role to simulate"
    )
    parser.add_argument(
        "--all", action="store_true", help="simulate every role at once"
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=5.0,
        help="samples per second (5 = experiment, 0.0167 = idle)",
    )
    parser.add_argument(
        "--samples-per-message",
        type=int,
        default=25,
        help="batch size (firmware uses 25 at 5 Hz over 5 s)",
    )
    parser.add_argument(
        "--duration", type=float, default=0, help="seconds to run; 0 = forever"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="stop after N messages; 0 = unlimited",
    )
    parser.add_argument(
        "--experiment", default=None, help="experiment id, or omit for idle"
    )
    parser.add_argument(
        "--scenario", choices=["normal", "dose", "startup"], default="normal"
    )
    parser.add_argument(
        "--t-src", default="ntp", choices=["ntp", "espnow", "rtc", "monotonic"]
    )
    parser.add_argument(
        "--clock-offset-ms",
        type=int,
        default=0,
        help="simulate a skewed device clock",
    )
    parser.add_argument(
        "--drift-ppm", type=float, default=0.0, help="simulate clock drift"
    )
    parser.add_argument(
        "--drop-every",
        type=int,
        default=0,
        help="skip a seq value every N messages (tests gap detection)",
    )
    parser.add_argument("--rssi", type=int, default=-61)
    parser.add_argument(
        "--namespace",
        default=None,
        help="override the node_id (tests unknown-node handling)",
    )
    args = parser.parse_args()

    if not args.all and not args.role:
        parser.error("specify --role or --all")

    if not REGISTRY.exists():
        print(f"registry not found at {REGISTRY}", file=sys.stderr)
        return 2
    document = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    roles = sorted(document["roles"]) if args.all else [args.role]

    client = mqtt.Client(client_id=f"aqua-sim-{uuid.uuid4().hex[:8]}")
    if args.password:
        client.username_pw_set(args.username, args.password)
    try:
        client.connect(args.host, args.port, keepalive=60)
    except OSError as exc:
        print(
            f"cannot reach broker at {args.host}:{args.port}: {exc}",
            file=sys.stderr,
        )
        return 2
    client.loop_start()

    # Each role gets its own simulated clock with a different offset, so the
    # gateway's per-node offset tracking is genuinely exercised.
    clocks: dict[str, NodeClock] = {}
    node_ids: dict[str, str] = {}
    seqs: dict[str, int] = {}
    for index, role in enumerate(roles):
        clocks[role] = NodeClock(
            boot_id=1,
            offset_ms=args.clock_offset_ms + index * 120,
            drift_ppm=args.drift_ppm or (index * 3.0),
        )
        node_ids[role] = (
            args.namespace + f"-{role}"
            if args.namespace
            else document["roles"][role]["node_id"]
        )
        seqs[role] = 0

    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    interval = args.samples_per_message / max(args.rate, 1e-6)
    started = time.time()
    messages = 0
    print(
        f"simulating {', '.join(roles)} at {args.rate} Hz "
        f"({args.samples_per_message}/msg every {interval:.1f}s) -> {args.host}:{args.port}"
    )
    if args.experiment:
        print(f"experiment: {args.experiment}")
    print("Ctrl-C to stop\n")

    try:
        while running:
            elapsed = time.time() - started
            if args.duration and elapsed > args.duration:
                break
            if args.count and messages >= args.count:
                break

            for role in roles:
                # Simulated message loss: skip a seq so gap detection has something to find.
                if args.drop_every and (messages + 1) % args.drop_every == 0:
                    seqs[role] += 1

                generator = GENERATORS[role]
                # One base time for this message, shared by the envelope and its samples so
                # the batch is anchored to the same instant firmware would anchor it to.
                base_ms = clocks[role].now_ms()
                times = sample_times(
                    base_ms, args.samples_per_message, args.rate
                )
                samples = []
                for k in range(args.samples_per_message):
                    t = elapsed + k / max(args.rate, 1e-6)
                    if role == "aerator":
                        running_now = not (
                            args.scenario == "startup" and t < 5.0
                        )
                        sample = generator(t, 0.0, running=running_now)
                    elif role == "pump":
                        sample = generator(
                            t, 0.0, dosing=(args.scenario == "dose")
                        )
                    else:
                        sample = generator(t, 0.0)
                    # Reserved format key, not a measurement: the gateway keeps it out of
                    # the metric set and uses it for the archive's first column.
                    sample["t_dev_ms"] = times[k]
                    samples.append(sample)

                seqs[role] += 1
                envelope = build_envelope(
                    role,
                    node_ids[role],
                    clocks[role],
                    seqs[role],
                    samples,
                    args.experiment,
                    args.t_src,
                    args.rssi,
                    t_dev_ms=base_ms,
                )
                client.publish(
                    f"aqua/tlm/{role}",
                    json.dumps(envelope, separators=(",", ":")),
                    qos=1,
                )

            messages += 1
            if messages % 10 == 0:
                print(
                    f"  {messages} messages, {messages * args.samples_per_message * len(roles)} samples"
                )
            time.sleep(interval)
    finally:
        client.loop_stop()
        client.disconnect()

    print(
        f"\ndone: {messages} messages, ~{messages * args.samples_per_message * len(roles)} samples"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
