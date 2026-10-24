#!/usr/bin/env python3
"""
Assert the virtual node's bench `set_do` override holds, clears, and clamps.

Why this exists
---------------
A recipe's first phase is often a *physical precondition*. `planta_2_step.p2_precond`
gates on `DO < 3.5 mg/L`, and the plant models DO physically: respiration alone takes
~42 min to fall from 7.4, and the fast path (dosing sulfite) needs the pump, which needs
the mixer. So on a partial bench any precondition-gated recipe was untestable -- a gap in
the simulator, not the station.

`set_do` closes that gap, but it is the one verb that writes a *physical* value directly
instead of simulating it. Three things can silently go wrong and none is visible by
reading the code:

  1. **It seeds instead of holding.** The first draft assigned `plant.doMgL` once. The next
     tick resumed respiration and the value drifted off the requested one, so a recipe
     waiting for `DO < 3.5` saw 3.2 then 2.9 then... and a recipe waiting for `DO >= 5`
     could never be held there at all. An override that drifts is not an override.
  2. **Clearing does not clear.** If the override is stored but the clear path forgets it
     (or a second, dead `set_do` branch shadows the live one -- which is exactly what
     shipped), the plant stays pinned and the "return to physics" path is untestable.
  3. **Out-of-range values are accepted.** The plant clamps to saturation; an override of
     99 mg/L would make the sensor report a physically impossible number and quietly
     corrupt any experiment built on it.

This drives the real binary over the same stdin protocol `tools/virtual-nodes.py` uses
(`CMD<TAB><role><TAB><json>`) and reads the samples off stdout, so it tests the shipped
artefact rather than a copy of its logic.

Usage:  python3 check_do_override.py [path-to-virtual_node]
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
    """The compiled host node, driven over its stdin/stdout protocol."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [str(NODE), "--hz", "20"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.samples: list[float] = []
        self._stop = False
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        # stdout is `TEL<TAB><topic><TAB><json>` per envelope (PRESENCE lines share the
        # shape). The binary publishes all four roles, so filter to the od_sensor topic
        # rather than assuming a single-role stream.
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            if self._stop:
                return
            parts = raw.decode(errors="replace").rstrip("\n").split("\t", 2)
            if (
                len(parts) < 3
                or parts[0] != "TEL"
                or "od_sensor" not in parts[1]
            ):
                continue
            try:
                env = json.loads(parts[2])
            except ValueError:
                continue
            for s in env.get("samples") or []:
                v = s.get("od_do_mg_l")
                if v is not None:
                    self.samples.append(float(v))

    def command(self, action: str, **target: object) -> None:
        """Send one command exactly as `tools/virtual-nodes.py` forwards it."""
        fwd = {"action": action, "cmd_id": "check", **target}
        frame = f"CMD\tod_sensor\t{json.dumps(fwd)}\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame.encode())
        self.proc.stdin.flush()

    def window(self, seconds: float) -> list[float]:
        """Samples observed over the next `seconds`, then forget them."""
        self.samples.clear()
        time.sleep(seconds)
        return list(self.samples)

    def settle(self, seconds: float) -> None:
        """Discard whatever is in flight so the next window holds only post-command data.

        An envelope is a *batch*: five samples stamped over 25 s and delivered at once. A
        window opened immediately after a command therefore contains the tail of the batch
        that was already being assembled -- samples taken *before* the command applied. That
        is why `set_do 3.2` first read 3.200 .. 7.370: the 7.370 was the previous envelope.
        Wait out one full batch, then start measuring.
        """
        time.sleep(seconds)

    def close(self) -> None:
        self._stop = True
        self.proc.kill()
        self.proc.wait(timeout=5)


#: The od_sensor flushes one envelope per batch, and the real node samples every 5 s
#: (`nodo-sensor-od/src/main.cpp`, `experimentIntervalMs = 5000`) with a batch of 5. So the
#: first envelope is ~25 s after boot -- its honest cadence is 0.2 Hz. Any window shorter
#: than that reads as "the node is dead", which is how this check first failed against a
#: perfectly healthy node.
BOOT_S = 30.0


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
        # Start monitoring and let the first envelope land. The sensor's cadence is 0.2 Hz,
        # so waiting less than a batch interval is indistinguishable from a dead node.
        node.command("start_monitor")
        base = node.window(BOOT_S)
        assert base, (
            f"no telemetry in {BOOT_S:.0f}s -- the node is not publishing"
        )
        lo, hi = min(base), max(base)
        print(f"  baseline      DO {lo:.3f} .. {hi:.3f}  (n={len(base)})")
        if not 6.0 < (lo + hi) / 2 < 9.0:
            failures.append(
                f"baseline DO {lo:.2f}-{hi:.2f} is not near saturation"
            )

        # -- 2. the override must HOLD, not seed ------------------------------
        node.command("set_do", do_mg_l=3.2)
        node.settle(BOOT_S)  # let the pre-command batch drain
        held = node.window(BOOT_S)
        assert held, "no telemetry after set_do"
        lo, hi = min(held), max(held)
        spread = hi - lo
        print(f"  set_do 3.2    DO {lo:.3f} .. {hi:.3f}  spread={spread:.4f}")
        if abs(hi - 3.2) > 1e-6 or abs(lo - 3.2) > 1e-6:
            failures.append(
                f"override did not hold at 3.2 (saw {lo:.3f}-{hi:.3f}); "
                "it seeded and then drifted"
            )

        # -- 3. clamping: above saturation is pulled to Cs --------------------
        node.command("set_do", do_mg_l=99.0)
        node.settle(BOOT_S)
        clamped = node.window(BOOT_S)
        if clamped:
            hi_c = max(clamped)
            print(f"  set_do 99.0   DO {min(clamped):.3f} .. {hi_c:.3f}")
            if hi_c > 9.0:
                failures.append(
                    f"override 99.0 was not clamped to saturation (saw {hi_c:.2f})"
                )

        # -- 4. clearing must return to physics ------------------------------
        node.command("set_do", do_mg_l=3.2)
        node.settle(BOOT_S)
        node.command("set_do")  # no value -> clear
        node.settle(BOOT_S)
        cleared = node.window(BOOT_S)
        assert cleared, "no telemetry after clearing the override"
        print(f"  cleared       DO {min(cleared):.3f} .. {max(cleared):.3f}")
        # Respiration pulls it down and the clamp is gone, so it must move off 3.2.
        if all(abs(v - 3.2) < 1e-9 for v in cleared):
            failures.append(
                "clear did not release the override; still pinned at 3.2"
            )

    finally:
        node.close()

    if failures:
        print("\n  FAIL")
        for f in failures:
            print(f"    - {f}")
        return 1

    print("\n  PASS: set_do holds, clamps, and clears")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
