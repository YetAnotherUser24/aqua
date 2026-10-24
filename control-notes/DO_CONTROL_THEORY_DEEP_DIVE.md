# Dissolved Oxygen Control: Theoretical Foundations, Asymmetric Dynamics & Academic Literature

## 1. Mathematical Modeling of Clean-Water Non-Steady State Aeration

### 1.1 The Mass Transfer Governing Equation
In accordance with the **ASCE Standard ASCE/EWRI 2-22 (and 2-06)** (*Measurement of Oxygen Transfer in Clean Water*), non-steady state aeration in a clean-water basin is governed by two-film gas-liquid mass transfer theory without biological sinks:

$$\frac{dC(t)}{dt} = K_L a(u) \cdot \left[ C^*_\infty(T, P) - C(t) \right] - R_{bio}$$

Where:
* $C(t)$: Bulk liquid dissolved oxygen concentration ($\text{mg/L}$).
* $C^*_\infty(T, P)$: Equilibrium dissolved oxygen saturation concentration at ambient water temperature $T$ and barometric pressure $P$ ($\text{mg/L}$).
* $K_L a(u)$: Volumetric mass transfer coefficient ($\text{s}^{-1}$ or $\text{h}^{-1}$), directly modulated by the actuator control variable $u$ (aerator angular velocity $\omega$ or diffused air flow $Q_{air}$).
* $R_{bio} = 0$: In clean-water bench tests, biological respiration is strictly zero.

Under constant motor speed $u$, the analytical open-loop solution from an initial deoxygenated state $C(0) = C_0 \approx 0$ (achieved chemically via sodium sulfite $Na_2SO_3$ catalyzed by $CoCl_2$) is an exponential saturation curve:

$$C(t) = C^*_\infty - (C^*_\infty - C_0) e^{-K_L a(u) \cdot t}$$

The effective time constant of the water volume is:
$$\tau_{\text{plant}}(u) = \frac{1}{K_L a(u)}$$

Because $K_L a$ is a function of actuator speed ($K_L a \approx k_1 \cdot \omega^\gamma$, where $\gamma \approx 1.2\text{--}1.8$ for mechanical surface aerators), **the plant's time constant is input-dependent and non-linear**.

---

### 1.2 The Actuation Asymmetry & Irreversibility Boundary
In standard control theory, actuators provide bidirectional control authority ($u \in [-u_{\max}, +u_{\max}]$) or the plant has a passive restoring force toward zero ($R_{bio} > 0$).

On a clean-water bench:
1. Actuator authority is strictly non-negative: $u \in [0, u_{\max}]$.
2. When the actuator is commanded to zero speed ($u = 0$):
   $$\left.\frac{dC}{dt}\right|_{u=0} = K_L a_{\text{natural}} \cdot (C^*_\infty - C(t)) \ge 0 \quad \forall \; C(t) \le C^*_\infty$$
3. **The physical restoring derivative is non-existent**:
   $$\forall \; C(t) \le C^*_\infty, \quad \frac{dC}{dt} \ge 0$$
   The process variable **monotonically increases toward $C^*_\infty$**. It can never spontaneously decrease. Consequently, any overshoot past the setpoint ($C(t) > SP$) is an **irrecoverable terminal state failure**.

---

### 1.3 Sensor & Mixing Dynamics (Transfer Function Formulation)
The overall measurement channel is not instantaneous. It comprises two distinct lags:
1. **Hydrodynamic Transport & Mixing Delay ($\theta_{\text{mix}}$)**:
   The time required for micro-bubbles generated at the aerator to circulate and alter the bulk concentration at the probe location.
2. **Optical Sensor Lag ($\tau_{\text{sensor}}$)**:
   Modern optical luminescent DO probes (such as the Ponsel OPTOD) operate on dynamic luminescence quenching. Oxygen diffusion through the silicon sensing membrane is a first-order diffusion-limited process:
   $$G_{\text{sensor}}(s) = \frac{1}{\tau_s s + 1}, \quad \text{where } \tau_s = \frac{T_{90}}{\ln(10)} \approx 15\text{--}25\text{ s} \quad (T_{90} \approx 30\text{--}60\text{ s})$$

The complete linearized plant transfer function relating motor speed perturbation $\Delta U(s)$ to measured DO concentration $\Delta C_m(s)$ is a **Second-Order Plus Dead Time (SOPDT)** system:

$$G_p(s) = \frac{\Delta C_m(s)}{\Delta U(s)} = \frac{K_p e^{-\theta_{\text{mix}} s}}{(\tau_{\text{plant}} s + 1)(\tau_s s + 1)}$$

---

## 2. Theoretical Breakdown: Why Classical PID Fails in One-Way Plants

In classical linear control, a standard parallel PID controller calculates:

