#!/usr/bin/env python3
"""
Cross-language conformance check: firmware output -> the real gateway parser.

`test_protocol.cpp` proves the C++ side behaves; this proves the **gateway accepts what the
firmware produces**. Those are different claims, and only the second one matters at 03:00 when
a node is publishing into the void.

It runs the compiled host test, reads the envelopes it emitted, and puts each one through:

  * ``aqua_gateway.mqtt.envelope.parse_envelope`` — the exact function that will receive it in
    production, with the topic-derived role, so a topic/payload mismatch is caught;
  * the schema registry, to confirm every field name the firmware sends is declared in
    ``roles.yaml`` for that role.

An unknown field is not fatal to the gateway — it lands in the unknown-field side channel
rather than being dropped — but it *does* mean the firmware and the registry have drifted, and
the export would silently omit the column. So it fails here.

Usage:
    python3 firmware/aqua_protocol/test/verify_against_gateway.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTOCOL_DIR = HERE.parent
REPO_ROOT = PROTOCOL_DIR.parent.parent
GATEWAY = REPO_ROOT / "gateway"

sys.path.insert(0, str(GATEWAY))
try:
    from aqua_gateway.mqtt.envelope import EnvelopeError, parse_envelope
    from aqua_gateway.mqtt.topics import role_from_telemetry_topic, telemetry
    from aqua_gateway.schema.registry import load_registry
except ImportError as exc:  # pragma: no cover - developer feedback
    sys.exit(
        f"cannot import the gateway from {GATEWAY}: {exc}\n"
        "Install its dependencies first, e.g. pip install -e gateway"
    )

#: label -> role implied by the topic the envelope would be published on.
EXPECTED_ROLE = {
    "od_sensor_rtc": "od_sensor",
    "od_sensor_monotonic_no_rssi": "od_sensor",
    "od_sensor_batch8": "od_sensor",
    "aerator_running": "aerator",
    "aerator_stopped": "aerator",
    "aerator_with_experiment": "aerator",
    "mixer_running": "mixer",
    "pump_idle": "pump",
    "pump_dosing": "pump",
}


def build_binary(dest: Path) -> None:
    sources = [HERE / "test_protocol.cpp", PROTOCOL_DIR / "aqua_protocol.cpp"]
    cmd = [
        "g++",
        "-std=c++17",
        "-O1",
        "-Wall",
        "-Wextra",
        f"-I{PROTOCOL_DIR}",
        *(str(s) for s in sources),
        "-o",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("compile failed:\n", result.stdout, result.stderr)
        sys.exit(1)
    # -Wall -Wextra output is a finding, not noise: this code runs on a node nobody can attach
    # a debugger to.
    for stream in (result.stdout, result.stderr):
        for line in stream.splitlines():
            if "warning:" in line:
                print(f"  compiler warning: {line.strip()}")


def main() -> int:
    if not (GATEWAY / "aqua_gateway").is_dir():
        sys.exit(f"gateway package not found at {GATEWAY}/aqua_gateway")

    registry = load_registry()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        binary = tmpdir / "test_protocol"
        envelopes = tmpdir / "envelopes.jsonl"

        print("=== compile ===")
        build_binary(binary)
        print(f"  ok -> {binary.name}")

        print("\n=== run C++ host test ===")
        result = subprocess.run(
            [str(binary), str(envelopes)], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if line.strip():
                print(f"  {line}")
        if result.returncode != 0:
            print(f"  C++ test FAILED (exit {result.returncode})")
            if result.stderr.strip():
                print(result.stderr)
            return 1

        print("\n=== feed every envelope to the real gateway parser ===")
        if not envelopes.exists():
            sys.exit("  no envelopes emitted")

        lines = [ln for ln in envelopes.read_text().splitlines() if ln.strip()]
        failures: list[str] = []
        seen_labels: set[str] = set()

        for index, line in enumerate(lines, start=1):
            label, _, payload = line.partition("\t")
            seen_labels.add(label)
            role = EXPECTED_ROLE.get(label)
            topic_role = (
                role_from_telemetry_topic(telemetry(role)) if role else None
            )

            if role is None:
                failures.append(
                    f"{label}: unknown label, cannot derive a topic"
                )
                continue
            if topic_role != role:
                failures.append(
                    f"{label}: topic builder yielded {topic_role!r}, expected {role!r}"
                )
                continue

            try:
                raw = json.loads(payload)
            except json.JSONDecodeError as exc:
                failures.append(
                    f"{label}: emitted payload is not valid JSON ({exc})"
                )
                continue

            try:
                envelope = parse_envelope(raw, expect_role=topic_role)
            except EnvelopeError as exc:
                failures.append(
                    f"{label}: gateway rejected it — {exc.code}: {exc.detail}"
                )
                continue

            # Identity must match the registry, or the node is impersonating another one.
            schema = registry.for_role(role)
            if envelope.node_id != schema.node_id:
                failures.append(
                    f"{label}: node_id {envelope.node_id!r} != registry {schema.node_id!r}"
                )

            # Every field must be declared, or the export silently omits a column.
            known = schema.field_names
            for sample_index, sample in enumerate(envelope.samples):
                unknown = sorted(set(sample) - known)
                if unknown:
                    failures.append(
                        f"{label}: samples[{sample_index}] has fields not in roles.yaml: {unknown}"
                    )
                missing = sorted(known - set(sample))
                if missing:
                    # Not fatal — a node may legitimately omit a metric — but on a
                    # hand-built fixture it means the sample table is incomplete.
                    print(
                        f"  note: {label} samples[{sample_index}] omits {missing}"
                    )

            print(
                f"  {label:30s} v{envelope.v} {envelope.role:9s} "
                f"seq={envelope.seq:<10d} t_src={envelope.t_src.value:9s} "
                f"samples={len(envelope.samples):<3d} rssi={envelope.rssi}"
            )

        unemitted = sorted(set(EXPECTED_ROLE) - seen_labels)
        if unemitted:
            failures.append(f"no envelope emitted for: {unemitted}")

        print()
        if failures:
            print(f"=== {len(failures)} FAILURE(S) ===")
            for failure in failures:
                print(f"  !! {failure}")
            return 1

        print(f"=== all {len(lines)} envelopes accepted by the gateway ===")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
