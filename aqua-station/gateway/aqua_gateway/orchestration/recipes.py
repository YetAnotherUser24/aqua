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
from dataclasses import dataclass, field
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
                "por mg O₂). El volumen de solución madre (100 g/L) se calcula para llevar el "
                "estanque al DO piso objetivo."
            ),
            mode="hibrido",
            condition_text="Volumen dispensado = objetivo (38.0 mL ± 0.2 mL) registrado por encoder",
            condition={"kind": "dose_complete", "target_ml": 38.0, "tol_ml": 0.2},
            # No timeout: this phase waits for the operator. `None` is deliberate -- a number
            # here would look like a guarantee the engine no longer honours.
            timeout_s=None,
            commands=(("pump", "dose", {"target_ml": 38.0}),),
            # 38.0 mL, not the 12.5 mL this carried before. The dose has to satisfy the *next*
            # phase, which waits for `DO < 3.0` (p1_deox, do_floor_mg_l 3.0), and the arithmetic
            # never worked:
            #
            #   stock 100 g/L -> 100 mg Na2SO3 per mL
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
            params={"dose_ml": 38.0, "stock_conc_gpl": 100},
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
            condition_text="DO < 3.0 mg/L Y derivada en el fondo de respiración Y t > 180 s",
            condition={
                "kind": "do_below_and_stable",
                "below_mg_l": 3.0,
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
            timeout_s=600,
            # Stop the mixer for the measurement. The impeller is a re-oxygenation surface, so
            # with it running the tank's equilibrium is `Cs - respiration/kla` =
            # 8.3 - 0.0012/(0.25*0.0035) ~= 6.9 mg/L -- above the 3.0 floor, and the derivative
            # only flattens approaching it. Mixing is what makes the sulfite react, which is why
            # it runs through p1_prep and the dose; once the reagent is spent it is only a source
            # of oxygen, and the protocol asks for the point where "la caída cesa" -- the end of
            # the *reaction*, measured without the re-oxygenation the impeller adds.
            # Sulfite reaction consumes dissolved oxygen. Mixing distributes the chemical.
            commands=(),
            params={"do_floor_mg_l": 3.0},
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
    roles=("od_sensor", "aerator"),
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
            commands=(("aerator", "stop", {}),),
            params={"do_start_max_mg_l": 3.5, "settle_time_s": 60},
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
            commands=(("aerator", "set_speed", {"speed_percent": 40, "target_rpm": 240, "step_rpm": 240}),),
            params={"step_rpm": 240, "mixer_rpm": 0},
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
            params={"stop_do_mg_l": 6.00},
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
            commands=(("aerator", "stop", {}),),
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
                "para consumir el oxígeno disuelto en el estanque."
            ),
            mode="hibrido",
            condition_text="Volumen dispensado = objetivo (38.0 mL ± 0.2 mL)",
            condition={"kind": "dose_complete", "target_ml": 38.0, "tol_ml": 0.2},
            timeout_s=None,
            commands=(("pump", "dose", {"target_ml": 38.0}),),
            params={"dose_ml": 38.0, "stock_conc_gpl": 100, "mix_during_dose": True},
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
            condition_text="DO ≤ 3.0 mg/L Y estabilizado (|d[DO]/dt| < 0.15 mg/L/min) durante ≥ 180 s",
            condition={
                "kind": "do_below_and_stable",
                "below_mg_l": 3.0,
                "eps_mg_l_per_min": 0.15,
                "min_elapsed_s": 180,
            },
            timeout_s=600,
            commands=(),
            params={"do_floor_mg_l": 3.0},
        ),
        Phase(
            phase_id="pd_trans",
            num="4",
            title="Transición y Verificación Pre-Escalón (C₀)",
            desc=(
                "<strong>Etapa 4 (Enlace A → B):</strong> Desarma la bomba peristáltica, verifica "
                "DO inicial bajo C₀ ≤ 3.5 mg/L y estabiliza el fluido antes de arrancar el aireador."
            ),
            mode="hibrido",
            condition_text="DO basal < 3.5 mg/L Y estanque en reposo durante ≥ 30 s",
            condition={"kind": "do_below", "threshold_mg_l": 3.5, "min_elapsed_s": 30},
            timeout_s=None,
            commands=(
                ("pump", "force_off", {}),
                ("aerator", "stop", {}),
            ),
            params={"do_start_max_mg_l": 3.5, "settle_time_s": 30},
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
            commands=(("aerator", "set_speed", {"speed_percent": 40, "target_rpm": 240, "step_rpm": 240}),),
            params={"step_rpm": 240, "mixer_rpm": 0},
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
            params={"stop_do_mg_l": 6.00},
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
            commands=(("aerator", "stop", {}),),
            params={"kla_c_star": 7.15, "export_kla": True},
        ),
    ),
)


RECIPES: dict[str, Recipe] = {r.recipe_id: r for r in (_PLANTA_1_DEOX, _PLANTA_2_STEP, _CLOSED_LOOP, _PLANTA_COMBINADA_D)}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_VALID_MODES = frozenset({"auto", "hibrido", "manual"})
_VALID_CONDITIONS = frozenset(
    {
        "elapsed",
        "do_below",
        "do_above",
        "do_stable",
        "do_below_and_stable",
        "dose_complete",
        "setpoint_reached",
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
