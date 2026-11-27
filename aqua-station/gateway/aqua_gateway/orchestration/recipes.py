"""
Recipe definitions: the phase plans a run walks through.

Ported from the mockup's ``PROTOCOLS`` object, but as **data**, not logic. A recipe is a
list of phases; a phase is a title, a set of parameter defaults, the command to issue on
entry, and the physical condition that ends it. Nothing here branches — the engine
interprets this structure, so adding a fourth protocol is an edit to this file and nothing
else.

Every phase carries a ``timeout_s`` except the supervised ones. A phase whose condition
never becomes true -- a sensor that dies mid-run, a step the operator never takes -- would
otherwise hold the run open forever, so an ``auto`` phase must declare one and
``validate_recipes`` enforces it at import time. A phase that waits for a *human* may set it
to ``None``: there is no clock to run out, because the whole point of ``manual`` and
``hibrido`` is that the operator decides. ``None`` is a deliberate statement ("no valve
here"), which is why it is spelled out rather than left as a number nothing reads.

Stopping a run that is holding on a human is the operator's job, and ``pause`` / ``IDLE``
already do it -- no timeout is needed to rescue an unattended bench, only attention.

Commands are *abstract setpoints*, never per-sample values. The node's own firmware loop
satisfies them: ``set_speed {speed_percent: 40}`` asks the ODrive node to reach 240 RPM and
hold it, and the node decides how. That split is the whole point of the design — the gateway
sequences, the nodes control.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

#: Command issued on ENTERING an experiment-bound phase, keyed by role.
#: An empty tuple means "this phase issues nothing" (a pure wait/verify phase).
PhaseCommand = tuple[str, str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class Phase:
    """One step of a protocol."""

    phase_id: str
    num: str
    title: str
    #: Operator-facing objective of the phase — what it is for, in the mockup's words. Shown
    #: in the phase card body so the run is legible without reading the source.
    desc: str = ""
    #: Default mode: ``auto`` | ``hibrido`` | ``manual``. The operator may override per run.
    mode: str = "auto"
    #: Human-readable exit condition, shown in the UI. The machine-checked form is
    #: ``condition`` below; this is the text beside it.
    condition_text: str = ""
    #: Structured predicate. ``{"kind": ..., **params}`` — see ``conditions.py``.
    condition: Mapping[str, Any] = field(default_factory=dict)
    #: Hard ceiling for an ``auto`` phase, or ``None`` for a supervised one that waits for the
    #: operator. Required for ``auto`` -- that mode advances on its own, so without a ceiling a
    #: dead sensor holds the run open forever. Optional for ``manual``/``hibrido``, where the
    #: human is the clock: a number there would be a guarantee that does not exist.
    #:
    #: ``None`` does *not* mean "use the default"; it means "there is no valve". An operator
    #: stops such a run with ``pause`` or ``IDLE``.
    timeout_s: int | None = 900
    #: Commands issued on entry, in order.
    commands: tuple[PhaseCommand, ...] = ()
    #: Parameter defaults surfaced in the UI (names are free-form, used only for display).
    params: Mapping[str, Any] = field(default_factory=dict)
    #: What to do if timeout_s expires before exit condition is satisfied:
    #: "advance" (default: mark phase completed and advance to next)
    #: "abort" (abort experiment immediately to prevent invalid downstream phases)
    #: "pause" (pause experiment and alert operator)
    on_timeout: str = "advance"


@dataclass(frozen=True, slots=True)
class Recipe:
    """A complete protocol."""

    recipe_id: str
    title: str
    filename: str
    #: Roles that must be online for the run to be meaningful.
    roles: tuple[str, ...]
    phases: tuple[Phase, ...]


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

_PLANTA_1_DEOX = Recipe(
    recipe_id="planta_1_deox",
    title="Protocolo A — Planta 1: Desoxigenación Química (Na₂SO₃)",
    filename="EXP_01_DEOX.CSV",
    roles=("od_sensor", "mixer", "pump"),
    phases=(
        Phase(
            phase_id="p1_prep",
            num="1",
            title="Homogenización del Estanque (Mezcla Previa)",
            desc=(
                "<strong>Objetivo:</strong> Homogeniza los 87.5 L de agua para eliminar la "
                "estratificación térmica y de OD y asegurar flujo laminar sobre la membrana del "
                "sensor Aqualabo OPTOD antes de dosificar reactivos."
            ),
            mode="auto",
            condition_text="DO estabilizado (|d[DO]/dt| < 0.05 mg/L/min) durante ≥ 30 s",
            condition={"kind": "do_stable", "eps_mg_l_per_min": 0.05, "window_s": 30},
            timeout_s=300,
            commands=(
                ("mixer", "start_mixer", {"target_rpm": 1000}),
                ("aerator", "stop", {}),
            ),
            params={"mixer_rpm": 1000, "warm_time_s": 60},
        ),
        Phase(
            phase_id="p1_dose",
            num="2",
            title="Dosificación Na₂SO₃ (Bomba Peristáltica)",
            desc=(
                "<strong>Reacción química:</strong> 2 Na₂SO₃ + O₂ → 2 Na₂SO₄ (7.878 mg Na₂SO₃ "
                "por mg O₂). El volumen de solución madre (100 g/L, ρ = 1.10 g/mL) se calcula a partir de "
                "los gramos de sulfito deseados (3.80 g Na₂SO₃ = 38.0 mL) para llevar el estanque al DO piso objetivo."
            ),
            mode="hibrido",
            condition_text="Masa dispensada = 3.80 g Na₂SO₃ (38.0 mL ± 0.2 mL) registrado por encoder",
            condition={"kind": "dose_complete", "target_ml": 38.0, "tol_ml": 0.2},
            # No timeout: this phase waits for the operator. `None` is deliberate -- a number
            # here would look like a guarantee the engine no longer honours.
            timeout_s=None,
            commands=(("pump", "dose", {"target_ml": 38.0, "sulfite_g": 3.80}),),
            # 38.0 mL (3.80 g Na2SO3 puro), not the 12.5 mL this carried before. The dose has to satisfy the *next*
            # phase, which waits for `DO < 3.0` (p1_deox, do_floor_mg_l 3.0), and the arithmetic
            # never worked:
            #
            #   stock 100 g/L -> 100 mg Na2SO3 per mL (0.100 g/mL)
            #   solución: 1000 mL agua + 100 g sulfito, densidad ρ = 1.10 g/mL
            #   2 Na2SO3 + O2 -> 2 Na2SO4, so 7.878 mg Na2SO3 per mg O2
            #   active volume 87.5 L
            #   => 1 mL removes 100 / 87.5 / 7.878 = 0.1450 mg/L
            #
            # 12.5 mL therefore removed 1.81 mg/L: from saturation (~8.3 at 24 degC) the tank
            # landed near 6.5 and p1_deox could never fire, so the phase timed out at 600 s on
            # every run and p1_end followed without the deep deox it exists to produce. The
            # phase's own note says the volume "se calcula para llevar el estanque al DO piso
            # objetivo" -- it was simply never calculated.
            #
            # The tank starts somewhere between ~7.0 (respiration endpoint) and ~8.3
            # (saturation), so the dose has to be sized for that whole band:
            #
            #   from 8.3, reaching 3.0 needs 5.30 mg/L = 36.5 mL  <- the binding case
            #   from 7.0, stopping near 1.0 needs 6.0 mg/L  = 41.4 mL  <- the ceiling
            #
            # 38.0 mL (5.51 mg/L) sits inside that window: it crosses below 3.0 from
            # saturation, and from a 7.0 start it lands near 1.5 rather than draining the
            # tank. Both ends keep a usable margin, which a value chosen for one starting
            # point cannot. Sized this way because undershooting makes the phase unreachable
            # -- which is what silently happened -- while overshooting only wastes reagent.
            params={"sulfite_g": 3.80, "dose_ml": 38.0, "stock_conc_gpl": 100, "density_g_per_ml": 1.10},
        ),
        Phase(
            phase_id="p1_deox",
            num="3",
            title="Desoxigenación Química Activa",
            desc=(
                "<strong>Detección de fin de sulfito:</strong> el sulfito se agota cuando la "
                "caída cesa y la pendiente se anula (|d[DO]/dt| &lt; 0.15 mg/L/min, el fondo de "
                "respiración) y se cumple el tiempo cinético de reacción (&gt; 180 s con cobalto)."
            ),
            mode="auto",
            condition_text="DO < 3.0 mg/L Y estabilizado (ΔDO ≤ 0.04 mg/L en 60 s) Y t > 180 s",
            condition={
                "kind": "do_below_and_stable",
                "below_mg_l": 3.0,
                "window_s": 60,
                "max_delta_mg_l": 0.04,
                # 0.15, not the 0.02 this carried before. The two halves of this phase's
                # condition have to be satisfiable at the same time, and with the constants in
                # the plant model they were not:
                #
                #   respiration alone drives dDO/dt to -0.0012 mg/L/s = -0.072 mg/L/min
                #
                # so a tank with no sulfite left *still* falls 3.6x faster than the old 0.02
                # threshold allowed. The criterion was below the model's own noise floor: it
                # could never read as "flat", so `p1_deox` burned its 600 s timeout on every
                # run regardless of the dose.
                #
                # What the protocol actually needs to separate is "the sulfite reaction is
                # over" from "the sulfite reaction is still running". Respiration is the floor
                # between them (-0.072 mg/L/min here); a live dose falls far faster. 0.15 sits
                # above that floor with ~2x margin and far below an active reaction, so it
                # still distinguishes the two states rather than accepting anything.
                "eps_mg_l_per_min": 0.15,
                "min_elapsed_s": 180,
            },
            timeout_s=1800,
            # Stop the mixer for the measurement. The impeller is a re-oxygenation surface, so
            # with it running the tank's equilibrium is `Cs - respiration/kla` =
            # 8.3 - 0.0012/(0.25*0.0035) ~= 6.9 mg/L -- above the 3.0 floor, and the derivative
            # only flattens approaching it. Mixing is what makes the sulfite react, which is why
            # it runs through p1_prep and the dose; once the reagent is spent it is only a source
            # of oxygen, and the protocol asks for the point where "la caída cesa" -- the end of
            # the *reaction*, measured without the re-oxygenation the impeller adds.
            # Sulfite reaction consumes dissolved oxygen. Mixing distributes the chemical.
            commands=(),
            params={
                "do_floor_mg_l": 3.0,
                "max_delta_mg_l": 0.04,
                "window_s": 60,
                "safety_timeout_s": 1800,
                "do_alert": True,
            },
        ),
        Phase(
            phase_id="p1_end",
            num="4",
            title="Fin de Desoxigenación / Reposo",
            desc=(
                "<strong>Cierre del Protocolo A:</strong> desarma la bomba peristáltica y deja el "
                "estanque en estado basal desoxigenado listo para el Protocolo B (escalón de "
                "reoxigenación)."
            ),
            mode="auto",
            condition_text="Bomba en reposo — listo para transicionar a Planta 2",
            condition={"kind": "elapsed", "seconds": 5},
            timeout_s=30,
            commands=(("pump", "force_off", {}),),
            params={},
        ),
    ),
)

_PLANTA_2_STEP = Recipe(
    recipe_id="planta_2_step",
    title="Protocolo B — Planta 2: Escalón de Aireación",
    filename="EXP_02_STEP.CSV",
    roles=("od_sensor", "aerator", "mixer"),
    phases=(
        Phase(
            phase_id="p2_precond",
            num="1",
            title="Verificación de Condiciones Iniciales (C₀)",
            desc=(
                "<strong>Objetivo:</strong> confirma que el estanque esté desoxigenado "
                "(DO inicial C₀ &lt; 3.5 mg/L) y el fluido estabilizado para fijar el punto "
                "inicial (t₀, C₀) antes de aplicar el escalón."
            ),
            mode="hibrido",
            condition_text="DO inicial < 3.5 mg/L Y ODrive en 0 RPM durante ≥ 60 s",
            condition={"kind": "do_below", "threshold_mg_l": 3.5, "min_elapsed_s": 60},
            # No timeout: this phase waits for the operator. `None` is deliberate -- a number
            # here would look like a guarantee the engine no longer honours.
            timeout_s=None,
            commands=(
                ("aerator", "stop", {}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"do_start_max_mg_l": 3.5, "settle_time_s": 60, "mixer_enable": True, "mixer_rpm": 1000},
        ),
        Phase(
            phase_id="p2_step",
            num="2",
            title="Escalón de Aireación (Marca t₀)",
            desc=(
                "<strong>Marca automática t₀:</strong> guarda el timestamp RTC en el instante "
                "exacto en que el motor arranca, anclando el origen temporal de la curva "
                "DO(t) del ensayo."
            ),
            mode="hibrido",
            condition_text="ODrive alcanza consigna (240 RPM)",
            condition={"kind": "setpoint_reached", "role": "aerator", "field": "drv_rpm", "target": 240, "tol": 30},
            # No timeout: this phase waits for the operator. `None` is deliberate -- a number
            # here would look like a guarantee the engine no longer honours.
            timeout_s=None,
            commands=(
                ("aerator", "set_speed", {"speed_percent": 40, "target_rpm": 240, "step_rpm": 240}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"step_rpm": 240, "mixer_enable": True, "mixer_rpm": 1000},
        ),
        Phase(
            phase_id="p2_acq",
            num="3",
            title="Adquisición de Curva de Oxigenación DO(t)",
            desc=(
                "<strong>Criterio de parada:</strong> finaliza al alcanzar el 80–85% de "
                "saturación C* (~6.0 mg/L), evitando el tramo final asintótico donde d[DO]/dt "
                "es muy bajo y sensible al ruido."
            ),
            mode="auto",
            condition_text="DO ≥ 6.00 mg/L ó t - t₀ ≥ 900 s",
            condition={"kind": "do_above", "threshold_mg_l": 6.00},
            timeout_s=900,
            commands=(),
            params={"stop_do_mg_l": 6.00, "safety_timeout_s": 900},
        ),
        Phase(
            phase_id="p2_analysis",
            num="4",
            title="Cierre y Verificación de Curva",
            desc=(
                "<strong>Verificación:</strong> el operador revisa la curva registrada, confirma "
                "que no hubo huecos de datos y archiva el reporte del ensayo."
            ),
            mode="manual",
            condition_text="Operador valida la curva y archiva el reporte",
            condition={"kind": "elapsed", "seconds": 5},
            # No timeout: this phase waits for the operator. `None` is deliberate -- a number
            # here would look like a guarantee the engine no longer honours.
            timeout_s=None,
            commands=(
                ("aerator", "stop", {}),
                ("mixer", "stop_mixer", {}),
            ),
            params={},
        ),
    ),
)

_CLOSED_LOOP = Recipe(
    recipe_id="closed_loop",
    title="Protocolo C: Control en Lazo Cerrado (PID / On-Off)",
    filename="EXP_03_PID_LOOP.CSV",
    # This recipe regulates toward a DO setpoint, so it needs the tank *below* that setpoint
    # for the loop to have anything to do. From saturation the loop correctly commands 0 RPM
    # (there is no oxygen deficit to correct) and `cl_run`'s 600 s window expires first --
    # respiration alone takes ~19 min to fall from 7.4 to the 6.0 setpoint. It is the third
    # step of a sequence, not a standalone run: Protocolo A ends "en estado basal
    # desoxigenado" and leaves DO near 1.7, at which point the loop opens at ~258 RPM. The
    # phases say so in prose but nothing enforces the ordering, so running this on a fresh
    # tank looks exactly like a broken controller.
    roles=("od_sensor", "aerator"),
    phases=(
        Phase(
            phase_id="cl_config",
            num="1",
            title="Configuración del Lazo de Control",
            mode="manual",
            condition_text="Operador valida ganancias y confirma armado del lazo",
            condition={"kind": "elapsed", "seconds": 5},
            # No timeout: this phase waits for the operator. `None` is deliberate -- a number
            # here would look like a guarantee the engine no longer honours.
            timeout_s=None,
            # `target_do` is the key the node reads; `setpoint_mg_l` is the name the console and
            # the phase editor use. Both are sent.
            #
            # `cloud_worker.cpp:486` reads `p["target_do"]` into `cfg.target_do_mg_l`, whose
            # default is 7.5 (`control_engine.h:12`). This recipe only ever sent
            # `setpoint_mg_l`, so the key was never found and the loop regulated toward 7.5
            # while the phase, the console and the ack all said 6.00 -- the same shape as the
            # aerator `rpm`/`speed_percent` and mixer `duty` bugs: a command that is
            # acknowledged and ignored. `setpoint_mg_l` stays because `PhaseForm.tsx` and
            # `MultivariableInspector.tsx` both work in that name, so dropping it would break
            # the UI to fix the node.
            commands=(
                ("aerator", "set_mode", {"mode": "pid"}),
                ("aerator", "set_config", {
                    "target_do": 6.00,
                    "setpoint_mg_l": 6.00,
                    "kp": 221.65,
                    "ki": 1.915,
                    "kd": 0.0,
                    "beta": 0.35,
                    "tt": 100.0,
                    "tau_lag": 30.0,
                }),
            ),
            params={"setpoint_mg_l": 6.00, "kp": 221.65, "ki": 1.915, "kd": 0.0, "beta": 0.35, "tt": 100.0, "tau_lag": 30.0},
        ),
        Phase(
            phase_id="cl_run",
            num="2",
            title="Lazo Cerrado Activo (Regulación)",
            mode="auto",
            condition_text="Tiempo transcurrido ≥ duración fijada (600 s)",
            condition={"kind": "elapsed", "seconds": 600},
            timeout_s=600,
            commands=(("aerator", "start", {}),),
            params={"run_time_s": 600},
        ),
        Phase(
            phase_id="cl_eval",
            num="3",
            title="Cierre del Lazo",
            mode="manual",
            condition_text="Operador valida métricas de control",
            condition={"kind": "elapsed", "seconds": 5},
            # No timeout: this phase waits for the operator. `None` is deliberate -- a number
            # here would look like a guarantee the engine no longer honours.
            timeout_s=None,
            commands=(("aerator", "stop", {}),),
            params={},
        ),
    ),
)

_PLANTA_COMBINADA_D = Recipe(
    recipe_id="planta_d_comb",
    title="Protocolo D — Ensayo Combinado (Desoxigenación A + Escalón de Reoxigenación B)",
    filename="EXP_04_COMB_DEOX_REOX.CSV",
    roles=("od_sensor", "aerator", "mixer", "pump"),
    phases=(
        Phase(
            phase_id="pd_prep",
            num="1",
            title="Homogenización Previa del Estanque",
            desc=(
                "<strong>Etapa 1 (de Protocolo A):</strong> Homogeniza los 87.5 L de agua para "
                "eliminar estratificación térmica y asegurar flujo laminar sobre el sensor OPTOD."
            ),
            mode="auto",
            condition_text="DO estabilizado (|d[DO]/dt| < 0.05 mg/L/min) durante ≥ 30 s",
            condition={"kind": "do_stable", "eps_mg_l_per_min": 0.05, "window_s": 30},
            timeout_s=300,
            commands=(
                ("mixer", "start_mixer", {"target_rpm": 1000}),
                ("aerator", "stop", {}),
            ),
            params={"mixer_rpm": 1000, "warm_time_s": 60},
        ),
        Phase(
            phase_id="pd_dose",
            num="2",
            title="Dosificación Na₂SO₃ (Desoxigenación)",
            desc=(
                "<strong>Etapa 2 (de Protocolo A):</strong> Inyección estequiométrica con bomba peristáltica "
                "para consumir el oxígeno disuelto en el estanque a partir de los gramos de sulfito deseados."
            ),
            mode="hibrido",
            condition_text="Masa dispensada = 3.80 g Na₂SO₃ (38.0 mL ± 0.2 mL)",
            condition={"kind": "dose_complete", "target_ml": 38.0, "tol_ml": 0.2},
            timeout_s=None,
            commands=(("pump", "dose", {"target_ml": 38.0, "sulfite_g": 3.80}),),
            params={"sulfite_g": 3.80, "dose_ml": 38.0, "stock_conc_gpl": 100, "density_g_per_ml": 1.10, "mix_during_dose": True},
        ),
        Phase(
            phase_id="pd_deox",
            num="3",
            title="Caída de DO y Agotamiento de Sulfito",
            desc=(
                "<strong>Etapa 3 (de Protocolo A):</strong> Reacción química y caída de DO hasta alcanzar "
                "la meseta desoxigenada."
            ),
            mode="auto",
            condition_text="DO ≤ 3.0 mg/L Y estabilizado (ΔDO ≤ 0.04 mg/L en 60 s) durante ≥ 180 s",
            condition={
                "kind": "do_below_and_stable",
                "below_mg_l": 3.0,
                "window_s": 60,
                "max_delta_mg_l": 0.04,
                "eps_mg_l_per_min": 0.15,
                "min_elapsed_s": 180,
            },
            timeout_s=1800,
            commands=(),
            params={
                "do_floor_mg_l": 3.0,
                "max_delta_mg_l": 0.04,
                "window_s": 60,
                "safety_timeout_s": 1800,
                "do_alert": True,
            },
        ),
        Phase(
            phase_id="pd_trans",
            num="4",
            title="Transición y Verificación Pre-Escalón (C₀)",
            desc=(
                "<strong>Etapa 4 (Enlace A → B):</strong> Desarma la bomba peristáltica, verifica "
                "DO inicial bajo C₀ ≤ 3.5 mg/L y estabiliza el fluido con mezcla auxiliar antes de arrancar el aireador."
            ),
            mode="hibrido",
            condition_text="DO basal < 3.5 mg/L Y estanque en reposo durante ≥ 30 s",
            condition={"kind": "do_below", "threshold_mg_l": 3.5, "min_elapsed_s": 30},
            timeout_s=None,
            commands=(
                ("pump", "force_off", {}),
                ("aerator", "stop", {}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"do_start_max_mg_l": 3.5, "settle_time_s": 30, "mixer_enable": True, "mixer_rpm": 1000},
        ),
        Phase(
            phase_id="pd_step",
            num="5",
            title="Escalón de Aireación ODrive (Marca t₀)",
            desc=(
                "<strong>Etapa 5 (de Protocolo B):</strong> Marca automática t₀ en instante de arranque "
                "del motor ODrive a consigna de RPM para el salto de reoxigenación."
            ),
            mode="hibrido",
            condition_text="ODrive alcanza consigna (240 RPM)",
            condition={"kind": "setpoint_reached", "role": "aerator", "field": "drv_rpm", "target": 240, "tol": 30},
            timeout_s=None,
            commands=(
                ("aerator", "set_speed", {"speed_percent": 40, "target_rpm": 240, "step_rpm": 240}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"step_rpm": 240, "mixer_enable": True, "mixer_rpm": 1000},
        ),
        Phase(
            phase_id="pd_acq",
            num="6",
            title="Adquisición de Curva de Reoxigenación DO(t)",
            desc=(
                "<strong>Etapa 6 (de Protocolo B):</strong> Registro de la curva DO(t) hasta "
                "alcanzar 80–85% de saturación C* (~6.00 mg/L)."
            ),
            mode="auto",
            condition_text="DO ≥ 6.00 mg/L ó t - t₀ ≥ 900 s",
            condition={"kind": "do_above", "threshold_mg_l": 6.00},
            timeout_s=900,
            commands=(),
            params={"stop_do_mg_l": 6.00, "safety_timeout_s": 900},
        ),
        Phase(
            phase_id="pd_analysis",
            num="7",
            title="Finalización del Ensayo Combinado y Ajuste kla",
            desc=(
                "<strong>Etapa 7 (Cierre):</strong> Detiene el aireador, consolida el dataset "
                "completo de desoxigenación y reoxigenación y prepara el ajuste OLS."
            ),
            mode="manual",
            condition_text="Operador valida dataset combinado y archiva reporte",
            condition={"kind": "elapsed", "seconds": 5},
            timeout_s=None,
            commands=(
                ("aerator", "stop", {}),
                ("mixer", "stop_mixer", {}),
            ),
            params={"kla_c_star": 7.15, "export_kla": True},
        ),
    ),
)


_AUTO_BENCHMARK_KLA = Recipe(
    recipe_id="auto_benchmark_kla",
    title="Protocolo Automatizado: Curva de Reacción (KLa)",
    filename="EXP_KLA_AUTO.CSV",
    roles=("od_sensor", "aerator", "mixer", "pump"),
    phases=(
        Phase(
            phase_id="abk_prep",
            num="1",
            title="Homogenización y Estabilización Inicial",
            desc=(
                "Homogeniza los 87.5 L de agua con mezcla auxiliar para estabilizar "
                "gradientes térmicos y de OD antes de dosificar reactivos."
            ),
            mode="auto",
            condition_text="DO estabilizado (|d[DO]/dt| < 0.05 mg/L/min) tras mezcla ≥ 60 s",
            condition={"kind": "do_stable", "eps_mg_l_per_min": 0.05, "window_s": 30, "min_elapsed_s": 60},
            timeout_s=300,
            commands=(
                ("mixer", "start_mixer", {"target_rpm": 1000}),
                ("aerator", "stop", {}),
            ),
            params={"mixer_rpm": 1000, "warm_time_s": 60},
        ),
        Phase(
            phase_id="abk_dose",
            num="2",
            title="Dosificación Rápida de Na₂SO₃ (Sin Cobalto)",
            desc=(
                "Dosificación peristáltica de sulfito (8.0 g Na₂SO₃ = 80.0 mL solución madre 100 g/L) "
                "para forzar una desoxigenación rápida del estanque."
            ),
            mode="hibrido",
            condition_text="Masa dispensada = 8.00 g Na₂SO₃ (80.0 mL ± 0.2 mL) registrado por encoder",
            condition={"kind": "dose_complete", "target_ml": 80.0, "tol_ml": 0.2},
            timeout_s=None,
            commands=(("pump", "dose", {"target_ml": 80.0, "sulfite_g": 8.00}),),
            params={"sulfite_g": 8.00, "dose_ml": 80.0, "stock_conc_gpl": 100, "density_g_per_ml": 1.10, "mix_during_dose": True},
        ),
        Phase(
            phase_id="abk_deox",
            num="3",
            title="Desoxigenación Profunda y Detección de Fondo",
            desc=(
                "Espera la caída libre de DO sin catalizador de cobalto hasta alcanzar el fondo "
                "asintótico (DO ≤ 0.25 mg/L con meseta plana en tubo de estabilidad)."
            ),
            mode="auto",
            condition_text="DO ≤ 0.25 mg/L Y estabilizado (ΔDO ≤ 0.04 mg/L en 60 s) durante ≥ 180 s",
            condition={
                "kind": "do_below_and_stable",
                "below_mg_l": 0.25,
                "window_s": 60,
                "max_delta_mg_l": 0.04,
                "eps_mg_l_per_min": 0.15,
                "min_elapsed_s": 180,
            },
            timeout_s=2700,
            commands=(),
            params={"do_floor_mg_l": 0.25, "max_delta_mg_l": 0.04, "window_s": 60, "safety_timeout_s": 2700, "do_alert": True},
        ),
        Phase(
            phase_id="abk_burn",
            num="4",
            title="Purga de Sulfito Residual y Captura Basal (C₀ = 0.70 mg/L)",
            desc=(
                "El aerador realiza purga activa a 250 RPM para oxidar forzadamente el reactivo residual. "
                "El firmware del ESP32 frena predictivamente a 0 RPM al alcanzar el setpoint C₀ = 0.70 mg/L."
            ),
            mode="auto",
            condition_text="DO ≥ 0.60 mg/L alcanzado tras quemar sulfito residual",
            condition={"kind": "do_above", "threshold_mg_l": 0.60},
            timeout_s=1800,
            commands=(
                ("aerator", "set_mode", {"mode": "pid"}),
                ("aerator", "set_config", {
                    "target_do": 0.65,
                    "setpoint_mg_l": 0.65,
                    "kp": 221.65,
                    "ki": 1.915,
                    "kd": 0.0,
                    "beta": 1.0,
                    "tt": 100.0,
                    "tau_lag": 25.0,
                    "max_rpm": 300.0,
                }),
                ("aerator", "start", {}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"target_do": 0.65, "stop_do_mg_l": 0.60, "safety_timeout_s": 1800},
        ),
        Phase(
            phase_id="abk_verify",
            num="5",
            title="Verificación Post-Corte de Cero Sulfito Residual",
            desc=(
                "Aerador en 0 RPM y mezclador a 1000 RPM. Se verifica durante al menos 120 s que no haya caída "
                "química de DO (pendiente ≥ -0.005 mg/L/min), garantizando meseta plana y ausencia de reactivo libre. "
                "Si se detecta caída residual, el orquestador aplica micro-pulsos adaptativos suaves hasta neutralizar."
            ),
            mode="auto",
            condition_text="DO ≥ 0.60 mg/L sostenido y meseta plana (t ≥ 120 s, tasa ≥ -0.005 mg/L/min)",
            condition={
                "kind": "sulfite_exhausted_post_hold",
                "min_elapsed_s": 120,
                "min_do_mg_l": 0.60,
                "window_s": 60,
                "max_delta_mg_l": 0.04,
                "min_slope_mg_l_per_min": -0.005,
                "rest_cooldown_s": 60.0,
            },
            timeout_s=1800,
            on_timeout="abort",
            commands=(
                ("aerator", "stop", {}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={
                "hold_time_s": 120,
                "min_do_mg_l": 0.60,
                "safety_timeout_s": 1800,
                "rest_cooldown_s": 60.0,
                "pulse_rpm": 100,
                "pulse_trigger_slope": -0.015,
                "pulse_trigger_do": 0.45,
                "pulse_brake_do": 0.60,
                "pulse_tau_lag_s": 20.0,
                "max_pulse_s": 20,
            },
        ),
        Phase(
            phase_id="abk_step",
            num="6",
            title="Arranque del Escalón de Aireación (Marca t₀)",
            desc="Arranque automático del aerador a consigna fijada (default 360 RPM) para el salto de reoxigenación.",
            mode="auto",
            condition_text="ODrive alcanza consigna (360 RPM)",
            condition={"kind": "setpoint_reached", "role": "aerator", "field": "drv_rpm", "target": 360, "tol": 30},
            timeout_s=180,
            commands=(
                ("aerator", "set_mode", {"mode": "manual"}),
                ("aerator", "set_speed", {"speed_percent": 60, "target_rpm": 360, "step_rpm": 360}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"step_rpm": 360, "mixer_enable": True, "mixer_rpm": 1000},
        ),
        Phase(
            phase_id="abk_acq",
            num="7",
            title="Adquisición de Curva DO(t) hasta Saturación Completa C*",
            desc=(
                "Registro continuo de reoxigenación a régimen permanente hasta alcanzar la saturación asintótica "
                "(DO ≥ 6.40 mg/L con meseta estable |d[DO]/dt| < 0.03 mg/L/min en 60 s)."
            ),
            mode="auto",
            condition_text="Saturación alcanzada (DO ≥ 6.40 mg/L y meseta estable |d[DO]/dt| < 0.03 mg/L/min en 60 s)",
            condition={
                "kind": "do_above_and_stable",
                "above_mg_l": 6.40,
                "window_s": 60,
                "eps_mg_l_per_min": 0.03,
                "min_elapsed_s": 180,
            },
            timeout_s=2700,
            commands=(),
            params={
                "stop_do_mg_l": 6.40,
                "kla_c_star": 6.70,
                "window_s": 60,
                "eps_mg_l_per_min": 0.03,
                "safety_timeout_s": 2700,
            },
        ),
        Phase(
            phase_id="abk_eval",
            num="8",
            title="Finalización y Ajuste OLS KLa (ASCE 2-22)",
            desc="Detiene actuadores y consolida la estimación de KLa con ventana de truncamiento estándar [0.20, 0.80] C*.",
            mode="auto",
            condition_text="Consolidación completa del dataset y cálculo KLa",
            condition={"kind": "elapsed", "seconds": 5},
            timeout_s=30,
            commands=(
                ("aerator", "stop", {}),
                ("mixer", "stop_mixer", {}),
            ),
            params={"kla_c_star": 6.70, "export_kla": True},
        ),
    ),
)


_AUTO_BENCHMARK_CONTROL = Recipe(
    recipe_id="auto_benchmark_control",
    title="Protocolo Automatizado: Benchmark PID 2-DOF",
    filename="EXP_PID_BENCHMARK.CSV",
    roles=("od_sensor", "aerator", "mixer", "pump"),
    phases=(
        Phase(
            phase_id="abc_prep",
            num="1",
            title="Homogenización y Estabilización Inicial",
            desc="Homogeniza los 87.5 L de agua con mezcla auxiliar para estabilizar gradientes térmicos y de OD.",
            mode="auto",
            condition_text="DO estabilizado (|d[DO]/dt| < 0.05 mg/L/min) tras mezcla ≥ 60 s",
            condition={"kind": "do_stable", "eps_mg_l_per_min": 0.05, "window_s": 30, "min_elapsed_s": 60},
            timeout_s=300,
            commands=(
                ("mixer", "start_mixer", {"target_rpm": 1000}),
                ("aerator", "stop", {}),
            ),
            params={"mixer_rpm": 1000, "warm_time_s": 60},
        ),
        Phase(
            phase_id="abc_dose",
            num="2",
            title="Dosificación Rápida de Na₂SO₃ (Sin Cobalto)",
            desc="Dosificación peristáltica de sulfito (8.0 g Na₂SO₃ = 80.0 mL solución madre 100 g/L).",
            mode="hibrido",
            condition_text="Masa dispensada = 8.00 g Na₂SO₃ (80.0 mL ± 0.2 mL) registrado por encoder",
            condition={"kind": "dose_complete", "target_ml": 80.0, "tol_ml": 0.2},
            timeout_s=None,
            commands=(("pump", "dose", {"target_ml": 80.0, "sulfite_g": 8.00}),),
            params={"sulfite_g": 8.00, "dose_ml": 80.0, "stock_conc_gpl": 100, "density_g_per_ml": 1.10, "mix_during_dose": True},
        ),
        Phase(
            phase_id="abc_deox",
            num="3",
            title="Desoxigenación Profunda y Detección de Fondo",
            desc="Espera la caída libre de DO hasta alcanzar el fondo asintótico (DO ≤ 0.25 mg/L con meseta plana).",
            mode="auto",
            condition_text="DO ≤ 0.25 mg/L Y estabilizado (ΔDO ≤ 0.04 mg/L en 60 s) durante ≥ 180 s",
            condition={
                "kind": "do_below_and_stable",
                "below_mg_l": 0.25,
                "window_s": 60,
                "max_delta_mg_l": 0.04,
                "eps_mg_l_per_min": 0.15,
                "min_elapsed_s": 180,
            },
            timeout_s=2700,
            commands=(),
            params={"do_floor_mg_l": 0.25, "max_delta_mg_l": 0.04, "window_s": 60, "safety_timeout_s": 2700, "do_alert": True},
        ),
        Phase(
            phase_id="abc_burn",
            num="4",
            title="Purga de Sulfito Residual y Captura Basal (C₀ = 0.70 mg/L)",
            desc="El aerador oxida el reactivo residual a 250 RPM y frena predictivamente a C₀ = 0.70 mg/L.",
            mode="auto",
            condition_text="DO ≥ 0.60 mg/L alcanzado tras quemar sulfito residual",
            condition={"kind": "do_above", "threshold_mg_l": 0.60},
            timeout_s=1800,
            commands=(
                ("aerator", "set_mode", {"mode": "pid"}),
                ("aerator", "set_config", {
                    "target_do": 0.65,
                    "setpoint_mg_l": 0.65,
                    "kp": 221.65,
                    "ki": 1.915,
                    "kd": 0.0,
                    "beta": 1.0,
                    "tt": 100.0,
                    "tau_lag": 25.0,
                    "max_rpm": 300.0,
                }),
                ("aerator", "start", {}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"target_do": 0.65, "stop_do_mg_l": 0.60, "safety_timeout_s": 1800},
        ),
        Phase(
            phase_id="abc_verify",
            num="5",
            title="Verificación Post-Corte de Cero Sulfito Residual",
            desc="Verifica durante al menos 120 s en reposo que el DO no caiga químicamente antes del salto de control. Aplica micro-pulsos adaptativos si hay sulfito persistente.",
            mode="auto",
            condition_text="DO ≥ 0.60 mg/L sostenido y meseta plana (t ≥ 120 s, tasa ≥ -0.005 mg/L/min)",
            condition={
                "kind": "sulfite_exhausted_post_hold",
                "min_elapsed_s": 120,
                "min_do_mg_l": 0.60,
                "window_s": 60,
                "max_delta_mg_l": 0.04,
                "min_slope_mg_l_per_min": -0.005,
                "rest_cooldown_s": 60.0,
            },
            timeout_s=1800,
            on_timeout="abort",
            commands=(
                ("aerator", "stop", {}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={
                "hold_time_s": 120,
                "min_do_mg_l": 0.60,
                "safety_timeout_s": 1800,
                "rest_cooldown_s": 60.0,
                "pulse_rpm": 100,
                "pulse_trigger_slope": -0.015,
                "pulse_trigger_do": 0.45,
                "pulse_brake_do": 0.60,
                "pulse_tau_lag_s": 20.0,
                "max_pulse_s": 20,
            },
        ),
        Phase(
            phase_id="abc_arm",
            num="6",
            title="Armado del Lazo PID y Consigna SP = 5.00 mg/L",
            desc="Configura parámetros de control 2-DOF PI y fija consigna del escalón a SP = 5.00 mg/L (configurable).",
            mode="auto",
            condition_text="Lazo PID armado y configurado hacia SP = 5.00 mg/L",
            condition={"kind": "elapsed", "seconds": 3},
            timeout_s=30,
            commands=(
                ("aerator", "set_mode", {"mode": "pid"}),
                ("aerator", "set_config", {
                    "target_do": 5.00,
                    "setpoint_mg_l": 5.00,
                    "kp": 221.65,
                    "ki": 1.915,
                    "kd": 0.0,
                    "beta": 0.35,
                    "tt": 100.0,
                    "tau_lag": 30.0,
                    "max_rpm": 600.0,
                }),
            ),
            params={"target_do": 5.00, "kp": 221.65, "ki": 1.915, "kd": 0.0, "beta": 0.35, "tt": 100.0, "tau_lag": 30.0},
        ),
        Phase(
            phase_id="abc_run",
            num="7",
            title="Lazo Cerrado Activo: Escalón de Control hacia SP",
            desc="Regulación autónoma en lazo cerrado durante 600 s para caracterización de respuesta temporal y energía.",
            mode="auto",
            condition_text="Tiempo transcurrido ≥ duración fijada (600 s)",
            condition={"kind": "elapsed", "seconds": 600},
            timeout_s=600,
            commands=(
                ("aerator", "start", {}),
                ("mixer", "start_mixer", {"target_rpm": 1000}),
            ),
            params={"target_do": 5.00, "run_time_s": 600},
        ),
        Phase(
            phase_id="abc_eval",
            num="8",
            title="Cierre del Ensayo y Reporte Automatizado de KPIs",
            desc="Detiene actuadores y calcula tr, ts(5%), ts(2%), Mp%, IAE, ITAE y consumo energético exclusivo de ODrive (W·h).",
            mode="auto",
            condition_text="Cálculo de métricas KPI de control consolidado",
            condition={"kind": "elapsed", "seconds": 5},
            timeout_s=30,
            commands=(
                ("aerator", "stop", {}),
                ("mixer", "stop_mixer", {}),
            ),
            params={"target_do": 5.00, "eval_kpi": True},
        ),
    ),
)


# ---------------------------------------------------------------------------
# Benchmark de control con base certificada (C0 = 1.0 mg/L por cruce)
# ---------------------------------------------------------------------------
#: Base del lazo: el PI se arma en el instante en que el OD medido cruza esta cota subiendo,
#: con la reaireación del mezclador certificada (sin sulfito residual). Ver INFORME §10.
BASE_DO_MG_L = 1.0
#: Tolerancia de conformidad de la condición inicial (OD medido al armar en [base, base + tol]).
BASE_TOL_MG_L = 0.10
#: Pendiente mínima de la subida libre (aerador en 0, mezclador a 1000 rpm). Sin sulfito, el
#: mezclador sube el OD ≈ 0.07–0.11 mg/L/min a 1 mg/L (k_s ≈ 0.65–1.24 h⁻¹); una meseta a OD bajo
#: significa sulfito consumiendo lo que aporta el mezclador.
BASE_MIN_SLOPE = 0.06
#: Curvatura mínima (mg/L/min²) de la subida en 120 s: descarta una subida que se apaga (cola de
#: un pulso vista a través del retardo de medición, con sulfito todavía presente). El ruido de la
#: curvatura en reposo es ≈ ±0.01 (26-03), de ahí el margen.
BASE_MIN_CURVATURE = -0.02
#: Fin de la desoxigenación de la receta base: el piso real con dosis normal fue 0.32 (26-03).
BASE_DEOX_FLOOR = 0.5


def _abc_phase(phase_id: str) -> Phase:
    return next(p for p in _AUTO_BENCHMARK_CONTROL.phases if p.phase_id == phase_id)


_ABC_BASE_VERIFY = Phase(
    phase_id="abc_verify",
    num="4",
    title=f"Acondicionamiento: quemar residual bajo la base y armar al cruzar C₀ = {BASE_DO_MG_L:.2f} mg/L",
    desc=(
        "Sin purga fuerte (su sobreimpulso por el retardo de 37 s supera la base). Aerador en reposo y "
        "mezclador a 1000 RPM; pulsos suaves sostienen el OD bajo la base mientras se consume el sulfito. "
        "Cuando ya no hay sulfito, el mezclador sube el OD solo: se certifica esa subida y el lazo se arma "
        "en el instante en que el OD cruza la base."
    ),
    mode="auto",
    condition_text=(
        f"OD ≥ {BASE_DO_MG_L:.2f} mg/L subiendo a la tasa del mezclador (≥ {BASE_MIN_SLOPE} mg/L/min en 60 s), "
        "subida que no se apaga (curvatura ≥ −0.02 mg/L/min² en 120 s), aerador en reposo ≥ 90 s"
    ),
    condition={
        "kind": "sulfite_exhausted_post_hold",
        "min_elapsed_s": 120,
        "min_do_mg_l": BASE_DO_MG_L,
        "window_s": 60,
        "max_delta_mg_l": 0.30,
        "min_slope_mg_l_per_min": BASE_MIN_SLOPE,
        "rest_cooldown_s": 90.0,
        "min_curvature_mg_l_per_min2": BASE_MIN_CURVATURE,
        "curvature_window_s": 120,
    },
    timeout_s=3000,
    on_timeout="pause",
    commands=(
        ("aerator", "stop", {}),
        ("mixer", "start_mixer", {"target_rpm": 1000}),
    ),
    params={
        "hold_time_s": 120,
        "min_do_mg_l": BASE_DO_MG_L,
        "min_slope_mg_l_per_min": BASE_MIN_SLOPE,
        "max_delta_mg_l": 0.30,
        "safety_timeout_s": 3000,
        "rest_cooldown_s": 90.0,
        "min_curvature_mg_l_per_min2": BASE_MIN_CURVATURE,
        "curvature_window_s": 120,
        # Diagnóstico de la condición inicial (no deciden): C' = y' + τ·y'' y demanda residual
        # r = k_s·(C* − C) − C'. τ es el retardo aparente identificado (cota superior), k_s y C*
        # la reaireación del mezclador medida en 11-17-01.
        "sensor_tau_s": 18.0,
        "mixer_ks_per_h": 1.24,
        "c_star_mg_l": 6.5,
        "pulse_rpm": 110,
        "pulse_trigger_slope": 0.0,
        "pulse_trigger_do": round(BASE_DO_MG_L - 0.45, 2),
        "pulse_brake_do": round(BASE_DO_MG_L - 0.30, 2),
        "pulse_tau_lag_s": 37.0,
        "max_pulse_s": 12,
        "base_do_mg_l": BASE_DO_MG_L,
        "base_tol_mg_l": BASE_TOL_MG_L,
    },
)

#: En la receta base todo es automático: la dosis no espera confirmación (en modo híbrido sin
#: timeout la corrida se quedaba esperando si nadie cambiaba los modos al lanzar). La bomba entrega
#: 80 mL en ≈ 170 s; si en 600 s no reporta el volumen, la corrida se aborta (no tiene sentido seguir).
_ABC_BASE_DOSE = replace(_abc_phase("abc_dose"), mode="auto", timeout_s=600, on_timeout="abort")

_ABC_BASE_DEOX = replace(
    _abc_phase("abc_deox"),
    desc=(
        f"Espera la caída libre de DO hasta una meseta bajo {BASE_DEOX_FLOOR} mg/L. El residual que quede "
        "lo consume la verificación siguiente; no hace falta llegar al fondo."
    ),
    condition_text=f"DO ≤ {BASE_DEOX_FLOOR} mg/L Y estabilizado (ΔDO ≤ 0.04 mg/L en 60 s) durante ≥ 180 s",
    condition={**_abc_phase("abc_deox").condition, "below_mg_l": BASE_DEOX_FLOOR},
    params={
        **_abc_phase("abc_deox").params,
        "do_floor_mg_l": BASE_DEOX_FLOOR,
        # Dosis complementaria si la caída se estanca sobre 0.7 mg/L (dosis corta, como 26-02):
        # tras ≥ 360 s en la fase y pendiente de 120 s ≥ −0.04 mg/L/min. Volumen por balance de O₂:
        # 80 mL ≙ 11.6 mg/L nominal; con el rendimiento bajo observado (0.65) son 10.6 mL por mg/L.
        "redose_enable": True,
        "redose_after_s": 360,
        "redose_min_do_mg_l": 0.7,
        "redose_window_s": 120,
        "redose_stall_slope": -0.04,
        "redose_margin_mg_l": 0.8,
        "redose_ml_per_mg_l": 10.6,
        "redose_min_ml": 10,
        "redose_max_ml": 40,
        "redose_max_count": 2,
        "stock_conc_gpl": 100,
        "density_g_per_ml": 1.10,
    },
)

#: Fin del lazo por estabilización: tras el pico el aerador satura en 0 rpm y lo que sigue es deriva
#: de la planta. Mínimo 180 s (t₉₀ ≤ 130 s en todos los lazos registrados) para que la ventana fija de
#: KPIs exista siempre; tope 600 s.
LOOP_SETTLE = {"min_elapsed_s": 180, "band_mg_l": 0.30, "window_s": 60, "eps_mg_l_per_min": 0.02}

_ABC_BASE_RUN = replace(
    _abc_phase("abc_run"),
    num="6",
    desc=(
        "Regulación autónoma en lazo cerrado hasta que el OD se estabiliza cerca de la consigna "
        "(tope 600 s). Tras el pico el aerador satura en 0 rpm: seguir no aporta información del lazo."
    ),
    condition_text=(
        f"t ≥ {LOOP_SETTLE['min_elapsed_s']} s, |OD − SP| ≤ {LOOP_SETTLE['band_mg_l']:.2f} mg/L y "
        f"|pendiente| ≤ {LOOP_SETTLE['eps_mg_l_per_min']} mg/L/min en {LOOP_SETTLE['window_s']} s (tope 600 s)"
    ),
    condition={"kind": "loop_settled", "setpoint": 5.00, **LOOP_SETTLE},
    params={**_abc_phase("abc_run").params, **{f"settle_{k}": v for k, v in LOOP_SETTLE.items()}},
)

_AUTO_BENCHMARK_CONTROL_BASE = Recipe(
    recipe_id="auto_benchmark_control_base",
    title=f"Protocolo Automatizado: Benchmark PID 2-DOF con base certificada (C₀ = {BASE_DO_MG_L:.1f} mg/L)",
    filename="EXP_PID_BASE.CSV",
    roles=_AUTO_BENCHMARK_CONTROL.roles,
    phases=(
        _abc_phase("abc_prep"),
        _ABC_BASE_DOSE,
        _ABC_BASE_DEOX,
        _ABC_BASE_VERIFY,
        replace(_abc_phase("abc_arm"), num="5"),
        _ABC_BASE_RUN,
        replace(_abc_phase("abc_eval"), num="7"),
    ),
)


RECIPES: dict[str, Recipe] = {
    r.recipe_id: r
    for r in (
        _PLANTA_1_DEOX,
        _PLANTA_2_STEP,
        _CLOSED_LOOP,
        _PLANTA_COMBINADA_D,
        _AUTO_BENCHMARK_KLA,
        _AUTO_BENCHMARK_CONTROL,
        _AUTO_BENCHMARK_CONTROL_BASE,
    )
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_VALID_MODES = frozenset({"auto", "hibrido", "manual"})
_VALID_ON_TIMEOUT = frozenset({"advance", "abort", "pause"})
_VALID_CONDITIONS = frozenset(
    {
        "elapsed",
        "do_below",
        "do_above",
        "do_stable",
        "do_below_and_stable",
        "dose_complete",
        "setpoint_reached",
        "sulfite_exhausted_post_hold",
        "do_above_and_stable",
        "loop_settled",
    }
)


def validate_recipes(recipes: Mapping[str, Recipe] | None = None) -> None:
    """
    Fail loudly on a recipe that could never complete or never be understood.

    Called at import. A recipe with a phase that has no timeout, an unknown mode, or an
    unknown condition kind is a bug that would otherwise surface mid-experiment — exactly
    when nobody can fix it.
    """
    recipes = recipes or RECIPES
    for recipe in recipes.values():
        if not recipe.phases:
            raise ValueError(f"recipe {recipe.recipe_id!r} has no phases")
        seen: set[str] = set()
        for phase in recipe.phases:
            if phase.phase_id in seen:
                raise ValueError(f"recipe {recipe.recipe_id!r} has duplicate phase {phase.phase_id!r}")
            seen.add(phase.phase_id)
            if phase.mode not in _VALID_MODES:
                raise ValueError(f"{recipe.recipe_id}.{phase.phase_id}: bad mode {phase.mode!r}")
            if phase.on_timeout not in _VALID_ON_TIMEOUT:
                raise ValueError(f"{recipe.recipe_id}.{phase.phase_id}: bad on_timeout {phase.on_timeout!r}")
            kind = phase.condition.get("kind")
            if kind not in _VALID_CONDITIONS:
                raise ValueError(f"{recipe.recipe_id}.{phase.phase_id}: unknown condition {kind!r}")
            # The deadlock guard, scoped to the mode that needs it. An `auto` phase advances on
            # its own, so without a ceiling a sensor that dies mid-run holds the run open with
            # nobody watching. A supervised phase has a human watching by definition -- that is
            # what the mode means -- so its timeout is optional and `None` is an honest "no
            # valve" rather than a number the engine would not read.
            if phase.timeout_s is not None and phase.timeout_s <= 0:
                raise ValueError(f"{recipe.recipe_id}.{phase.phase_id}: timeout_s must be positive or None")
            if phase.mode == "auto" and phase.timeout_s is None:
                raise ValueError(
                    f"{recipe.recipe_id}.{phase.phase_id}: an auto phase needs a timeout_s -- "
                    "it advances without an operator, so nothing else bounds it"
                )


validate_recipes()
