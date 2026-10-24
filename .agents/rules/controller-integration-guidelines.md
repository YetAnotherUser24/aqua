# Controller Integration & Tuning Guidelines for Agents

When designing, refactoring, or testing controllers in the AquaControl workspace, agents MUST follow these core principles:

1. **Separation of Concerns (Deterministic Core vs. Cloud Worker):**
   - **Core 1 (20 Hz / 100 Hz):** Dedicated exclusively to the control loop, sensor processing, and actuator UART/PWM ownership. Never make blocking network calls, flash writes, or allocations here.
   - **Core 0 (FreeRTOS):** Dedicated to WiFi, MQTT ingest/publish, and flash storage. State transfer between Core 0 and Core 1 MUST be guarded by spinlocks (`portENTER_CRITICAL` / `portEXIT_CRITICAL`).

2. **One-Way / Asymmetric Plant Physics:**
   - In clean-water bench tests, biological respiration is ZERO ($R_{bio} = 0$).
   - Actuators cannot extract oxygen ($u \ge 0$).
   - Overshoot past the target DO ($C > SP$) is an unrecoverable failure requiring chemical deoxygenation.
   - Any PID controller MUST implement anti-windup clamping and freeze integration outside a narrow error band ($|e| \le 0.8\text{ mg/L}$).
   - Prefer asymmetric fuzzy logic or predictive coasting cutoff ($C_{pred} = C_{meas} + \tau_{lag} \cdot \frac{dC}{dt}$) to stop the motor before crossing $SP$.

3. **Injected Bus Architecture (Real vs. Virtual):**
   - Do NOT insert `#ifdef` branches inside control logic.
   - Use the dependency injection pattern (`IMotorBus`, `IPwmActuator`, `IRotaryEncoder`). The same control code must execute identically on physical ESP32 boards and in the `-virtual` simulation environment.
   - Always run unit tests in `test/host/` or `aqua-station/firmware/virtual_node` before flashing physical boards.

4. **MQTT Command Contracts:**
   - Keep command field keys consistent across `recipes.py`, `PhaseForm.tsx`, and `cloud_worker.cpp`.
   - Send both `target_do` and `setpoint_mg_l` when configuring DO setpoints to maintain compatibility between firmware and web UI inspector.
