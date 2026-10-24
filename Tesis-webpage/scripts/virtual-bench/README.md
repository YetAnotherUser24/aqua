# Banco de pruebas virtual (simulador del sistema completo)

Simula los 4 nodos del banco **y la planta** contra la API web real, sin hardware. Sirve para
comprobar que el sistema completo **responde al orquestador y trabaja coordinado** mientras el banco
físico está desconectado.

## Alcance: qué prueba y qué NO

**NO hay lazo cerrado.** El aireador recibe consignas; no las calcula contra la planta. El objetivo es
observar coordinación, no desempeño de control.

| Se valida | NO se valida |
|---|---|
| Que cada nodo responda al orquestador | Ganancias, estabilidad, sobreimpulso, tiempo de asentamiento |
| **Integridad por nodo**: consigna → comportamiento y telemetría en la forma del hardware real | $K$, $\tau_p$, $\theta_d$ de la planta real |
| **Flujo del sistema**: el aireador sube el OD, el sulfito lo baja, el sensor lo reporta | Radio real, RS-485 real, FOC del motor, hidrodinámica del tanque |
| Enrutado por rol, E-Stop, watchdog, ACK, ingesta y descarga | Cualquier resultado presentable en el Capítulo VII (regla 10) |

> ⚠️ **La planta sintética es de plausibilidad, no de identificación.** Sus parámetros de
> transferencia (`KLA_MAX_PER_S`, `SULFITE_REACTION_K`, boosts de mezcla) están **INVENTADOS** y
> etiquetados `UNIDENTIFIED` en `models/process.js`. Producción de este modelo **nunca** es evidencia
> experimental.

## Física real que sí se usa

Para que la simulación no sea arbitraria, lo que tiene base física se usa tal cual:

- **Solubilidad de saturación del OD** en agua dulce en función de la temperatura (ecuación APHA). A
  24 °C da **8.33 mg/L**, coherente con los 8.39 mg/L implícitos en las mediciones del banco
  (7.27 mg/L = 86.6 % sat a 24.08 °C).
- **Estequiometría del sulfito**: Na₂SO₃ + ½O₂ → Na₂SO₄, es decir **7.878 mg de sulfito por mg de
  O₂**. Sin catalizador de cobalto (prohibido por la regla 11).
- **Volumen activo del banco**: 87.5 L (regla 10).

## Modelos portados del firmware

| Archivo | Porta | Qué reproduce |
|---|---|---|
| `models/odrive.js` | `odrive_virtual.cpp` | Inercia, rampa, **carga hidrodinámica cuadrática**, caída del bus DC, corriente dinámica, potencia, torque, temperatura de FET, máquina de estados |
| `models/controlEngine.js` | `control_engine.cpp` | Orden exacto: e-stop → MANUAL → sin muestra → **watchdog 35 s** → PID/Fuzzy → saturación 0-600, con anti-windup |
| `models/odLogger.js` | `sensor_engine.cpp` + `measurement_storage.cpp` | Ciclo Modbus OPTOD, decodificación de float, plausibilidad, escalado a enteros y **modos de fallo** |
| `models/espnow.js` | `esp_now_packet.h` + `esp_now_rx/sender.cpp` | Trama real, `channel = 0` (auto-seguir al AP), latencia, pérdida y la **frontera de confianza de `status_flags`** |
| `models/masterClock.js` | ADR-3 | Reloj maestro local→UTC, fallback NTP, **desfase por nodo**, extrapolación sin RTC |
| `models/process.js` | *(no tiene homólogo en firmware)* | Planta de OD: aireación y consumo por sulfito |

**Fidelidad verificada**: `verify-pid-fidelity.js` compila el `control_engine.cpp` de **producción** en
el host y compara 1080 pasos contra el port en JS → diferencia máxima **7.3e-05 RPM** (solo redondeo de
float32). `verify-twin-constants.js` falla si un `config.h` deja de coincidir con el gemelo.

## Uso

```bash
cd Tesis_webpage

node scripts/virtual-bench/runner.js                        # 18 escenarios contra producción
node scripts/virtual-bench/runner.js --scenario nodeMotor   # uno concreto
node scripts/virtual-bench/runner.js --target local         # contra `npm run dev`
node scripts/virtual-bench/runner.js --time-scale 4         # acelera el tiempo de la planta
node scripts/virtual-bench/runner.js --initial-do 3         # OD inicial de la planta

# Fidelidad del PID contra el motor real en C++
node scripts/virtual-bench/verify-pid-fidelity.js
```

## Escenarios

