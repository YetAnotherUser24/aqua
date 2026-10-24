# Lab Bench DO Control: Constraints, Zero-Overshoot Strategies & One-Way Dynamics

## 1. The Clean-Water Bench Reality

On a real aquaculture farm, fish and nitrifying bacteria continuously consume oxygen ($R_{bio} > 0$). If an aerator overshoots, biological respiration eventually pulls the DO back down.

On a **clean-water test bench** (standard ASCE reaeration testing):
1. **$R_{bio} = 0$**: There is zero biological consumption.
2. **Deox is strictly open-loop**: Sodium sulfite ($Na_2SO_3$ with $CoCl_2$ catalyst) is added only once before the test starts to strip oxygen down to $0\text{--}1\text{ mg/L}$.
3. **During the test, $Na_2SO_3$ injection is forbidden**: The aerator is the sole actuator.
4. **Natural reaeration is always positive**: Even if the aerator motor is completely shut off ($u = 0\text{ RPM}$):
   $$\frac{dC}{dt} = K_L a_{natural} \cdot (C^* - C) > 0 \quad (\text{for all } C < C^*)$$
   Ambient air naturally diffuses into the water across the surface.
5. **Temperature is isothermal**: In a climate-controlled lab, water temperature is stable ($T_{water} \approx T_{ambient}$), meaning saturation ceiling $C^*$ is fixed and temperature is not an active disturbance.

```
       CONSEQUENCE: THE RECOVERY FORCE IS PHYSICALLY ZERO (OR REVERSE)
  
   DO
    ▲                         Overshoot zone (FATAL ERROR)
    │                       ┌──────────────────────────────────────────────┐
 SP ┼─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
    │                      /│\                                             │
    │                     / │ \  Natural surface aeration PREVENTS return! │
    │                    /  │  \ Motor at 0 RPM CANNOT reduce DO!          │
    │                   /   └──────────────────────────────────────────────┘
    │                  /
    │                 /     Target trajectory MUST BE strictly monotonic
    │                /      (Overdamped, Zero Overshoot: Mp = 0.0%)
    │               /
  0 ┴──────────────┴───────────────────────────────────────────────────────► Time
```

> **Key Rule**: Any overshoot past the setpoint is an **irreversible failure** of the test. Once DO exceeds $SP$, the test run is ruined and the water must be chemically re-scrubbed with sulfite.

---

## 2. Process Analogies with One-Way / Irreversible Dynamics

Since there is zero cooling/purging, a standard symmetric plant model does not apply. The direct industrial analogies are plants with **strictly irreversible accumulation**:

| Reference Plant | Physical Phenomenon | Why It Matches Your Bench DO Plant |
|---|---|---|
| **Vacuum-Insulated Furnace / Autoclave (Heating only)** | Highly insulated chamber with electric heating and negligible heat dissipation to ambient. | If heating element overshoots target temperature, cooling takes hours or degrades product. Controller must brake early. |
| **Batch Tank Liquid Filling (No Drain Valve)** | Adding liquid from a high-flow pump into a container that cannot be drained backwards. | Overshooting the target level spills over or ruins chemical recipe. Deceleration curve is critical. |
| **Additive Manufacturing / Coating Thickness** | Thin-film vapor deposition or 3D printing. | You can deposit material, but you cannot subtract it. Final target thickness must be approached strictly from below. |
| **Resistor / Capacitor Trim & Etch** | Laser trimming or acid etching of microelectronics. | Subtractive process that only moves in one direction. Stopping at the exact tolerance is irreversible. |

---

## 3. The "Coasting" Effect (Latent Aeration + Sensor Lag)

In a liquid aeration plant, shutting off the motor does **not** stop DO from rising immediately:

1. **Micro-bubble Hold-up Delay ($\tau_{bubble}$)**:
   When the motor runs at high RPM, billions of fine micro-bubbles are entrained in the water column. When the motor hits $0\text{ RPM}$, those bubbles take $15\text{--}45\text{ seconds}$ to rise to the surface, continuously dissolving oxygen into the water while doing so.
2. **Optical Sensor Dynamic Delay ($\tau_{sensor}$)**:
   The OPTOD luminescent cap has an internal response lag ($T_{90} \approx 30\text{--}60\text{ s}$). The DO value currently reading on the screen is where the water was half a minute ago!

