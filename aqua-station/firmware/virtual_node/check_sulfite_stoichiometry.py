#!/usr/bin/env python3
"""
Assert a sulfite dose removes the oxygen its stoichiometry says it should.

Why this exists
---------------
The plant's sulfite consumption had a unit bug that made the whole deox protocol look
broken: the cap on how fast sulfite can strip oxygen was written as a *rate*
(`doMgL * 0.02 + 0.0005`, mg/L per second) but used as a *per-tick amount* and added to
a term that is later multiplied by `dt`. The sulfite ledger was drained by the same
un-scaled number, so the two sides disagreed by 1/dt and the ledger won -- at 5 Hz the
entire 12.5 mL dose was consumed in ~13 ticks (2.6 s) removing **0.34 mg/L** instead of
the **1.81 mg/L** the reaction calls for.

The symptom was indirect and easy to misread: `planta_1_deox.p1_deox` waits for
`DO < 3.0`, the DO trace sat flat at 7.0, and the phase simply timed out -- which looks
like "the sulfite did nothing", not like a factor-of-5 arithmetic error. Reading the
model did not surface it; measuring the DO drop against the stoichiometric prediction did.

Stoichiometry (from the recipe's own text and the model's constants):

    2 Na2SO3 + O2 -> 2 Na2SO4          1.0/7.878 mg O2 per mg Na2SO3
    stock 100 g/L  ->  kSulfiteMgPerMl   = 100.0 mg Na2SO3 per mL
    active volume  ->  kVolumeL          = 87.5 L

so `dose_ml` mL must remove `dose_ml * 100 / 87.5 / 7.878` mg/L, plus or minus the
aeration/respiration the tank is doing at the same time. The tolerant band below is wide
enough for that background (the aerator is off, the mixer adds a little re-oxygenation)
but far tighter than the factor-of-5 error it is guarding against.

Usage:  python3 check_sulfite_stoichiometry.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

NODE = Path(__file__).with_name("virtual_node")

#: From the plant model / the recipe's chemistry section.
SULFITE_MG_PER_ML = 100.0
OXYGEN_PER_SULFITE_MG = 1.0 / 7.878
VOLUME_L = 87.5

DOSE_ML = 12.5
#: mg/L of oxygen a 100%-efficient dose must remove. ~1.813 for the numbers above.
EXPECTED_DROP = DOSE_ML * SULFITE_MG_PER_ML / VOLUME_L * OXYGEN_PER_SULFITE_MG


class Node:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [str(NODE), "--hz", "5"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.do: list[float] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
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
                if s.get("od_do_mg_l") is not None:
                    self.do.append(float(s["od_do_mg_l"]))

    def command(self, role: str, action: str, **target: object) -> None:
        frame = f"CMD\t{role}\t{json.dumps({'action': action, 'cmd_id': 'check', **target})}\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame.encode())
        self.proc.stdin.flush()

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
        # The od_sensor samples every 5 s and batches 5, so an envelope is ~25 s. The dose is
        # spread over 12 s, so both the "before" and "after" readings need a full batch.
        node.command("od_sensor", "start_monitor")
        time.sleep(30)
        before = node.do[-5:] if len(node.do) >= 5 else node.do
        if not before:
            print(
                "  FAIL: no telemetry; the node is not publishing",
                file=sys.stderr,
            )
            return 2
        do_before = sum(before) / len(before)
        print(f"  before dose   DO {do_before:.3f}  (n={len(before)})")

        # Mixer on: the recipe runs it during the dose, and it is what makes the sulfite act.
        node.command("mixer", "start_mixer", target_rpm=1000)
        node.command("pump", "dose", target_ml=DOSE_ML)

        # Let the reaction finish, then read a settled batch. The reaction is fast (seconds);
        # the 60 s is for the sensor's own 25 s envelope plus the aerator-off plateau.
        time.sleep(75)
        node.do.clear()
        time.sleep(30)
        after = node.do
        if not after:
            print("  FAIL: no telemetry after the dose", file=sys.stderr)
            return 2
        do_after = sum(after) / len(after)
        print(f"  after dose    DO {do_after:.3f}  (n={len(after)})")

        drop = do_before - do_after
        print(
            f"  drop          {drop:.3f} mg/L   (stoichiometry says {EXPECTED_DROP:.3f})"
        )
        # Half to double the prediction: generous for the mixer's background re-oxygenation,
        # ample to catch the factor-of-5 shortfall the bug produced (0.34 vs 1.81).
        if not (0.6 * EXPECTED_DROP <= drop <= 1.4 * EXPECTED_DROP):
            failures.append(
                f"a {DOSE_ML} mL dose removed {drop:.3f} mg/L, expected about "
                f"{EXPECTED_DROP:.3f} (0.6-1.4x). A sulfite ledger that drains faster than "
                "the oxygen it is charged for gives a small drop with a normal-looking trace."
            )
    finally:
        node.close()

    if failures:
        print("\n  FAIL")
        for f in failures:
            print(f"    - {f}")
        return 1

    print(
        "\n  PASS: a sulfite dose removes the oxygen its stoichiometry calls for"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