### Integridad por nodo
| Escenario | Qué comprueba |
|---|---|
| `nodeMotor` | Al 25 % y 50 % responde ~150 y ~300 RPM, con **todos** los campos de telemetría y corriente no nula |
| `nodeMixer` | Gira dentro del tope del ESC (1000 RPM), registra `start_mixer` y se detiene |
| `nodeSensor` | El ítem bulk tiene la forma del hardware (`do_milli_mg_l` **entero**) y la saturación es coherente con la solubilidad real |

### Flujo del sistema
| Escenario | Qué comprueba |
|---|---|
| `systemFlowAeration` | Al girar el aireador el OD **sube**; parado, KLa = 0 |
| `systemFlowDeoxygenation` | El sulfito **baja** el OD y la agitación lo acelera (3.0×) |
| `coordinatedSequence` | Planta 1 desoxigena hasta agotar el residual y Planta 2 reoxigena: **el ciclo baja y sube** |

### Orquestación y seguridad
| Escenario | Qué comprueba |
|---|---|
| `planta1` / `planta2` | La receta correcta por planta: mixer ON en Planta 1, apagado en las de aireación |
| `pidArming` | Armado en PID y **signo** correcto de la ley de control |
| `sensorSampling` | El sensor se entera del experimento y muestrea (sin esto la curva sale vacía) |
| `manualOverride` | `MANUAL_OVERRIDE` deja los 4 nodos en OFF |
| `estop` / `estopPersistence` | E-Stop latchea, `clear_estop` lo libera y **armar una receta no lo libera** |
| `manualFailsafeGap` | **Caracteriza un hueco real**: en MANUAL el watchdog de 35 s NO actúa |
| `espnowTrust` | Con el sensor averiado el cero fantasma no entra al lazo y el watchdog frena |
| `ingestion` | La telemetría persiste en la BD (cuenta filas nuevas) |
| `channelAlignment` | Un canal ESP-NOW desalineado pierde el 100 % de los paquetes |
| `clockSkew` | Con 12 s de desfase el backend lo detecta y ancla al servidor |

Todos incluyen una **aserción transversal**: ningún nodo puede recibir acciones de otro rol.

## Flujo del sistema (lo que pediste ver)

```
Planta 1: mixer ON  -> se inyecta Na2SO3 -> el OD cae (8.00 -> 5.14 mg/L)
          se espera a que NO quede sulfito residual (la derivada se anula)
Planta 2: mixer OFF -> el aireador gira al escalón -> el OD sube (-> 5.64 mg/L)
          el sensor reporta toda la evolución en la forma del hardware real
```

La condición de "sin residual" no es un capricho del test: si queda reactivo sigue consumiendo
oxígeno más rápido de lo que el aireador aporta, y la fase de reoxigenación no arrancaría. Es el
mismo requisito que exige el protocolo experimental.

## Seguridad de datos

El banco escribe en la base de datos real, así que se protege por diseño:

1. **Se niega a correr** si el orquestador no está en `IDLE` (salvo `--force`).
2. **Snapshot de marcas** antes de empezar y borrado de **exactamente lo que creó**:
   `control_commands` por `created_at` restringido a los 4 nodos canónicos (su clave es UUID, no
   admite rango numérico); el resto por rango de `id`.
3. **Restaura el `system_state` previo** y **elimina los experimentos de prueba**, incluso si un
   escenario falla. Cada escenario sanea sensor, reloj y canal en un `finally`.
4. Reporta las filas creadas por tabla, para que la limpieza sea auditable.


## Qué es y qué no es

**Modelos portados del firmware**, no inventados:

| Archivo | Porta | Qué reproduce |
|---|---|---|
| `models/odrive.js` | `odrive_virtual.cpp` | Inercia, rampa, **carga hidrodinámica cuadrática**, caída del bus DC, corriente dinámica, potencia, torque, temperatura de FET, máquina de estados de armado |
| `models/controlEngine.js` | `control_engine.cpp` | Orden exacto de decisión: e-stop → MANUAL → sin muestra → **watchdog 35 s** → PID/Fuzzy → saturación 0-600, con anti-windup `I_max = max_rpm/Ki` |
| `models/odLogger.js` | `sensor_engine.cpp` + `measurement_storage.cpp` | Ciclo Modbus del OPTOD, decodificación de float, plausibilidad, escalado a enteros y **modos de fallo** (ceros + `STATUS_OD_SENSOR_BIT`) |
| `models/espnow.js` | `esp_now_packet.h` + `esp_now_rx/sender.cpp` | Trama real, `channel = 0` (auto-seguir al AP), latencia, pérdida y la **frontera de confianza de `status_flags`** |
| `models/masterClock.js` | ADR-3 | Reloj maestro local→UTC, fallback NTP, **desfase por nodo** y extrapolación del nodo sin RTC |