### The Coasting Horizon Equation
If you cut the aerator to 0 RPM right when $DO(t) = SP$, the real DO will overshoot by:
$$\Delta C_{coast} \approx \left( \frac{dC}{dt} \right) \cdot (\tau_{sensor} + \tau_{bubble})$$
If DO was rising at $0.05\text{ mg/L per sec}$, a 30-second combined lag results in a **$+1.5\text{ mg/L}$ overshoot**, completely ruining the experiment!

---

## 4. Control Strategies Tailored for Zero-Overshoot Bench Testing

### Strategy A: Safe Braking Boundary (Predictive Coasting Horizon)
Calculate the anticipated coasting DO before sending the motor command:
$$C_{predicted}(t) = C_{measured}(t) + \tau_{lag} \cdot \frac{dC}{dt}$$
- Where $\tau_{lag} \approx \tau_{sensor} + \tau_{bubble} \approx 35\text{--}50\text{ s}$.
- As soon as $C_{predicted}(t) \ge SP$, immediately force the motor to minimum speed or idle ($0\text{ RPM}$), allowing residual bubbles and sensor lag to glide cleanly onto the setpoint.

---

### Strategy B: Asymmetric Fuzzy Logic (The Natural Solution)
Fuzzy logic is ideal because rules can have **extreme asymmetry** hardcoded into their linguistic structure.

Looking at your implementation in `nodo-aerador/src/control_engine.cpp`:
- Error $e = SP - DO$
- Rate of change of error: $dError = \frac{\Delta e}{\Delta t}$
  *(Note: When DO is rising towards SP, $e$ is positive but shrinking, so $dError < 0$. This is labeled `u_improving`.)*

#### Rule Adjustments for Zero-Overshoot Bench Conditions:
1. **Never allow I-action or motor thrust when approaching setpoint**:
   If $e \in [0, 0.8\text{ mg/L}]$ and $dError < 0$ (`u_improving`), output must be strictly **`RPM_OFF`** (or minimum idle), not `RPM_LOW`.
2. **Braking zone for medium errors**:
   If $e \in [0.8, 1.8\text{ mg/L}]$ and $dError < 0$ (`u_improving`), motor should drop immediately to **`RPM_LOW`** (e.g. 25% RPM).
3. **Emergency Zero-Power Trigger**:
   If $e \le 0.1\text{ mg/L}$, force $u = 0\text{ RPM}$ regardless of any other rule.

---

### Strategy C: Trajectory Planning (Soft Setpoint Ramping)
Never feed a step change in setpoint ($SP = 0 \to 6.0\text{ mg/L}$) to a feedback loop on an irreversible plant.

Instead, route the desired target through an **asymptotic filter or S-curve profile**:
$$SP_{filtered}(k) = \alpha \cdot SP_{filtered}(k-1) + (1 - \alpha) \cdot SP_{target}$$
Or a linear ramp limiter:
$$\frac{dSP}{dt} \le \text{MaxSafeRate} \quad (\text{e.g. } 0.02\text{ mg/L per minute})$$

By forcing the setpoint to crawl slowly into the target, the feedback error never gets large enough to command full motor speed when close to the target.

---

### Strategy D: If Using PID — Mandatory Fixes
Standard PID is dangerous here because the integral term $\int e \, dt$ stores positive accumulated energy from the rise phase. When $DO$ hits $SP$, the integral term is at its **maximum**, forcing the motor to keep spinning!

1. **Conditional Integration (Freeze Zone)**:
   ```cpp
   // Only integrate when far from setpoint, and FREEZE/ZERO when close
   if (error > 1.5f) {
       _integral += error * dt;
   } else {
       // Bleed or clamp integral immediately to prevent push-through
       _integral *= 0.95f; 
   }
   ```
2. **Overdamped Tuning ($\zeta \ge 1.5\text{--}2.0$)**:
   Tuning parameters must satisfy $M_p = 0.0\%$. Integral gain $K_i$ should be extremely small, relying mostly on proportional and derivative damping ($PI\text{-}D$).
3. **Deadband**:
   Freeze control output and set RPM to 0 whenever $SP - 0.2\text{ mg/L} \le DO \le SP$.