$$u(t) = K_p \left( e(t) + \frac{1}{T_i} \int_0^t e(\tau) d\tau + T_d \frac{de(t)}{dt} \right)$$

Where $e(t) = SP - C_m(t)$. During a large step change (e.g., from $0\text{ mg/L}$ to $5\text{ mg/L}$):

1. **Integral Accumulation (Windup)**:
   Because $\tau_{\text{plant}}$ is large, $e(t) > 0$ persists for several minutes. The integral term accumulates a massive value:
   $$I(t_{\text{cross}}) = \frac{K_p}{T_i} \int_0^{t_{\text{cross}}} e(\tau) d\tau \gg u_{\max}$$
2. **Phase Lag Overshoot**:
   At the exact crossing instant $t_{\text{cross}}$ where $C_m(t) = SP$, $e(t) = 0$ and proportional action drops to zero. However:
   $$u(t_{\text{cross}}) = I(t_{\text{cross}}) + \left. K_p T_d \frac{de}{dt} \right|_{t_{\text{cross}}}$$
   Even if derivative action opposes motion, the accumulated integral saturation holds $u(t)$ at a non-zero velocity.
3. **Latent Bubble Hold-Up & Sensor Unmasking**:
   Even if $u(t)$ were instantly forced to 0 at $t_{\text{cross}}$, the sensor lag $\tau_s$ and suspended micro-bubbles continue to drive the real concentration upward by:
   $$\Delta C_{\text{overshoot}} \approx \left. \frac{dC}{dt} \right|_{t_{\text{cross}}} \cdot (\theta_{\text{mix}} + \tau_s)$$
   Because $R_{bio} = 0$, this overshoot is permanent.

---

## 3. Advanced Control Strategies for Irreversible & Asymmetric Systems

### 3.1 Two-Degree-of-Freedom (2-DOF) PID with Setpoint Weighting
As formalized by **Åström & Hägglund (2006)**, a 2-DOF controller separates setpoint tracking from disturbance rejection:

$$u(t) = K_p \left( (b \cdot SP - C_m(t)) + \frac{1}{T_i} \int_0^t e(\tau) d\tau - T_d \frac{dC_m(t)}{dt} \right)$$

* **Setpoint Weighting ($b \in [0.2, 0.4]$)**: Softens the proportional kick, ensuring the controller does not aggressively accelerate toward a step setpoint.
* **Derivative on Measurement**: Eliminates "derivative kick" on setpoint changes while providing strong active braking as $\frac{dC_m}{dt} > 0$.
* **Conditional Integration (Clamping / Desaturation)**:
  $$\frac{dI(t)}{dt} = \begin{cases} \frac{K_p}{T_i} e(t) & \text{if } |e(t)| < \delta_{\text{linear}} \text{ and } u(t) \in [u_{\min}, u_{\max}] \\ 0 & \text{otherwise} \end{cases}$$
  The integrator is completely frozen while $C_m(t)$ is far from the setpoint, preventing windup during the rise phase.

---

### 3.2 Asymmetric / Switched Bang-Bang & Creep Control (Gravimetric Batching Approach)
Borrowed from precision chemical batching (where liquids cannot be drained back), this divides the trajectory into three operational regimes:

```
               [ Phase 1: Rapid Ascent ]
               u = RPM_MAX (e.g. 500 RPM)
                         │
                         ▼   e(t) <= Delta_brake  (e.g., 1.5 mg/L)
               [ Phase 2: Creep / Approach ]
               u = RPM_CREEP (e.g. 80-120 RPM)
               Reduces dC/dt to near zero
                         │
                         ▼   C_predicted(t) >= SP - Delta_deadband
               [ Phase 3: Coasting Cutoff ]
               u = 0 RPM
               Latent bubbles & sensor lag glide to setpoint
```

The predictive stopping condition uses the **Hamilton-Jacobi-Bellman deceleration boundary**:
$$C_{\text{pred}}(t) = C_m(t) + \left( \tau_s + \theta_{\text{mix}} \right) \cdot \frac{dC_m(t)}{dt}$$
When $C_{\text{pred}}(t) \ge SP$, actuator shutoff is triggered immediately.

---

### 3.3 Asymmetric Fuzzy Logic Control (FLC)
Fuzzy logic handles non-linearities without requiring an exact analytical model. By using an **asymmetric rule base**, rules can explicitly prohibit actuation when the error is closing from below:

| Error $e(t)$ | Derivative of Error $\dot{e}(t)$ | Physical Meaning | Commanded Speed $u$ |
|---|---|---|---|
| **Positive Large (PL)** | Negative (N / Improving) | DO very low, rising rapidly | **Medium (MED)** |
| **Positive Large (PL)** | Zero / Positive (Steady/Worse)| DO very low, not rising | **Maximum (MAX)** |
| **Positive Small (PS)** | Negative (N / Improving) | Approaching target rapidly | **OFF / IDLE (0 RPM)** |
| **Positive Small (PS)** | Zero (Steady) | Near target, stopped | **Low (CREEP)** |
| **Zero (Z)** | Any | At setpoint | **OFF (0 RPM)** |
| **Negative (N)** | Any | Overshoot condition | **OFF (0 RPM)** |

