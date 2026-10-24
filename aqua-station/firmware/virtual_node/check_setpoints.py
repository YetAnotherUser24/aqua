#!/usr/bin/env python3
"""
Assert the virtual node reads the setpoint keys the orchestrator actually sends.

Why this exists
---------------
This simulator shipped three commands with a key nobody sends. The firmware reads a key
with *no default*, so a missing key means the default applies and the node then acks
`applied` -- an honest-looking success for a setpoint it silently ignored. That is the
worst failure shape in this system: the console shows green, the recipe advances, and the
actuator never moved. It was found by watching a real run: `planta_2_step.p2_step` is
gated on the aerator reaching 240 RPM, the phase advanced to acquisition, and the fan was
still at 0 RPM.

The three, and what the recipe sends:

  - aerator `set_speed` read `"rpm"`; `recipes.py` sends `{"speed_percent": 40}` and the
    real node reads `cmd["speed_percent"]` (cloud_worker.cpp). 40% of 600 RPM = 240 RPM.
  - mixer `set_speed` read `"duty"`; nothing sends it. The real node reads `target_rpm`
    then `mixer_rpm` (cloud_worker.cpp), and `planta_1_deox.p1_prep` sends `mixer_rpm`.
  - a bare aerator `start` fell back to 300 RPM. The real node floors at
    `manual_throttle_pct` or 20% -- 120 RPM of 600, not 300.

Each check asserts the *effect* (the RPM the node reports), not that the command parsed,
because parsing was never the broken half.

Usage:  python3 check_setpoints.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

NODE = Path(__file__).with_name("virtual_node")


class Node:
    """The compiled host node, driven over the same protocol tools/virtual-nodes.py uses."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [str(NODE), "--hz", "20"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        #: role -> list of (drv_rpm, drv_rpm_target)
        self.rpm: dict[str, list[tuple[float, float]]] = {
            "aerator": [],
            "mixer": [],
        }
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            parts = raw.decode(errors="replace").rstrip("\n").split("\t", 2)
            if len(parts) < 3 or parts[0] != "TEL":
                continue
            role = (
                "aerator"
                if "/aerator" in parts[1]
                else ("mixer" if "/mixer" in parts[1] else None)
            )
            if role is None:
                continue
            try:
                env = json.loads(parts[2])
            except ValueError:
                continue
            for s in env.get("samples") or []:
                if s.get("drv_rpm") is not None:
                    self.rpm[role].append(
                        (s["drv_rpm"], s.get("drv_rpm_target"))
                    )

    def command(self, role: str, action: str, **target: object) -> None:
        frame = f"CMD\t{role}\t{json.dumps({'action': action, 'cmd_id': 'check', **target})}\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame.encode())
        self.proc.stdin.flush()

    def last(
        self, role: str, seconds: float = 3.0
    ) -> tuple[float, float] | None:
        """The most recent (rpm, target) for a role after letting it settle."""
        self.rpm[role].clear()
        time.sleep(seconds)
        return self.rpm[role][-1] if self.rpm[role] else None

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=5)


def main() -> int:
    if not NODE.exists():
        print(
            f"  FAIL: {NODE} not built -- run ./build.sh first",
            file=sys.stderr,
        )
        return 2

    node = Node()
    failures: list[str] = []
    time.sleep(1.5)
    try:
        # -- aerator: speed_percent, which is what the recipe sends ------------
        node.command("aerator", "set_speed", speed_percent=40)
        got = node.last("aerator")
        print(f"  aerator set_speed speed_percent=40  -> {got}")
        # 40% of kMaxRpm (600) = 240, the value p2_step's condition waits for.
        if not got or abs((got[1] or -1) - 240.0) > 1.0:
            failures.append(
                f"aerator set_speed speed_percent=40 gave target {got[1] if got else None}, "
                "expected 240.0 -- the key is not being read"
            )

        # -- aerator: a bare start floors at 20%, not 50% ----------------------
        node.command("aerator", "stop")
        node.last("aerator", 2.0)
        node.command("aerator", "start")
        got = node.last("aerator")
        print(f"  aerator bare start                  -> {got}")
        if not got or abs((got[1] or -1) - 120.0) > 1.0:
            failures.append(
                f"aerator bare start gave target {got[1] if got else None}, expected 120.0 "
                "(20% of kMaxRpm, matching cloud_worker.cpp)"
            )

        # -- aerator: an explicit rpm still works ------------------------------
        node.command("aerator", "set_speed", rpm=550)
        got = node.last("aerator")
        print(f"  aerator set_speed rpm=550           -> {got}")
        if not got or abs((got[1] or -1) - 550.0) > 1.0:
            failures.append(
                f"aerator set_speed rpm=550 gave target {got[1] if got else None}, expected 550.0"
            )

        # -- mixer: mixer_rpm, which p1_prep sends -----------------------------
        node.command("mixer", "set_speed", mixer_rpm=1200)
        got = node.last("mixer", 4.0)
        print(f"  mixer   set_speed mixer_rpm=1200    -> {got}")
        # 1200 RPM clamps to the 1000 RPM ESC ceiling -> duty 1.0 -> the model's top speed.
        # Assert it moved at all: the bug was the mixer never turning.
        if not got or (got[0] or 0.0) < 100.0:
            failures.append(
                f"mixer set_speed mixer_rpm=1200 left drv_rpm at {got[0] if got else None}; "
                "the key is not being read and the impeller never turned"
            )

    finally:
        node.close()

    if failures:
        print("\n  FAIL")
        for f in failures:
            print(f"    - {f}")
        return 1

    print(
        "\n  PASS: ack means applied -- setpoint keys match what the orchestrator sends"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
