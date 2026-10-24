## Plan: Virtual (no-hardware) firmware for all four nodes

Give every node a **firmware `virtual` build environment** that runs the *real* control logic on the ESP32 against a **simulated device layer**, posts to the **real Vercel/Supabase backend under the same `DEVICE_KEY`**, and does **not** emulate ESP-NOW. `odrive_virtual` is explicitly **not** the reference — it is the anti-pattern this plan replaces.

### Design principles (the new architecture)

The existing `odrive_virtual` is cited only for what to avoid: a hand-written physics model bolted beside the real driver, sharing no interface, drifting from it, and "tested" by a copy of itself. The replacement architecture is:

1. **Simulate at the device boundary, not the driver boundary.** Model the *peripheral* (OD sensor register bank, ESC + motor, encoder + pump head), not the node's driver class. The node's own logic (PID, dosing controller, failsafe, cloud worker) then runs unmodified in both builds — the whole point of the exercise.
2. **One injected dependency, chosen once at boot.** The firmware is a *composition root*: `main.cpp` receives one bus object (simulated or real peripheral set) and passes it down. No `#if VIRTUAL` branches sprinkled through control code; the only compile-time switch selects which translation unit provides the bus.
3. **Virtual is a first-class build target, not a debug flag.** The virtual peripheral compiles only into the `-virtual` env and the real one only into real envs (via `build_src_filter`), so neither can leak into the other's binary.
4. **Behaviour comes from a declarative device model, not scattered physics.** Each virtual peripheral is a single **spec** (register map for the OD sensor, duty→rpm curve + spin-up time for the ESC, duty→flow for the pump) plus a small generic tick engine. "What the hardware does" lives in one reviewable place and is swappable (nominal / degraded / stuck) without touching control code.
5. **Deterministic time.** The virtual bus takes an explicit `now_us`/`now_ms` tick instead of calling `millis()` internally, so the same simulator runs on-hardware (driven by the loop) and on the host (driven by a test clock) with reproducible output.
6. **Faults and scenarios are data-driven.** Fault injection is a schedule (`at 5s sensor offline`, `at 12s ESC STALL`), not bespoke setters — one mechanism covers sensor error codes, ESC fault-pulse codes, and the pump's stuck encoder.
7. **The real path is the source of truth.** The shared interface is derived from what the real hardware path already needs; a host **conformance suite** runs the same assertions against the simulated *and* real peripheral (real via the `test/host/` Arduino shim), so the two cannot diverge silently.

### Resulting shape (per node)

```
src/<node>_bus.h           // small interface: the peripherals the node needs (real source of truth)
src/<node>_bus_real.cpp    // thin hardware I/O (UART/Modbus, LEDC, ISR, I2C) — no control logic
src/<node>_bus_virtual.cpp // binds the sim to the interface
src/<node>_sim.h/.cpp      // declarative model spec + tick engine (host-testable, no Arduino APIs)
main.cpp                   // single path; takes <node>_bus& from create<node>Bus()
test/host/test_<node>_bus.cpp  // same assertions against real (shim) and virtual
```

### Steps