Notice the critical rule: **`IF e is PS AND de is N THEN u is OFF`**. This rule acts as an active predictive brake that cuts power before the setpoint is reached.

---

## 4. Seminal Literature & Academic References

### 4.1 Foundational Standards & Process Models
1. **ASCE/EWRI 2-22 (2022)** (Revising ASCE 2-06 and ASCE 2-91):
   * *Measurement of Oxygen Transfer in Clean Water*, American Society of Civil Engineers.
   * *Significance*: Establishes the non-steady state mass-transfer equations, nonlinear regression techniques for estimating $K_L a$, and chemical deoxygenation procedures with sodium sulfite.
2. **Olsson, G., Åmand, L., & Carlsson, B. (2013)**:
   * *Aeration control – a review*, **Water Science and Technology**, 67(11), 2374–2398. [DOI: 10.2166/wst.2013.139]
   * *Significance*: Comprehensive survey of aeration dynamics, cascade loops (DO $\to$ airflow/RPM), sensor time constants, non-linear mass transfer, and energy optimization.
3. **Olsson, G., & Andrews, J. F. (1978)**:
   * *The dissolved oxygen profile—a valuable tool for control of the activated sludge process*, **Water Research**, 12(11), 985–1004.
   * *Significance*: Seminal work introducing dynamic DO profiles as primary control variables in liquid bioreactors.

### 4.2 Control Theory: 2-DOF, Anti-Windup & Asymmetric Dynamics
4. **Åström, K. J., & Hägglund, T. (2006)**:
   * *Advanced PID Control*, **ISA (The Instrumentation, Systems, and Automation Society)**, Research Triangle Park, NC.
   * *Significance*: Chapters on Two-Degree-of-Freedom (2-DOF) controllers, setpoint weighting parameters ($b, c$), and integrator windup prevention techniques for slow systems.
5. **Åström, K. J., & Rundqwist, L. (1989)**:
   * *Integrator Windup and How to Avoid It*, **IEEE Control Systems Magazine**, 9(3), 40–45. [DOI: 10.1109/37.24836]
   * *Significance*: Formal mathematical analysis of actuator saturation, conditional integration, and back-calculation anti-windup circuits.
6. **Bohn, C., & Atherton, D. P. (1995)**:
   * *An analysis package comparing PID anti-windup strategies*, **IEEE Control Systems Magazine**, 15(2), 34–40.

### 4.3 Intelligent & Adaptive DO Control in Aquaculture & Water Systems
7. **Li, Z., et al. (2022)**:
   * *Modelling and controlling dissolved oxygen in recirculating aquaculture systems based on mechanism analysis and an adaptive PID controller*, **Computers and Electronics in Agriculture**, 198, 107094. [DOI: 10.1016/j.compag.2022.107094]
   * *Significance*: Formulates mechanistic mass-balance models in RAS and demonstrates how adaptive PID overcomes non-linear time delays and variable oxygen consumption.
8. **Sun, Y., et al. (2024)**:
   * *Advances in dissolved oxygen prediction and control methods in aquaculture: a review*, **Aquaculture International**, 32, 1145–1168.
   * *Significance*: Detailed state-of-the-art review comparing traditional PID, fuzzy-PID, neural network predictive controllers, and IoT platforms in aquaculture.
9. **Wang, C., et al. (2020)**:
   * *Intelligent Control System for Multivariable Regulation in Aquaculture based on Fuzzy-PID*, **Information Processing in Agriculture**, 7(2), 245–256.
   * *Significance*: Shows that fuzzy-adapted PID reduces overshoot by $>60\%$ compared to classical fixed-gain PID in slow aquatic environments.
10. **Han, H. G., Zhang, L., & Qiao, J. F. (2018)**:
    * *Adaptive fuzzy neural network control for dissolved oxygen in wastewater treatment processes with asymmetric constraints*, **IEEE Transactions on Cybernetics**, 48(6), 1845–1856.
    * *Significance*: Directly addresses asymmetric operational constraints and non-linear saturation in aeration basins.

### 4.4 Sensor Dynamics & Optical Probe Lag
11. **Bittig, H. C., et al. (2018)**:
    * *Oxygen Optode Sensors: Principle, Characterization, Calibration, and Application in the Ocean*, **Frontiers in Marine Science**, 4, 429. [DOI: 10.3389/fmars.2017.00429]
    * *Significance*: Detailed physical derivation of luminescence quenching time constants ($\tau$), membrane diffusion delays, and inverse filter deconvolution for dynamic DO measurements.
