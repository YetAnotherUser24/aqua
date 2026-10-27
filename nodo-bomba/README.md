# Nodo bomba — Dosificación peristáltica y control de velocidad (`nodo-bomba`)

> **Repositorio:** [github.com/YetAnotherUser24/nodo-bomba](https://github.com/YetAnotherUser24/nodo-bomba)

Controlador de velocidad angular en lazo cerrado y dosificador volumétrico de alta precisión para la bomba peristáltica de la **Planta 1** (Desoxigenación química ASCE/EWRI 2-22). Inyecta la disolución de sulfito de sodio ($Na_2SO_3$, $100\text{ g/L}$) gobernado por un **ESP32-S3**, puente H **TB6612FNG** (12V) y encoder magnético absoluto **AS5600** (12-bit I2C).

---

## 1. System Overview

```
+-----------------------------------------------------------------------------------------+
|                                    SYSTEM ARCHITECTURE                                  |
|                                                                                         |
|  [ ESP32-S3 Controller ]                                       [ TB6612FNG H-Bridge ]   |
|  - 100 Hz Discrete PI Controller (rad/s)                        - 12V High-Current Rail |
|  - Continuous Tanh Deadband Compensator                         - Standby Enable (STBY) |
|  - Virtual Flow Estimator Q = f(ω)                              - Direction Logic (AIN) |
|  - 20 kHz LEDC PWM (GPIO 4) ----------------------------------->  - PWMA Speed Input    |
|  - AS5600 I2C Driver (SDA: 9, SCL: 8) <--- [12-bit Hall] <----  - Motor Gearbox Output  |
|                                                                          |              |
|                                                                 [ Peristaltic Head ]    |
|                                                                 - Volumetric Dosing     |
+-----------------------------------------------------------------------------------------+
```

### Key Engineering Features
1. **Actuation (H-Bridge TB6612FNG @ 20 kHz Ultrasonic PWM)**:
   - Hardware **20 kHz LEDC PWM** con resolución de **10 bits** (1,024 pasos), eliminando el silbido audible del motor de 12V.
   - Control bidireccional: avance directo para dosificación volumétrica y reversa controlada (*suck-back*) para retraer el menisco 2–5 mm y evitar el goteo y cristalización en la boquilla.
2. **Estimador de Velocidad y Compensador de Zona Muerta (Stiction)**:
   - Lazo a **100 Hz** en Core 1 que lee el ángulo absoluto del **AS5600** (4096 cuentas/vuelta).
   - Compensador analítico continuo con aproximación de Padé para $\tanh(u / 0.03)$ que vence la fricción estática del pellizco del tubo de silicona sin producir chattering ni ciclos límite.
3. **Control PI Puro (Sin Derivada)**:
   - Sintonización estricta PI ($K_d = 0.0$) para cumplir con los invariantes de hardware: evita la amplificación de ruido producida por los pulsos discretos del encoder magnético y los rodillos de la bomba.
4. **Sensor Virtual de Caudal ($Q = \omega \cdot C_{\text{vol}}$)**:
   - Integra revoluciones del cabezal para derivar caudal en tiempo real ($\text{mL/min}$) y volumen acumulado ($\text{mL}$) con precisión gravimétrica ($R^2 = 1.0000$).
5. **Consola CLI Interactiva (USB CDC)**:
   - Mismo nivel de ergonomía y comandos que el nodo mezclador (`SPEED`, `RPM`, `<number>`, `DUTY`, `DOSE`, `STOP`, `TUNE`, `STATUS`, `SYSID`, `HELP`).

---

## 2. Pinout & Hardware Connections

| Pin ESP32-S3 | Señal / Función | Conexión en Banco | Notas |
| :---: | :---: | :---: | :--- |
| **GPIO 4** | `PWMA` | Pin `PWMA` del TB6612FNG | PWM LEDC canal 0 @ 20 kHz |
| **GPIO 6** | `AIN1` | Pin `AIN1` del TB6612FNG | Sentido de giro |
| **GPIO 5** | `AIN2` | Pin `AIN2` del TB6612FNG | Sentido de giro |
| **GPIO 7** | `STBY` | Pin `STBY` del TB6612FNG | HIGH = Habilitado, LOW = Standby |
| **GPIO 9** | `I2C SDA` | Pin `SDA` del AS5600 | Bus I2C de retroalimentación de ángulo |
| **GPIO 8** | `I2C SCL` | Pin `SCL` del AS5600 | Bus I2C @ 400 kHz |
| **GND** | `GND` | Común GND | Tierra lógica compartida |
| **+3.3V** | `VCC` | VCC lógica AS5600 / TB6612 | Alimentación lógica |
| **+12V** | `VM` | Bus 12V del banco | Alimentación de potencia motor |

---

## 3. Mathematical Formulation & Plant Model

### A. Modelo FOPDT de la Planta
La dinámica angular del motor con cabezal peristáltico se modela como un sistema continuo de primer orden con retardo aparente:

$$G(s) = \frac{K}{\tau s + 1} e^{-\theta s}$$

- **$K$ (Ganancia estática):** $\approx 12.64\ \frac{\text{rad/s}}{\text{duty}}$ (a través de reductora) o $\approx 338\ \frac{\text{rad/s}}{\text{duty}}$ (eje motor).
- **$\tau$ (Constante de tiempo):** $\approx 0.14\text{--}0.19\text{ s}$ (inercia mecánica y viscosidad de deformación del tubo).
- **$\theta$ (Retardo aparente):** $\approx 10\text{ ms}$ (tiempo de procesamiento y muestreo I2C).

### B. Sintonización Analítica SIMC (Skogestad IMC)
Para garantizar **cero sobrepico** ($M_p = 0.0\%$) conforme al estándar ASCE/EWRI 2-22 en clean-water:

$$K_p = \frac{1}{K} \frac{\tau}{\tau_c + \theta}, \qquad T_i = \min(\tau, 4(\tau_c + \theta)), \qquad K_i = \frac{K_p}{T_i}$$

Con $\tau_c = \max(\tau, \theta) = \tau$, la ganancia resulta críticamente amortiguada, rechazando perturbaciones periódicas de los rodillos sin excitar sobrepico.

---

## 4. Serial Command Reference (CLI Interactiva)

Al conectar el monitor serie a **115200 baud**, el firmware responde con el mismo esquema de comandos del nodo mezclador:

| Comando | Ejemplo | Descripción |
| :--- | :--- | :--- |
| `SPEED <rad/s>` | `SPEED 6.28` | Establece velocidad angular en unidades canónicas SI ($\text{rad/s}$) |
| `RPM <rpm>` | `RPM 60.0` | Establece velocidad en revoluciones por minuto mecánicas |
| `<number>` | `60` | **Atajo idéntico al mezclador:** escribir un número fija los RPM directamente |
| `DUTY <-1.0..1.0>` | `DUTY 0.25` | Aceleración manual en lazo abierto ($25\%$ hacia adelante; negativo = reversa) |
| `DOSE <ml> [rpm]` | `DOSE 25.0 60` | Dosifica el volumen especificado con frenado activo y suck-back automático |
| `SUCKBACK [rev] [rpm]` | `SUCKBACK 0.35 35` | Dispara retracción de menisco para evitar goteo (default 0.35 rev) |
| `REVERSE [rpm]` | `REVERSE 40` | Gira en reversa continua para purgar manguera hacia el frasco |
| `STOP` | `STOP` | Detiene inmediatamente el motor activando freno corto (*Active Short Brake*) |
| `TUNE <Kp> <Ki> [Kd]` | `TUNE 0.0056 0.0314` | Actualiza las ganancias PI en caliente sin re-flashear |
| `CALIB <ml_per_rev>` | `CALIB 0.9200` | Fija el factor volumétrico de calibración gravimétrica |
| `STATUS` | `STATUS` | Imprime diagnóstico completo de encoder, imán, AGC, caudal y lazo PI |
| `SYSID ON / OFF` | `SYSID ON` | Activa/desactiva telemetría CSV a 50 Hz para identificación |
| `DEBUG ON / OFF` | `DEBUG ON` | Activa/desactiva telemetría periódica a 2 Hz en el monitor serie |
| `HELP` | `HELP` | Muestra el menú de ayuda con comandos |

---

## 5. Pipeline Automatizado de Sintonización y Modelado (`pipeline.py`)

El archivo [`pipeline.py`](pipeline.py) automatiza el flujo completo de ingeniería experimental de la misma forma que en el nodo mezclador:

```bash
# 1. Flujo completo automatizado (Adquisición -> Identificación -> Validación -> Gráficos)
python pipeline.py full --method stair --rpm 60

# 2. Identificación simultánea y comparación de todos los métodos (Python SIMC/ITAE, MATLAB PSO, pidtune)
python pipeline.py ident-all

# 3. Comparación y simulación teórica en lazo cerrado (sin hardware)
python pipeline.py compare --rpm 1000

# 4. Validación en lazo cerrado en hardware físico
python pipeline.py run --rpm 60

# 5. Generación de todas las figuras de publicación
python pipeline.py plot
```

### Gráficos Generados Automáticamente (Estilo Publicación)
Todos los resultados se exportan a `scripts/results/figures/`:
1. `closed_loop_simulation_comparison.png`: Comparación teórica simultánea en lazo cerrado de todos los controladores sintonizados (Python SIMC, Python ITAE, MATLAB PIDTUNE, MATLAB PSO Custom y Pure ITAE) con respuesta de velocidad (RPM) y esfuerzo de control (Duty %).
2. `closed_loop_experimental_comparison.png`: Comparación experimental superpuesta de todas las corridas en hardware real (Python SIMC vs MATLAB PSO vs MATLAB PIDTUNE).
3. `sysid_identification.png`: 3 paneles (Entrada de Duty %, Velocidad Medida vs Modelo FOPDT continuo con $R^2$ y RMSE, y Residual de error $e(t)$).
4. `closed_loop_validation.png`: 3 paneles (Velocidad RPM con banda de tolerancia $\pm 2\%$, tiempo de subida, tiempo de asentamiento y sobrepico; Esfuerzo de control Duty %; Caudal y Volumen acumulado).
5. `pso/closed_loop_validation.png` y `pidtune/closed_loop_validation.png`: Comparación de algoritmos PSO y pidtune.
6. `flow_calibration_curve.png`: Regresión gravimétrica Caudal vs RPM con bandas de confianza al $95\%$.
7. `dosing_cycle_profile.png`: Perfil completo de inyección con aceleración, dosificación y retracción *suck-back*.

---

## 6. Compilación y Despliegue

```bash
# Entorno real de producción (ESP32-S3)
pio run -d nodo-bomba -e esp32-s3
pio run -d nodo-bomba -e esp32-s3 -t upload

# Simulación en banco (sin bomba ni encoder físicos)
pio run -d nodo-bomba -e esp32-s3-bench-sim -t upload

# Actualización inalámbrica OTA (WiFi de banco)
pio run -d nodo-bomba -e esp32-s3-ota -t upload
```
