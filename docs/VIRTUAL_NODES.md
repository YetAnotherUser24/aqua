# Virtual nodes — convention

Every node in this workspace can be built in a **virtual** flavour that runs the real
control logic on the ESP32 against a **simulated device layer**. Virtual firmware talks to
the **real AquaControl backend** under the node's normal `DEVICE_KEY`, so the dashboard and
orchestrator cannot tell it apart from hardware (by design). ESP-NOW radio is **not**
emulated — only each node's cloud-facing behaviour.

## Principles

1. **Simulate at the device boundary, not the driver boundary.** Model the *peripheral*
   (OD sensor register bank, ESC + motor, encoder + pump head), not the node's driver class.
   The node's own logic (PID, dosing controller, failsafe, cloud worker) then runs unmodified
   in both builds.
2. **One injected dependency, chosen once at boot.** `main.cpp` is a composition root: it
   receives one bus object (simulated or real peripheral set) and passes it down. Do **not**
   sprinkle `#if VIRTUAL` through control code — the only compile-time switch selects which
   translation unit provides the bus.
3. **Virtual is a first-class build target, not a debug flag.** The virtual peripheral is
   compiled only into the `-virtual` env and the real one only into real envs (via
   `build_src_filter`), so neither can leak into the other's binary.
4. **Behaviour comes from a declarative device model.** Each virtual peripheral is a single
   *spec* (register map, duty→rpm curve, duty→flow) plus a small generic tick engine, so the
   "what the hardware does" lives in one reviewable place and is swappable
   (nominal / degraded / stuck) without touching control code.
5. **Deterministic time.** The virtual bus takes an explicit `now_us`/`now_ms` tick instead of
   calling `millis()` internally, so the same simulator runs on-hardware (driven by the loop)
   and on the host (driven by a test clock) with reproducible output.
6. **Faults are data-driven.** Fault injection is a schedule (`at 5s sensor offline`,
   `at 12s ESC STALL`), not bespoke setters.
7. **The real path is the source of truth.** A host conformance suite runs the same assertions
   against the simulated *and* real peripheral (real via the `test/host/` Arduino shim), so the
   two cannot diverge silently.

## File layout (per node)

```
src/<node>_bus.h            # small interface: the peripherals the node needs
src/<node>_bus_real.cpp     # thin hardware I/O (UART/Modbus, LEDC, ISR, I2C) — no control logic
src/<node>_bus_virtual.cpp  # binds the sim to the interface
src/<node>_sim.h/.cpp       # declarative model spec + tick engine (host-testable, no Arduino APIs)
main.cpp                    # single path; takes <node>_bus& from create<Node>Bus()
test/host/test_<node>_bus.cpp  # same assertions against real (shim) and virtual
```

## Naming

| Thing | Convention |
|---|---|
| Environment | `[env:<board>-virtual]` (sensor-od keeps its legacy `virtual` alias) |
| Build flag | `-D<NODE>_VIRTUAL=1` |
| Interface | `I<Node>Bus` in `src/<node>_bus.h` |
| Factory | `create<Node>Bus()` + `destroy<Node>Bus()` |

## Safety

Never run a real node and a virtual node with the **same `DEVICE_KEY`** at the same time —
the backend cannot distinguish them and telemetry would interleave.

## Using a virtual build

The virtual build is the **first half** of bringing up a real board: it runs the real control
logic against the simulated peripheral, so it proves the MQTT and telemetry chain before a
motor or probe is in the loop. A failure there is the stack, not the mechanism.

Build order for bench work is `-virtual` first, real second — see `docs/DEPLOYMENT.md` §4.2
for the exact commands and the credential quoting, and `aqua-station/docs/TEST-PLAN.md` for
the procedure to walk a board through.

Note the two ESP32-S3 roles: `nodo-aerador` and `nodo-mezclador`. They share no telemetry
fields and no command verbs, so establish which board is on the bench before flashing
anything (`docs/TEST-PLAN.md` §0.1).