**Fidelidad verificada, no asumida:** `scripts/verify-pid-fidelity.js` compila el
`control_engine.cpp` de **producción** en el host y compara 1080 pasos contra el port en JS →
diferencia máxima **7.3e-05 RPM** (solo redondeo de float32). `scripts/verify-twin-constants.js`
falla si un `config.h` deja de coincidir con el gemelo, de modo que la deriva no puede pasar
inadvertida.

**Lo que NO valida** (solo se prueba en banco): capa física de radio (RSSI, modulación), RS-485 real,
FOC del motor y la hidrodinámica del tanque. Y por la regla 10 de `AGENTS.md`, los resultados del
Capítulo VII salen **exclusivamente del banco físico**: esto es una herramienta de desarrollo y
regresión, nunca una fuente de resultados.

## Uso

```bash
cd Tesis_webpage

# Todos los escenarios contra producción
node scripts/virtual-bench/runner.js

# Un escenario concreto
node scripts/virtual-bench/runner.js --scenario planta2

# Contra el dev server local
node scripts/virtual-bench/runner.js --target local

# Otros
--keep-data   # conserva las filas creadas (por defecto se limpian)
--force       # corre aunque el orquestador no esté en IDLE
--verbose     # trazas detalladas (comandos entrantes y resultado de cada push)
```

## Seguridad de datos

El banco escribe en la base de datos real, así que se protege por diseño:

1. **Se niega a correr** si el orquestador no está en `IDLE` (salvo `--force`), para no interferir
   con un ensayo real.
2. **Snapshot de marcas antes de empezar** y borrado de **exactamente lo que creó**:
   `control_commands` por `created_at` restringido a los 4 nodos canónicos (su clave es UUID, no
   admite rango numérico); el resto por rango de `id`.
3. **Restaura el `system_state` previo** y **elimina los experimentos de prueba** del registro,
   incluso si un escenario falla. Cada escenario además sanea sensor y reloj en un `finally`, para
   que un fallo no provoque fallos en cascada.
4. Informa de las tablas ausentes, que hoy son todas las de archivo de alta fidelidad mientras no se
   aplique `supabase/migrations/20261008_separate_actuator_roles.sql`.

## Escenarios

| Escenario | Qué comprueba |
|---|---|
| `planta1` | Planta 1: `mixer='on'` girando y aireador en `motor_mode='off'` |
| `planta2` | Planta 2: escalón manual al 40 %, mixer detenido y **aireador girando de verdad** |
| `closedloop` | Caso B: PID con setpoint 5.0 y **el PID demanda RPM** con OD bajo |
| `sensorSampling` | El sensor recibe `set_state ACTIVE_EXPERIMENT` (sin esto la curva sale vacía) |
| `manualOverride` | `MANUAL_OVERRIDE` deja los 4 nodos en OFF |
| `estop` | E-Stop latchea a 0 RPM y `clear_estop` lo libera |
| `manualFailsafeGap` | **Caracteriza un hueco real**: en MANUAL el watchdog de 35 s NO actúa |
| `espnowTrust` | Con el sensor averiado, el cero fantasma NO entra al lazo y el watchdog frena |
| `ingestion` | La telemetría persiste en la BD (cuenta filas nuevas) |
| `channelAlignment` | Un canal ESP-NOW desalineado pierde el 100 % de los paquetes |
| `clockSkew` | Con 12 s de desfase el backend lo detecta y ancla al servidor |

Todos los escenarios incluyen además una **aserción transversal**: ningún nodo puede recibir
acciones de otro rol (entrega cruzada).

## Fidelidad de los nodos

- **Cadencia real**: sensor 0.2 Hz, aireador 5 Hz de muestreo y lote cada 5 s, heartbeat de 20 s en
  IDLE, polling de comandos cada 1.5 s (1 s en MANUAL).
- **Endpoints y campos idénticos** al firmware (`buildSampleObject`, `sendTelemetryBatch`,
  telemetría del T-200 y `postPumpEvent`).
- **ACK con `rtc_timestamp_ms`** del instante de ejecución, para el anclaje por latencia.
- El aireador **no calcula la planta**: ejecuta el PID real contra la lectura que le llega por
  ESP-NOW. La dinámica del tanque no se simula.