**Phase 0 — Conventions** *(do first, blocks everything)*
1. Write the principles above into a short repo-root doc (there is no root `AGENTS.md` although many files cite it) and summarize per node's README.
2. Standardize env naming `<board>-virtual` (keep sensor-od's `virtual` as an alias) and the file-layout convention above.

**Phase 1 — `nodo-aerador` (template node; replaces `odrive_virtual`)**
3. Add `src/motor_bus.h`: a minimal interface exposing only what the node actually exercises — command velocity, arm/idle, clear faults, telemetry snapshot (`vbus, ibus, rpm, iq, torque, fet_temp, state, errors, armed`). Verified uses: `begin/setVelocityRpm/enterClosedLoop/enterIdle/clearErrors/pollTelemetry/updateTieredTelemetry/writeFloatProperty`, and `iq_measured` is consumed by cloud + SD.
4. Add `src/motor_bus_real.cpp` wrapping the existing `ODriveUART` (no behaviour change to the real path).
5. Add `src/motor_sim.h/.cpp` — the declarative motor+ODrive model: velocity command → first-order spin-up toward a **600 RPM** envelope, quadratic water load → bus current, bus sag, FET temp, IDLE↔CLOSED_LOOP state machine — plus a fault schedule.
6. Add `src/motor_bus_virtual.cpp` binding the sim. **Delete `odrive_virtual.h/.cpp`** rather than extend them.
7. Refactor `main.cpp` to a single path with `IMotorBus&` from `createMotorBus()`; remove the duplicated `#if/#else` init.
8. Fix stale constants/docs (`config.h` `3500 RPM`→600; README virtual section).
9. Replace `test/test_odrive_virtual.cpp` with `test/host/test_motor_bus.cpp`: **one command script + one assertion set** run against both the sim and the real wrapper (fake UART), including the fault schedule.

**Phase 2 — `nodo-sensor-od`**
10. Add `src/sensor_bus.h`: `readSensor(...)` → temp/DO/sat + validity, and `readBatteryMilliVolts()`. **Battery is ADC-only** — no INA226 work; `sensorReadings[5]` stays in mV.
11. Add `src/sensor_bus_real.cpp` reusing the existing Modbus sequence and `batteryAdcRead()`.
12. Add `src/sensor_sim.h/.cpp` — a **register-bank model of the OPTOD** (so the sim speaks the sensor's semantics, not the node's) with a fault schedule covering the Digisens error codes (`9999/9998/9996`) and status bit 22.
13. Add `src/sensor_bus_virtual.cpp`; delete the `#if SENSOR_VIRTUAL` blocks from `sensor_engine.cpp`.
14. Keep the `virtual` env (WiFi + Web Master) and the existing `DISABLE_ESPNOW`/`DISABLE_BLE` stubs (no radio emulation).
15. Replace `test/test_virtual_sensor.cpp` with a host conformance test over the register-bank model + fault schedule.

**Phase 3 — `nodo-mezclador`**
16. Add `src/mixer_bus.h`: `setDuty(float)`, `readTach()` → velocity state, `readKnob()`.
17. Add `src/mixer_bus_real.cpp` extracting `initPwmHardware`/`applyHardwareDuty`, the `onPulseRisingEdge` ISR + `updateSpeedMeasurement`, and `analogRead`.
18. Add `src/mixer_sim.h/.cpp` — a **duty→rpm device model** (7 pole pairs, first-order spin-up, deadband at `MOTOR_MIN_SPIN_DUTY`) plus the FG fault-pulse code schedule; `mixer_bus_virtual.cpp` binds it. The `PidController` above is untouched and runs in both builds.
19. Refactor `esp32s3_main.cpp` to the injected-bus path; add `[env:esp32-s3-virtual]` with the virtual bus in `build_src_filter`.
20. Add `test/host/test_mixer_bus.cpp` (same assertions against both buses; fault-pulse decoding is part of the contract).

**Phase 4 — `nodo-bomba`**
21. Add `src/pump_bus.h`: `setDuty(float)`, `readEncoderAngle()` (12-bit).
22. Add `src/pump_bus_real.cpp` extracting `applyDuty` and `readRawAngle`.
23. Add `src/pump_sim.h/.cpp` — a **duty→flow device model** (rpm∝duty, 4096 ticks/rev, wrap-around) + stuck-encoder fault schedule; `pump_bus_virtual.cpp` binds it. Refactor `main.cpp` to the injected-bus path.
24. `platformio.ini`: add `[env:esp32dev-virtual]`; keep legacy T-200 files excluded.
25. Add `test/host/` conformance test for revolution accumulation + the stuck fault.

**Phase 5 — Consistency & on-hardware validation**
26. Align `Tesis-webpage/scripts/virtual-bench/models/constants.js` and fix its `Codigos/` paths so `verify-twin-constants.js`/`verify-pid-fidelity.js` run green here; document that the firmware sim is the source of truth for the twin.
27. Document each node's virtual mode and the safety rule "never run real + virtual with the same device key at once".

### Relevant files
- `nodo-aerador/src/{main.cpp, odrive_uart.h/.cpp, odrive_virtual.h/.cpp (delete), config.h}`, `platformio.ini`, `test/test_odrive_virtual.cpp`, `test/host/Arduino.h`
- `nodo-sensor-od/src/{sensor_engine.cpp/.h, n_logger_config.h, esp_now_sender.h}`, `platformio.ini`, `test/test_virtual_sensor.cpp`
- `nodo-mezclador/src/esp32s3_main.cpp` (`onPulseRisingEdge`, `updateSpeedMeasurement`, `applyHardwareDuty`, `PidController`, `setup`/`loop`), `platformio.ini`
- `nodo-bomba/src/{main.cpp, config.h}` (`readRawAngle`, `applyDuty`, `dosingLoop`), `platformio.ini`
- `Tesis-webpage/scripts/virtual-bench/{models/constants.js, verify-twin-constants.js, verify-pid-fidelity.js, config.js}`

### Verification
Build and flash on the **connected ESP32-S3** (`/dev/ttyACM0`, Espressif USB JTAG — confirmed working: `pio run -e esp32-s3` builds, `-e esp32-s3-virtual -t upload --upload-port /dev/ttyACM0` flashes).

1. Per node, build both real and virtual envs (`pio run -e <real>` and `-e <virtual>`) — no regression on real envs.
2. Host conformance tests per node via `g++` (same assertions against real and virtual buses + fault schedules).
3. Flash each virtual env on the S3: boot banner says VIRTUAL, WiFi connects, telemetry lands in the correct archive table under the correct device key, a queued command round-trips with an ACK.
4. Fault schedule produces the expected status (sensor bit 22, mixer fault code, aerador `axis_error`).
5. End-to-end: all 4 virtual nodes against the prod API with the orchestrator IDLE→ACTIVE; verify `start_experiment`, dose, mixer-on and manual-override commands are applied and ACKed.

### Decisions
- Firmware env form; real backend with same device keys; no ESP-NOW emulation; protocol/contract fidelity.
- Device-boundary simulation via an injected bus (composition root), declarative device models + fault schedules, deterministic tick time, host conformance tests against the real path.
- **Excluded**: radio/ESP-NOW emulation, physics-identified plant models, overhauling the JS twin beyond constant/path alignment, removing `nodo-bomba`'s dead legacy code, and any new INA226 work (battery is ADC-only).

### Further Considerations
1. **Env naming** — unified `<board>-virtual`, keeping `virtual` as a sensor-od alias: **A)** keep both (recommended) / **B)** rename and accept the break.
2. **JS twin** — align constants/paths now (small, keeps the drift guard honest) vs. defer: **A)** align now (recommended) / **B)** leave and note.
3. **`nodo-bomba` legacy dead code** — leave as-is with a note (recommended) vs. quarantine into `legacy/`.
