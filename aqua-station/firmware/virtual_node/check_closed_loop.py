#!/usr/bin/env python3
"""
Assert the aerator's closed loop actually regulates dissolved oxygen.

Why this exists
---------------
The host node accepted `set_mode`/`set_config` and only echoed the mode into its ack: it
never stored a setpoint and never closed a loop. `closed_loop` therefore looked configured
while `drv_rpm` sat at whatever the previous phase had left, and the protocol it exists to
run -- Protocolo C, PID regulation -- was untestable on the bench.

Two defects were hiding behind that, and they are independent:

  1. **The simulator had no loop.** Even with a setpoint the node held a fixed throttle.
  2. **The recipe sent a setpoint the node cannot read.** `cloud_worker.cpp` reads
     `p["target_do"]` into `cfg.target_do_mg_l`, while the recipe sent `setpoint_mg_l` --
     the console's name. On real hardware the key was never found, so the loop regulated
     toward the 7.5 mg/L default from `control_engine.h` while the phase, the ack and the
     dashboard all said 6.00.

This checks the first. The second is checked in the gateway suite
(`test_a_pid_setpoint_is_sent_under_the_key_the_node_reads`), because only the recipe can
see it.

The law mirrors the firmware's (`control_engine.cpp`):

    error = target_do - measuredDo      # positive -> DO too low -> aerate more
    rpm   = constrain(kp * error * kMaxRpm, 0, kMaxRpm)

The node runs a full PID; the host model is proportional on purpose. P-only is enough to
make regulation observable, and the integral and derivative terms only shape the approach
transient, which no phase gates on.

Usage:  python3 check_closed_loop.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

NODE = Path(__file__).with_name("virtual_node")

#: From the plant model (`Plant::kMaxRpm`) and the recipe's gains.
K_MAX_RPM = 600.0
KP = 0.10


class Node:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [str(NODE), "--hz", "5"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.do: list[float] = []
        self.rpm: list[float] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            parts = raw.decode(errors="replace").rstrip("\n").split("\t", 2)
            if len(parts) < 3 or parts[0] != "TEL":
                continue
            try:
                env = json.loads(parts[2])
            except ValueError:
                continue
            role = env.get("role")
            for s in env.get("samples") or []:
                if role == "od_sensor" and s.get("od_do_mg_l") is not None:
                    self.do.append(float(s["od_do_mg_l"]))
                if role == "aerator" and s.get("drv_rpm") is not None:
                    self.rpm.append(float(s["drv_rpm"]))

    def command(self, role: str, action: str, **target: object) -> None:
        frame = f"CMD\t{role}\t{json.dumps({'action': action, 'cmd_id': 'check', **target})}\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame.encode())
        self.proc.stdin.flush()

    def settle(self, seconds: float) -> None:
        """Discard in-flight samples so the next reading is post-command."""
        self.rpm.clear()
        time.sleep(seconds)

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
    try:
        node.command("od_sensor", "start_monitor")
        time.sleep(2)
        node.command("aerator", "set_mode", mode="pid")
        node.command("aerator", "set_config", target_do=6.0, kp=KP)
        node.command("aerator", "start", speed_percent=40)

        # -- DO below the setpoint -> the loop must aerate ---------------------
        # `set_do` holds the plant at a value, which is what makes the error stable enough to
        # assert against; the loop is reacting to the sensor, so a drifting plant would only
        # make the expected RPM a moving target.
        node.command("od_sensor", "set_do", do_mg_l=2.0)
        node.settle(8.0)
        expected_low = min(K_MAX_RPM, max(0.0, KP * (6.0 - 2.0) * K_MAX_RPM))
        got_low = node.rpm[-1] if node.rpm else None
        print(
            f"  DO 2.0 vs SP 6.0 (error +4.0) -> drv_rpm {got_low}  (expected ~{expected_low:.0f})"
        )
        if got_low is None or abs(got_low - expected_low) > 1.0:
            failures.append(
                f"with DO below the setpoint the loop commanded {got_low}, expected "
                f"~{expected_low:.0f} RPM -- the loop is not regulating"
            )

        # -- DO above the setpoint -> the loop must not aerate -----------------
        node.command("od_sensor", "set_do", do_mg_l=7.0)
        node.settle(8.0)
        got_high = node.rpm[-1] if node.rpm else None
        print(
            f"  DO 7.0 vs SP 6.0 (error -1.0) -> drv_rpm {got_high}  (expected 0)"
        )
        if got_high is None or got_high > 1.0:
            failures.append(
                f"with DO above the setpoint the loop commanded {got_high} RPM, expected 0 -- "
                "an aerator that runs above its setpoint drives the tank the wrong way"
            )

        # -- the setpoint the node stores must be the one it was given ---------
        # A different setpoint has to move the answer, or the loop is reading the default.
        node.command("od_sensor", "set_do", do_mg_l=3.0)
        node.command("aerator", "set_config", target_do=5.0, kp=KP)
        node.settle(8.0)
        expected_moved = min(K_MAX_RPM, max(0.0, KP * (5.0 - 3.0) * K_MAX_RPM))
        got_moved = node.rpm[-1] if node.rpm else None
        print(
            f"  DO 3.0 vs SP 5.0 (error +2.0) -> drv_rpm {got_moved}  (expected ~{expected_moved:.0f})"
        )
        if got_moved is None or abs(got_moved - expected_moved) > 1.0:
            failures.append(
                f"after set_config target_do=5.0 the loop commanded {got_moved}, expected "
                f"~{expected_moved:.0f} -- the setpoint is not being stored (a default would "
                "give the same answer for both setpoints)"
            )

    finally:
        node.close()

    if failures:
        print("\n  FAIL")
        for f in failures:
            print(f"    - {f}")
        return 1

    print(
        "\n  PASS: the aerator's loop regulates toward the configured setpoint"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
