# ODrive S1 Aerator Control & Telemetry Invariants

When working on the aerator node firmware, gateway schema, or SCADA charts, agents MUST adhere to these constraints:

1. **Velocity Limits & Physical Clamping:**
   - The M8325s motor driven by ODrive S1 is physically capped at **600 RPM** (`MAX_MOTOR_RPM = 600.0f`).
   - In firmware (`odrive_uart.cpp`, `main.cpp`, `aqua_link.cpp`) and frontend charts (`ObservatoryChart.tsx`, `MultivariableInspector.tsx`), ALWAYS clamp `actual_rpm` to $[0, 600\text{ RPM}]$.
   - Discard UART velocity spikes $> 650\text{ RPM}$ to prevent ASCII buffer fragmentation or negative jitter underflow glitches (e.g. 3248 RPM or 64947 RPM) when motor is stationary in IDLE.

2. **ODrive S1 ASCII Protocol States & Faults:**
   - Communication is over UART at 115200 baud on ESP32-S3.
   - State 8 = `CLOSED_LOOP_CONTROL` (Armado).
   - State 1 = `IDLE` (En Reposo / Desarmado).
   - Telemetry must carry:
     - `drv_state`: Axis state (`axis0.current_state`).
     - `drv_status`: Active errors bitmask (`axis0.active_errors`).
     - `drv_disarm_reason`: Error code that caused the last disarm (`axis0.disarm_reason`).
   - Use `odriveStatus.ts` in the web frontend to decode bitmasks (e.g. `0x100` = DC_BUS_UNDER_VOLTAGE, `0x20` = DRV_FAULT, `0x40000` = SPINOUT_DETECTED, `0x800` = CURRENT_LIMIT_VIOLATION) into Spanish descriptions with actionable advice for the operator.

3. **Monotonic X Timestamps in Recharts:**
   - Recharts monotone spline paths divide by delta-X ($\Delta x = x_{i+1} - x_i$). A zero delta-X produces `NaN` in SVG path coordinates, causing the rendered chart lines to vanish or flicker.
   - Enforce strictly increasing $X$ timestamps (`sample.ms > lastMs`) across all chart data streams.

