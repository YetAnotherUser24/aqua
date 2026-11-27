# Criterios de repetibilidad — serie con base certificada (fijados el 2026-11-27, antes de la serie)

Este archivo se versiona **antes** de las corridas de la serie para que los criterios no puedan
ajustarse a posteriori. Código de la serie: receta `auto_benchmark_control_base`, tag `tesis-2026-11-27c`
(revisiones del mismo día, antes de la primera corrida: fin del lazo por estabilización, ventana de KPIs de 180 s,
receta 100 % automática y dosis complementaria si la desoxigenación se estanca sobre 0.7 mg/L).

## Condiciones de repetibilidad (VIM 2.20)
Mismo procedimiento (receta y huella `recipe_sha256_16` iguales), mismo banco (87.5 L, 25 cm, mezclador a
1000 rpm), mismo operador y lugar, período corto (27–28-nov) y **un solo lote de solución de Na₂SO₃**
(100 g en 1 L, preparada fresca, frasco cerrado, agitada antes de cada dosis). Dosis: 80 mL.
PI 2-DOF fijo: Kp 221.65, Ki 1.915, β 0.35, Tt 100, τ_lag 30. SP = 5 mg/L. **Sin cierre manual**: el lazo termina
solo al estabilizarse (t ≥ 180 s, |OD − SP| ≤ 0.30 mg/L, |pendiente de 60 s| ≤ 0.02 mg/L/min; tope 600 s). Tras el pico
el aerador satura en 0 rpm y lo que sigue es deriva de la planta, sin información del lazo.

Se registra por corrida: hora de preparación y edad de la solución, OD antes de la dosis
(objetivo ≥ 6.3 mg/L; airear ≈ 5 min a 360 rpm entre corridas), número de corridas acumuladas en el agua,
temperatura, intervenciones manuales (meta 0).

## Nivel N2 — condición inicial conforme (evento `initial_condition`, `conforming = true`)
- `do_in_band`: OD al armar ∈ [1.00, 1.10] mg/L
- `slope_ok`: pendiente lineal de 60 s ≥ 0.06 mg/L/min
- `curvature_ok`: curvatura (ajuste cuadrático de 120 s) ≥ −0.02 mg/L/min²
- `aerator_at_rest`: |rpm| ≤ 5

Se reportan además C′ = y′ + τ·y″ (τ = 18 s) y la demanda residual r = k_s(C* − C) − C′
(k_s = 1.24 h⁻¹, C* = 6.5 mg/L) como diagnóstico, y la tasa de conformidad k/n.

## Nivel N4 — repetibilidad del lazo (solo corridas conformes, ventana fija de 180 s desde el armado)
Se acepta la repetibilidad del ensayo de lazo si, con **n ≥ 3** corridas conformes:
- CV de t₁₀₋₉₀ ≤ 10 %
- CV de IAE/salto ≤ 10 %
- CV de energía/salto ≤ 10 %
- DE del pico ≤ 0.10 mg/L
- Se informa también el tiempo hasta la estabilización (fin automático del lazo).

Se reporta media ± DE, CV, rango e IC95 (t de Student, n pequeño). Las corridas no conformes no se
descartan: se informan con su motivo y forman el grupo de contraste (C₀ libre).

## Inclusión de EXP-2026-11-26-03
Corrida con el código `tesis-2026-11-26c` y un lote anterior de solución. Se incluye si cumple los criterios
de N2 al reevaluarla offline con los datos guardados (al armar: OD 1.003, y′ 0.085, y″ +0.021 → cumple).
Se informa por separado de la serie del lote fresco.

## Validación previa con el gemelo digital (`aqua-station/gateway/tests/sim_bench.py`)
Planta calibrada con 26-02 y 26-03 completas (η = 0.65 / 0.79, igual que el balance de O₂; misma cinética).
Barrido de 288 corridas (η 0.55–1.15, cinética ×0.7–1.4, orden 0–0.3, OD inicial 5.0–6.5, ruido):
todas terminan sin intervención; con la cinética observada o más rápida, 191/192 conformes,
mediana 40 min por corrida. Límite conocido: con una reacción ≥ 30 % más lenta que la medida, el OD cruza
la banda antes de certificar (C₀ 1.13–1.31) y la corrida queda marcada no conforme.

## Enmienda (2026-11-27, 18:30, entre la corrida 2 y la 3; tag `tesis-2026-11-27d`)
No cambia ningún criterio N2/N4 ni la ventana de KPIs (180 s). Cambia el procedimiento:
- **Pre-aireación automática** en la preparación: aerador a 360 rpm hasta OD ≥ 6.2 mg/L sostenido 60 s
  (tope 420 s); la dosis detiene el aerador antes de dosificar. Fija el OD de partida (las corridas 1 y 2
  partieron de 6.7 y 5.1) y con él el sulfito sobrante.
- **Fin del lazo más exigente:** banda asimétrica (SP − 0.10 / SP + 0.30: bajo SP el controlador aún
  debe actuar; sobre SP el aerador no puede bajar un sobreimpulso) y estabilidad sostenida 120 s (antes 60 s).
  Los lazos terminan a ≈ 4.5 min (antes ≈ 3.4).
