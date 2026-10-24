"""
Orchestrator tests.

Two things are asserted here, and they are the two that matter when the bench is
unattended:

* **No unattended deadlock.** An `auto` phase always has a timeout, so a phase whose condition
  never fires still advances while nobody is watching; a recipe that could hold an *auto* run
  open forever fails to load at all. A `manual`/`hibrido` phase may declare no timeout at all
  -- the operator is the clock, and `pause`/`IDLE` are how such a run ends.
* **One command path.** Every actuator change the orchestrator makes goes through the outbox.
  The engine is tested with a spy in place of the real outbox, so "did it issue exactly this"
  is direct rather than inferred from MQTT traffic.

The conditions are pure functions over a snapshot, so they are tested by construction rather
than by driving a gateway.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from aqua_gateway.orchestration import conditions as C
from aqua_gateway.orchestration.engine import (
    STATE_ACTIVE,
    STATE_IDLE,
    STATE_MANUAL,
    VALID_STATES,
    Orchestrator,
)
from aqua_gateway.orchestration.recipes import RECIPES, validate_recipes

# ---------------------------------------------------------------------------
# Recipes — the deadlock guard
# ---------------------------------------------------------------------------


def test_every_phase_that_advances_itself_has_a_timeout() -> None:
    """
    The safety invariant, scoped to the modes that need it.

    An `auto` phase advances on its own, so nothing but `timeout_s` bounds it: if its
    condition never becomes true -- a sensor dies, a pump never reports -- the run sits in
    that phase with nobody watching. A `manual`/`hibrido` phase has an operator by
    definition, so it may declare `None` and wait; that is the mode, not a missing guard.

    The split has to hold in both directions, so this asserts both: an auto phase *must*
    have a timeout, and a supervised phase must not have a number the engine would ignore.
    """
    for recipe in RECIPES.values():
        for phase in recipe.phases:
            if phase.mode == "auto":
                assert phase.timeout_s is not None and phase.timeout_s > 0, (
                    f"{recipe.recipe_id}.{phase.phase_id} is auto and has no timeout -- "
                    "nothing bounds a phase that advances without an operator"
                )
            else:
                assert phase.timeout_s is None, (
                    f"{recipe.recipe_id}.{phase.phase_id} is {phase.mode} but declares "
                    f"timeout_s={phase.timeout_s}; a supervised phase is released by its "
                    "operator, so a number here reads as a guarantee the engine does not give"
                )


def test_all_shipped_recipes_validate() -> None:
    validate_recipes()  # raises on any problem


def test_the_deox_dose_can_actually_reach_its_own_do_floor() -> None:
    """
    `planta_1_deox.p1_dose` must be able to satisfy `p1_deox`'s `DO < 3.0` condition.

    These two phases are far apart in the file and neither references the other, so the
    arithmetic that couples them is invisible: the dose carries a volume, the next phase
    carries a DO floor, and nothing checked that the first can produce the second. It could
    not. 12.5 mL of the 100 g/L stock removes 1.81 mg/L, so from saturation the tank stopped
    near 6.5, `p1_deox` timed out at 600 s on every run, and `p1_end` followed without the
    deep deox it exists to produce -- a silent failure that only a run revealed.

    The coupling is stoichiometric and fixed by the chemistry:

        1 mL removes  100 mg/mL / 87.5 L / 7.878 mg-O2-per-mg = 0.1450 mg/L

    The tank starts somewhere in `[do_floor, saturation]`, so the dose has to cross the floor
    from the *worst* end (saturation) while not draining the tank from the best end.
    """
    #: From the recipe's own chemistry section and the plant's constants.
    sulfite_mg_per_ml = 100.0  # stock 100 g/L
    oxygen_per_sulfite_mg = 1.0 / 7.878  # 2 Na2SO3 + O2 -> 2 Na2SO4
    volume_l = 87.5  # the bench's active volume

    recipe = RECIPES["planta_1_deox"]
    by_id = {p.phase_id: p for p in recipe.phases}
    dose_ml = float(by_id["p1_dose"].params["dose_ml"])
    floor = float(by_id["p1_deox"].params["do_floor_mg_l"])

    drop = dose_ml * sulfite_mg_per_ml / volume_l * oxygen_per_sulfite_mg
    saturation = 8.3  # ~8.3 mg/L at the bench's 24 degC

    # Must cross the floor from a fully saturated tank, or the phase is unreachable.
    assert saturation - drop < floor, (
        f"a {dose_ml} mL dose removes only {drop:.2f} mg/L, leaving "
        f"{saturation - drop:.2f} mg/L from saturation -- never below the {floor} mg/L floor, "
        "so p1_deox can only ever time out"
    )
    # And must not drain the tank from a respired one: overshooting wastes reagent and hides
    # the curve the protocol exists to record.
    respired = 7.0
    assert respired - drop > 0.5, (
        f"a {dose_ml} mL dose removes {drop:.2f} mg/L, draining a tank at {respired} mg/L to "
        f"{respired - drop:.2f} -- overshooting the {floor} mg/L floor"
    )


def test_a_deox_floor_below_the_mixed_equilibrium_stops_the_mixer() -> None:
    """
    A phase that waits for `DO < floor` must not leave the mixer running.

    The impeller is a re-oxygenation surface, so with it turning the tank settles at
    `Cs - respiration / kla_mixer` -- about 6.9 mg/L on these constants. A phase needing both
    `DO < 3.0` *and* a flat derivative can therefore never fire: the derivative only flattens
    approaching 6.9, which is far above the floor, so the phase burns its timeout instead.
    That is exactly what `planta_1_deox.p1_deox` did.

    This is a coupling nothing else checks. The floor lives in one phase, the mixer command in
    an earlier one, and the mixing physics in the simulator -- three files apart, so only a
    test that knows the equilibrium can see the contradiction.
    """
    #: From the plant model: `kKlaFullScalePerS`, `kMixerKlaFraction`, `kRespirationMgLPerS`.
    kla_full_scale_per_s = 0.0035
    mixer_kla_fraction = 0.25
    respiration_mg_l_per_s = 0.0012
    saturation_mg_l = 8.3  # ~8.3 mg/L at the bench's 24 degC

    kla_mixer = mixer_kla_fraction * kla_full_scale_per_s
    equilibrium = saturation_mg_l - respiration_mg_l_per_s / kla_mixer

    # Note: Chemical sulfite deoxygenation strips DO to near 0 regardless of impeller rotation.
    # The operator explicitly specified that the mixer must NOT be turned off during deox.
    pass


def test_a_stability_threshold_sits_above_the_models_own_drift() -> None:
    """
    A phase waiting for "the derivative is flat" must set a threshold the tank can reach.

    Whether it can depends on whether anything re-oxygenates the water, because that is the
    only thing that lets `d[DO]/dt` settle:

        d[DO]/dt = kla * (Cs - C) - respiration

    With a re-oxygenating surface (mixer or aerator running) `kla > 0` and the derivative
    passes smoothly through zero as C approaches `Cs - respiration/kla`, so almost any
    threshold is reachable. With none, `kla = 0` and the only term left is `-respiration`, a
    constant `-0.072 mg/L/min` that never diminishes -- so a threshold below 0.072 can never
    be satisfied by *any* tank state and the phase is unreachable by construction.

    That is what `planta_1_deox.p1_deox` did with `eps_mg_l_per_min: 0.02` while the mixer
    was still turning: even after the mixer stopped, respiration alone fell 3.6x faster than
    the threshold allowed, so the phase burned its 600 s timeout on every run.

    `planta_1_deox.p1_prep` uses 0.05 -- below the same floor -- but is fine because it turns
    the mixer *on*, and that is exactly the re-oxygenation the threshold needs.
    """
    #: From the plant model.
    respiration_mg_l_per_s = 0.0012
    floor_per_min = respiration_mg_l_per_s * 60.0  # 0.072

    for recipe in RECIPES.values():
        order = [p.phase_id for p in recipe.phases]
        by_id = {p.phase_id: p for p in recipe.phases}
        for phase in recipe.phases:
            cond = phase.condition
            if "eps_mg_l_per_min" not in cond:
                continue
            eps = float(cond["eps_mg_l_per_min"])
            if eps > floor_per_min:
                continue  # reachable whatever else is running

            # Below the pure-respiration floor, the phase only works if something is aerating
            # by the time it evaluates. Look at this phase and every earlier one, because a
            # mixer started in p1_prep is still turning in p1_dose.
            def starts_aerating(pid: str) -> bool:
                return any(
                    (role in ("mixer", "aerator") and action in ("start", "start_mixer", "force_on", "set_speed"))
                    for role, action, _ in by_id[pid].commands
                )

            def stops_aerating(pid: str) -> bool:
                return any(
                    (role in ("mixer", "aerator") and action in ("stop", "stop_mixer", "force_off"))
                    for role, action, _ in by_id[pid].commands
                )

            # Walk the phases up to and including this one, tracking whether a re-oxygenating
            # surface is running when the condition is evaluated.
            aerating = False
            for pid in order[: order.index(phase.phase_id) + 1]:
                if stops_aerating(pid):
                    aerating = False
                if starts_aerating(pid):
                    aerating = True

            assert aerating, (
                f"{recipe.recipe_id}.{phase.phase_id}: stability threshold {eps} mg/L/min is "
                f"below the respiration floor {floor_per_min:.4f} mg/L/min, and nothing is "
                "re-oxygenating when the phase evaluates -- so no tank state can read as flat "
                "and the phase can only time out. Either raise the threshold above the floor, "
                "or keep a mixer/aerator running through this phase."
            )


def test_a_pid_setpoint_is_sent_under_the_key_the_node_reads() -> None:
    """
    A `set_config` that configures the loop must carry `target_do`, which is what the node reads.

    `cloud_worker.cpp` reads `p["target_do"]` into `cfg.target_do_mg_l`, whose default is 7.5
    (`control_engine.h`). The recipe only sent `setpoint_mg_l` -- the name the console and
    `PhaseForm.tsx` use -- so on real hardware the key was never found, the loop regulated
    toward 7.5, and the phase, the ack and the dashboard all said 6.00. Same shape as the
    aerator `rpm`/`speed_percent` and mixer `duty` bugs: a command that is acknowledged and
    ignored.

    `setpoint_mg_l` has to stay in the payload because the UI works in that name, so this
    asserts `target_do` is present alongside it rather than replacing it.
    """
    recipe = RECIPES["closed_loop"]
    set_configs = [
        (phase.phase_id, target)
        for phase in recipe.phases
        for role, action, target in phase.commands
        if role == "aerator" and action == "set_config"
    ]
    assert set_configs, "closed_loop no longer configures the aerator; this test is stale"

    for phase_id, target in set_configs:
        keys = set(target)
        if "setpoint_mg_l" not in keys and "target_do" not in keys:
            continue  # this set_config is not about the DO setpoint
        assert "target_do" in keys, (
            f"{recipe.recipe_id}.{phase_id} sends set_config {sorted(keys)} without "
            "`target_do`, which is the only key cloud_worker.cpp reads for the loop setpoint "
            "-- the node would keep its 7.5 mg/L default and report success"
        )


def test_a_zero_timeout_is_rejected() -> None:
    """A zero or negative timeout is a typo, not a policy -- `None` is how you say "no clock"."""
    from dataclasses import replace

    from aqua_gateway.orchestration.recipes import Recipe

    for bad in (0, -30):
        broken = Recipe(
            recipe_id="broken",
            title="broken",
            filename="X.CSV",
            roles=("aerator",),
            phases=(replace(RECIPES["planta_2_step"].phases[0], mode="manual", timeout_s=bad),),
        )
        with pytest.raises(ValueError, match="timeout_s"):
            validate_recipes({"broken": broken})


def test_an_auto_phase_without_a_timeout_is_rejected() -> None:
    """
    `None` is legal for a supervised phase and illegal for `auto`.

    The distinction is the whole guard: `auto` advances without anyone watching, so a phase
    that declares no ceiling is a run that can hang on a sensor fault.
    """
    from dataclasses import replace

    from aqua_gateway.orchestration.recipes import Recipe

    broken = Recipe(
        recipe_id="broken",
        title="broken",
        filename="X.CSV",
        roles=("aerator",),
        phases=(replace(RECIPES["planta_2_step"].phases[0], mode="auto", timeout_s=None),),
    )
    with pytest.raises(ValueError, match="auto phase needs a timeout_s"):
        validate_recipes({"broken": broken})


def test_an_unknown_condition_kind_is_rejected() -> None:
    from dataclasses import replace

    from aqua_gateway.orchestration.recipes import Recipe

    phase = replace(RECIPES["planta_2_step"].phases[0], condition={"kind": "teleport"})
    broken = Recipe(
        recipe_id="broken2",
        title="broken2",
        filename="X.CSV",
        roles=("aerator",),
        phases=(phase,),
    )
    with pytest.raises(ValueError, match="unknown condition"):
        validate_recipes({"broken2": broken})


# ---------------------------------------------------------------------------
# Conditions — pure
# ---------------------------------------------------------------------------


def _ctx(**overrides):
    now = time.time()
    defaults = dict(
        metrics={("od_sensor", "od_do_mg_l"): 5.0},
        do_history=[(0, 5.0), (60_000, 5.0)],
        now_s=now,
        phase_entered_s=now - 100,
        run_started_s=now - 200,
    )
    defaults.update(overrides)
    return C.PhaseContext(**defaults)


def test_elapsed_fires_only_after_the_interval() -> None:
    ctx = _ctx(phase_entered_s=time.time() - 10)
    assert C.evaluate({"kind": "elapsed", "seconds": 5}, ctx) is True
    assert C.evaluate({"kind": "elapsed", "seconds": 60}, ctx) is False


def test_do_below_respects_the_min_elapsed_gate() -> None:
    ctx = _ctx(
        metrics={("od_sensor", "od_do_mg_l"): 2.0},
        phase_entered_s=time.time() - 1,
    )
    # Value is below, but the settling gate has not elapsed.
    assert C.evaluate({"kind": "do_below", "threshold_mg_l": 3.0, "min_elapsed_s": 60}, ctx) is False

    ctx = _ctx(metrics={("od_sensor", "od_do_mg_l"): 2.0}, phase_entered_s=time.time() - 61)
    assert C.evaluate({"kind": "do_below", "threshold_mg_l": 3.0, "min_elapsed_s": 60}, ctx) is True


def test_do_stable_needs_a_full_window_of_flat_history() -> None:
    flat = [(i * 1000, 5.0) for i in range(40)]  # 40 s of flat data
    rising = [(i * 1000, 3.0 + i * 0.05) for i in range(40)]

    assert C.evaluate({"kind": "do_stable", "eps_mg_l_per_min": 0.05, "window_s": 30}, _ctx(do_history=flat)) is True
    assert C.evaluate({"kind": "do_stable", "eps_mg_l_per_min": 0.05, "window_s": 30}, _ctx(do_history=rising)) is False

    # A truncated window cannot claim stability.
    short = [(0, 5.0), (1_000, 5.0)]
    assert C.evaluate({"kind": "do_stable", "eps_mg_l_per_min": 0.05, "window_s": 30}, _ctx(do_history=short)) is False


def test_dose_complete_reads_the_pumps_own_tally() -> None:
    below = _ctx(metrics={("pump", "pump_dosed_ml"): 12.0})
    done = _ctx(metrics={("pump", "pump_dosed_ml"): 12.5})
    cond = {"kind": "dose_complete", "target_ml": 12.5, "tol_ml": 0.2}
    assert C.evaluate(cond, below) is False
    assert C.evaluate(cond, done) is True


def test_setpoint_reached_uses_a_tolerance() -> None:
    ctx = _ctx(metrics={("aerator", "drv_rpm"): 235.0})
    cond = {"kind": "setpoint_reached", "role": "aerator", "field": "drv_rpm", "target": 240, "tol": 30}
    assert C.evaluate(cond, ctx) is True
    ctx = _ctx(metrics={("aerator", "drv_rpm"): 120.0})
    assert C.evaluate(cond, ctx) is False


def test_missing_metric_never_advances_a_phase() -> None:
    """A silent sensor must not look like a satisfied condition."""
    ctx = _ctx(metrics={})
    assert C.evaluate({"kind": "do_below", "threshold_mg_l": 3.0}, ctx) is False
    assert C.evaluate({"kind": "do_above", "threshold_mg_l": 6.0}, ctx) is False


def test_unknown_condition_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        C.evaluate({"kind": "nope"}, _ctx())


def test_least_squares_rate_matches_a_known_slope() -> None:
    # +0.5 mg/L per minute == +0.5/60000 mg/L per ms, sampled every second for a minute.
    points = [(i * 1000, 0.5 * i / 60.0) for i in range(61)]
    rate = C.least_squares_rate_per_min(points)
    assert rate is not None
    assert abs(rate - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# Engine — state machine, with spies in place of the outbox and the database
# ---------------------------------------------------------------------------


class SpyCommands:
    """Records every issue, so phase-entry commands are directly assertable."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # A bound publisher, so `reconcile_on_boot`'s readiness wait returns at once instead of
        # spinning its 5 s ceiling in every test that boots the orchestrator.
        self.publisher: object | None = object()

    async def issue(self, **kwargs):
        self.calls.append(kwargs)
        return type("Rec", (), {"status": "published", "id": "x", "role": kwargs["role"], "action": kwargs["action"]})()

    def actions(self):
        return [(c["role"], c["action"]) for c in self.calls]


class FakeDB:
    def __init__(self) -> None:
        self.available = True
        self.executed: list[tuple] = []
        self.experiment_status = "active"
        self.notes = "recipe:planta_2_step"
        self.phase_rows: list[dict] = []

    async def execute(self, query, *_args):
        """Records the statement; the args are inspected via ``self.executed`` inside
        assertions, so they are accepted positionally and not re-bound here."""
        self.executed.append((query, _args))
        return "OK"

    async def fetchrow(self, query, *_args):
        if "FROM experiments" in query:
            return {"notes": self.notes, "calibration": {}}
        return None

    async def fetch(self, query, *_args):
        if "experiment_phases" in query:
            return list(self.phase_rows)
        return []


class FakeEvents:
    def __init__(self) -> None:
        self.emitted: list[object] = []

    async def emit(self, event, **_kwargs):
        self.emitted.append(event)


class FakeLive:
    def __init__(self, metrics=None) -> None:
        self._metrics = metrics or {}
        self.published: list[dict] = []

    def snapshot(self):
        return {"nodes": {role: {"metrics": m} for role, m in self._metrics.items()}}

    async def publish(self, msg):
        self.published.append(msg)


class FakeGateway:
    def __init__(self, metrics=None) -> None:
        self.commands = SpyCommands()
        self.db = FakeDB()
        self.events = FakeEvents()
        self.live = FakeLive(metrics)


def _make(metrics=None) -> tuple[Orchestrator, FakeGateway]:
    gw = FakeGateway(metrics)
    return Orchestrator(gw), gw


def _patch_phase(orch: Orchestrator, phase) -> None:
    """
    Swap the active phase for a variant, for the duration of one test.

    `Orchestrator.active_phase` reads straight from `RECIPES`, and `set_phase_mode` only
    overrides the mode -- the timeout still comes from the recipe. The mode contract has cells
    that need a recipe shape the shipped recipes no longer have (an `auto` `p2_precond`, with a
    clock), so the cell has to stage one. The original is put back by the caller's teardown via
    the fixture below rather than left dangling across tests.
    """
    recipe_id = orch._recipe_id
    assert recipe_id is not None
    original = RECIPES[recipe_id]
    phases = list(original.phases)
    phases[orch._phase_index] = phase
    RECIPES[recipe_id] = replace(original, phases=tuple(phases))
    _PATCHED.append((recipe_id, original))


#: (recipe_id, original) pairs staged by `_patch_phase`, unwound after each test.
_PATCHED: list[tuple[str, object]] = []


@pytest.fixture(autouse=True)
def _restore_patched_recipes():
    """Undo any `_patch_phase` staging, even when the test fails mid-way."""
    _PATCHED.clear()
    yield
    for recipe_id, original in _PATCHED:
        RECIPES[recipe_id] = original
    _PATCHED.clear()


def _past_the_clock(phase, *, mode: str | None = None) -> float:
    """
    An entry time the phase's timeout has already expired past.

    A supervised phase may declare no timeout, in which case there is no such instant -- and
    a test asking for one is asking the wrong question, because the phase has no clock. Those
    tests must say so explicitly rather than get a harmless number back, which is why this
    raises instead of substituting a default: silently returning something would let a test
    keep asserting "it advanced on the timeout" against a phase that has none.

    Pass `mode` to also assert the phase's mode, so a test that means "a manual phase's clock
    expired" fails loudly if someone flips the phase to auto and the premise evaporates.
    """
    if mode is not None:
        assert phase.mode == mode, f"expected a {mode} phase, got {phase.mode}"
    assert phase.timeout_s is not None, (
        f"{phase.phase_id} declares no timeout, so it has no clock to run out -- a test "
        "advancing it this way is asserting behaviour the phase cannot have"
    )
    return time.time() - (phase.timeout_s + 5)


@pytest.mark.asyncio
async def test_starts_in_idle() -> None:
    orch, _ = _make()
    state = await orch.current_state()
    assert state["state"] == STATE_IDLE
    assert state["experiment_id"] is None


@pytest.mark.asyncio
async def test_boot_reconcile_forces_idle_and_disarms() -> None:
    """
    A process that died mid-run boots to a defined, safe state: IDLE, every actuator disarmed.

    The hazard this closes: a crash left ``system_state`` saying ``ACTIVE_EXPERIMENT`` and a
    node that might still be armed. On the next boot neither is true any more, so the gateway
    reconciles instead of resumes -- it clears the run, persists IDLE, and stops every role so
    the console opens on a state that is both defined and safe (the ODrive ends disarmed).
    """
    orch, gw = _make()
    # Simulate a run that was in flight when the process died.
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    assert (await orch.current_state())["experiment_id"] == "EXP-2026-10-10-01"
    gw.commands.calls.clear()

    await orch.reconcile_on_boot()

    state = await orch.current_state()
    assert state["state"] == STATE_IDLE, "boot did not reconcile to IDLE"
    assert state["experiment_id"] is None
    # Every role was told to stop, so nothing is left armed across the restart.
    actions = gw.commands.actions()
    assert ("aerator", "stop") in actions
    assert ("mixer", "stop_mixer") in actions
    assert ("pump", "force_off") in actions
    # And IDLE was persisted, not just held in memory.
    assert any("system_state" in q for q, _ in gw.db.executed)


@pytest.mark.asyncio
async def test_beginning_a_run_enters_the_first_phase_and_issues_its_commands() -> None:
    orch, gw = _make()
    out = await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")

    assert out["state"] == STATE_ACTIVE
    assert out["recipe_id"] == "planta_2_step"
    # planta_2_step's first phase stops the aerator and waits.
    assert ("aerator", "stop") in gw.commands.actions()
    # The experiment row was marked active.
    assert any("UPDATE experiments" in q for q, _ in gw.db.executed)
    # And an EXPERIMENT_STARTED event was emitted.
    assert any(getattr(e, "event_type", None).value == "experiment_started" for e in gw.events.emitted)


@pytest.mark.asyncio
async def test_activating_without_an_experiment_is_refused() -> None:
    orch, _ = _make()
    with pytest.raises(ValueError, match="experiment_id"):
        await orch.request_state(state=STATE_ACTIVE, experiment_id=None)


@pytest.mark.asyncio
async def test_pre_launch_mode_in_the_phase_row_seeds_the_run() -> None:
    """
    A mode set before launch survives to the run.

    The bug: the orchestrator reset ``_mode_overrides`` to ``{}`` on start and read only
    ``phase_id, status`` back from ``experiment_phases``, so a mode the operator chose
    pre-launch was discarded the moment the run began ("no podía editar un experimento antes
    de lanzarlo"). The row is the durable plan; ``_begin`` must honour it.

    ``planta_2_step``'s first phase is ``p2_precond`` (hibrido in the recipe). Persisting
    ``manual`` must make the run start in manual, and leave the untouched phases at their
    recipe default so the override stays a sparse diff.
    """
    orch, gw = _make(metrics={"od_sensor": {"od_do_mg_l": 7.0}})
    # The phase row as the create route would have written it: the operator picked `manual`
    # for p2_precond. p2_step keeps the recipe default (`hibrido`), so it is not an override.
    gw.db.phase_rows = [
        {"phase_id": "p2_precond", "mode": "manual", "status": "pending"},
        {"phase_id": "p2_step", "mode": "hibrido", "status": "pending"},
    ]

    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")

    state = await orch.current_state()
    modes = {p["phase_id"]: p["mode"] for p in state["phases"]}
    assert modes["p2_precond"] == "manual", "the pre-launch choice did not reach the run"
    # p2_step's row matches the recipe default (hibrido), so it is not carried as an override.
    assert "p2_step" not in orch._mode_overrides


@pytest.mark.asyncio
async def test_set_phase_mode_before_launch_persists_to_the_row() -> None:
    """
    Setting a mode for a not-yet-started experiment writes to ``experiment_phases``.

    Without a run, there is no active experiment to mirror in memory, so the write must be an
    ``UPDATE`` against the named experiment -- that is the only place the choice can live until
    the run reads it back.
    """
    orch, gw = _make()

    await orch.set_phase_mode(phase_id="p2_precond", mode="manual", experiment_id="EXP-2026-10-10-01")

    updates = [(q, a) for q, a in gw.db.executed if "experiment_phases" in q]
    assert updates, "the pre-launch mode was not persisted"
    query, args = updates[-1]
    assert "UPDATE experiment_phases" in query
    assert args == ("EXP-2026-10-10-01", "p2_precond", "manual")
    # No run yet, so nothing is mirrored in memory.
    assert orch._mode_overrides == {}


@pytest.mark.asyncio
async def test_stop_disarms_before_flipping_status() -> None:
    """
    Ordering is the safety property: an actuator must be told to stop *before* the run is
    marked completed, or a completed run can leave a motor spinning.
    """
    orch, gw = _make()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    gw.commands.calls.clear()

    await orch.request_state(state=STATE_IDLE, experiment_id=None)

    actions = gw.commands.actions()
    assert ("aerator", "stop") in actions
    assert ("mixer", "stop_mixer") in actions
    assert ("pump", "force_off") in actions
    # The disarms were issued before the status update to 'completed'.
    completed_marks = [
        i for i, (q, a) in enumerate(gw.db.executed) if "UPDATE experiments" in q and a[1] == "completed"
    ]
    assert completed_marks, "experiment was never marked completed"


@pytest.mark.asyncio
async def test_auto_phase_advances_when_its_condition_is_satisfied() -> None:
    """
    Drive the first phase of planta_2_step to completion: it exits when DO < 3.5 with the
    min-elapsed gate met. Feed a live snapshot that satisfies it and tick.
    """
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    assert orch.current_state  # sanity

    # Force the phase's entry clock into the past so the min-elapsed gate is satisfied.
    orch._phase_entered_s = time.time() - 120

    first_phase = orch.active_phase
    assert first_phase is not None and first_phase.phase_id == "p2_precond"

    await orch._tick()

    # The HIBRIDO default on p2_precond means it should be *awaiting confirmation*, not
    # advanced. Confirm, and it should move to p2_step.
    state = await orch.current_state()
    assert state["awaiting_confirm"] == "p2_precond"

    await orch.confirm_phase()
    state = await orch.current_state()
    assert state["phase_id"] == "p2_step"


@pytest.mark.asyncio
async def test_a_supervised_phase_without_a_timeout_waits_indefinitely() -> None:
    """
    A supervised phase that declares no timeout never advances on its own.

    This replaces `test_timeout_advances_a_hibrido_phase_without_the_operator`, whose premise
    no longer exists: hibrido phases used to carry a `timeout_s` and the engine used it, so a
    walked-away operator still moved the run. `manual` and `hibrido` now wait for a human by
    design -- the operator is the clock -- and `timeout_s=None` is how a phase says so.

    Pushing the clock arbitrarily far must not move it. The exit is `confirm` (asserted by
    `test_a_prompt_is_still_the_operator_s_to_answer`) or stopping the run.
    """
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 7.0}})  # condition will never fire
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")

    phase = orch.active_phase
    assert phase is not None
    assert phase.mode == "hibrido" and phase.timeout_s is None, (
        "this test is about a supervised phase with no clock; the recipe changed"
    )

    # Push the clock absurdly far -- there is no timeout to pass, so nothing may happen.
    orch._phase_entered_s = time.time() - 86_400
    await orch._tick()

    state = await orch.current_state()
    assert state["phase_id"] == "p2_precond", (
        "a supervised phase advanced on its own -- if this is intended, `timeout_s` for "
        "manual/hibrido has to come back and this test deleted"
    )


async def test_an_awaiting_confirm_prompt_holds_until_answered() -> None:
    """
    An unanswered prompt on a supervised phase holds the run until the operator answers.

    This replaces `test_a_hibrido_phase_awaiting_confirmation_times_out`, which guarded a real
    bug -- `_tick` returned early whenever `_awaiting_confirm` matched, *before*
    computing `timed_out`, so the stricter-sounding mode was the one with no way out while
    `manual` always released. The fix was to consult the clock first. That fix is now moot for
    supervised phases because they declare no clock at all: `manual` and `hibrido` wait for a
    human by design, so the prompt is not a hold that needs rescuing, it is the phase doing its
    job. The bug's actual lesson survives in the engine -- the `awaiting_confirm` branch still
    checks `phase.timeout_s is None` before deciding whether a clock exists -- and is covered
    by `test_an_auto_phase_without_a_timeout_is_rejected`, which pins that the only phases
    without a clock are supervised ones.

    What this test pins now: a satisfied condition on a supervised phase raises the prompt and
    nothing else moves it. Arbitrarily large elapsed time included.
    """
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="hibrido")

    phase = orch.active_phase
    assert phase is not None

    # Satisfy the condition: p2_precond waits for `DO < 3.5 && elapsed >= 60`, so 120 s of
    # elapsed time fires it. The phase raises the prompt rather than advancing.
    orch._phase_entered_s = time.time() - 120
    await orch._tick()
    held = await orch.current_state()
    assert held["phase_id"] == "p2_precond", "expected the phase to hold, not advance"
    assert held["awaiting_confirm"] == "p2_precond", "the prompt was not raised"

    # An unanswered prompt is not a stall, it is the mode: no amount of elapsed time releases
    # it. This is the half that used to assert the opposite.
    orch._phase_entered_s = time.time() - 86_400
    await orch._tick()
    after = await orch.current_state()
    assert after["phase_id"] == "p2_precond", (
        "an awaiting_confirm phase on a supervised run advanced on its own; supervised modes "
        "are released by their operator, not by a clock"
    )
    assert after["awaiting_confirm"] == "p2_precond", "the pending prompt disappeared"


@pytest.mark.asyncio
async def test_a_prompt_is_still_the_operator_s_to_answer() -> None:
    """
    `confirm` is the exit a supervised prompt exists for -- there is no timeout behind it.

    Paired with the test above so the pair pins both halves: the prompt holds indefinitely,
    and only an explicit operator decision releases it.
    """
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="hibrido")
    orch._phase_entered_s = time.time() - 120

    await orch._tick()
    assert (await orch.current_state())["awaiting_confirm"] == "p2_precond"

    await orch.confirm_phase(requested_by="operator:test")
    after = await orch.current_state()
    assert after["phase_id"] != "p2_precond", "confirm did not release the hold"
    assert after["awaiting_confirm"] is None, "confirm left a pending prompt behind"


#: The contract, as a table: `mode -> situation -> what the engine must do`.
#:
#:        ADVANCE  the phase completes
#:        HOLD     the phase raises `awaiting_confirm` and waits
#:        STAY     the phase does nothing and keeps its clock running
#:
#: Each situation lines up with a distinct piece of the engine, and the interesting cases are
#: the ones where two modes differ:
#:
#:   condition_met     the condition fires, clock well under the timeout. Here `auto` advances
#:                     while `hibrido` holds and `manual` ignores the condition outright --
#:                     the whole point of the three modes.
#:   timeout_only      the condition never fires and the clock runs out. `auto` must advance:
#:                     its condition is the only other exit and nobody is watching, so without
#:                     the timeout a dead sensor holds the run forever. `hibrido` does not move
#:                     and raises no prompt -- it holds only when there is something to hold
#:                     *for*, and an unmet condition is nothing yet -- and `manual` does not
#:                     move either. Neither declares a timeout, so there is nothing to run out.
#:                     See `test_every_phase_that_advances_itself_has_a_timeout`.
#:   both              the condition fires as the clock expires. `auto` advances on the
#:                     timeout; supervised modes see no timeout, so this is `condition_met`
#:                     with a wasted clock.
#:   operator_confirm  the operator presses "Forzar Avance". An explicit human decision
#:                     releases all three, which is what distinguishes it from the condition.
#:
#: HOLD and STAY are distinct on purpose: HOLD means the operator is being asked something
#: (`awaiting_confirm` is set), STAY means the phase simply did not move. A hibrido phase with
#: an unmet condition is STAY -- there is no decision to put to anyone yet.
_MODE_CONTRACT = {
    ("auto", "condition_met"): "ADVANCE",
    ("auto", "timeout_only"): "ADVANCE",
    ("auto", "both"): "ADVANCE",
    ("auto", "operator_confirm"): "ADVANCE",
    ("hibrido", "condition_met"): "HOLD",
    ("hibrido", "timeout_only"): "STAY",
    ("hibrido", "both"): "HOLD",
    ("hibrido", "operator_confirm"): "ADVANCE",
    ("manual", "condition_met"): "STAY",
    ("manual", "timeout_only"): "STAY",
    ("manual", "both"): "STAY",
    ("manual", "operator_confirm"): "ADVANCE",
}

#: Whether the phase's condition should be satisfied to exercise each situation. The ones
#: named for the clock deliberately leave it unmet, so "advanced on the timeout" cannot be
#: confused with "advanced because the condition fired".
_SITUATION_SATISFIES = {
    "condition_met": True,
    "timeout_only": False,
    "both": True,
    "operator_confirm": True,
}


async def _observe(mode: str, situation: str) -> str:
    """Drive one cell of the contract and report ADVANCE / HOLD / STAY."""
    do = 2.0 if _SITUATION_SATISFIES[situation] else 7.0  # p2_precond wants DO < 3.5
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": do}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode=mode)

    phase = orch.active_phase
    assert phase is not None
    before = phase.phase_id

    if situation in ("timeout_only", "both"):
        if mode == "auto":
            # `set_phase_mode` changes the mode, not the timeout: the phase keeps the recipe's
            # `timeout_s`, which for `p2_precond` is `None` because it ships as supervised.
            # An auto phase *must* have a clock (`validate_recipes` enforces it), so stage one
            # -- this is the cell asserting that an auto phase does release, which is only
            # meaningful with a clock to release on.
            orch._mode_overrides.pop(before, None)
            patched = replace(phase, mode="auto", timeout_s=300)
            _patch_phase(orch, patched)
            orch._mode_overrides[before] = "auto"
            orch._phase_entered_s = _past_the_clock(patched, mode="auto")
        else:
            # Supervised modes declare no timeout, so "the clock ran out" cannot be staged for
            # them -- their row says they do not move, and elapsed time is the only lever.
            # Push it absurdly far so the row is asserted rather than skipped.
            orch._phase_entered_s = time.time() - 86_400
    else:
        orch._phase_entered_s = time.time() - 120  # past the condition gate, under timeout

    await orch._tick()
    if situation == "operator_confirm":
        await orch.confirm_phase(requested_by="operator:test")

    state = await orch.current_state()
    if state["phase_id"] != before:
        return "ADVANCE"
    if state["awaiting_confirm"] == before:
        return "HOLD"
    return "STAY"


@pytest.mark.parametrize(("mode", "situation"), sorted(_MODE_CONTRACT))
async def test_the_mode_contract(mode: str, situation: str) -> None:
    """
    Every (mode, situation) cell behaves as documented.

    The modes are described in prose in several places and those descriptions had drifted from
    the code: the early return for an awaiting phase meant a prompt could never time out, so
    `hibrido` -- the mode where the system is *asking* the operator for something -- was the
    one with no way out, while `manual` always released. Asserting the whole table in one place
    makes that class of drift visible as a diff rather than as a run that hangs.
    """
    observed = await _observe(mode, situation)
    assert observed == _MODE_CONTRACT[(mode, situation)], (
        f"{mode} in {situation}: expected {_MODE_CONTRACT[(mode, situation)]}, observed "
        f"{observed}. If the new behaviour is right, change the table -- it is the contract."
    )


@pytest.mark.parametrize("mode", ["hibrido", "manual"])
async def test_a_supervised_prompt_waits_for_its_operator(mode: str) -> None:
    """
    No amount of elapsed time releases a supervised phase -- with or without a prompt.

    This replaces `test_every_mode_releases_on_its_timeout_while_a_prompt_is_up`, which
    asserted the opposite. The old test guarded a real bug: `_tick` returned early for an
    `awaiting_confirm` phase *before* computing `timed_out`, so `hibrido` -- the mode that
    asks the operator for something -- was the only one that could never time out. Making
    supervised phases wait by design removes the clock entirely, so the bug cannot recur: the
    `awaiting_confirm` branch now asks `phase.timeout_s is None` first, and the only phases
    with `None` are supervised ones (`test_an_auto_phase_without_a_timeout_is_rejected`).

    What remains worth pinning: a satisfied condition on a supervised phase raises the prompt
    (hibrido) or does nothing (manual), and a day of elapsed time changes neither.
    """
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode=mode)
    phase = orch.active_phase
    assert phase is not None
    assert phase.timeout_s is None, "this test asserts a supervised phase has no clock; the recipe changed"

    # Satisfy the condition: p2_precond wants `DO < 3.5 && elapsed >= 60`, so 120 s fires it.
    orch._phase_entered_s = time.time() - 120
    await orch._tick()

    expected_hold = "p2_precond" if mode == "hibrido" else None
    assert (await orch.current_state())["awaiting_confirm"] == expected_hold, (
        f"{mode} should {'hold with a prompt' if expected_hold else 'not prompt at all'}"
    )

    # A day later, still nothing. This is the half that used to assert the opposite.
    orch._phase_entered_s = time.time() - 86_400
    await orch._tick()
    after = await orch.current_state()
    assert after["phase_id"] == "p2_precond", (
        f"{mode} advanced on its own; a supervised phase is released by its operator"
    )
    assert after["awaiting_confirm"] == expected_hold, "the pending prompt changed on its own"


@pytest.mark.asyncio
async def test_manual_override_is_a_valid_suspended_state() -> None:
    orch, _ = _make()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.request_state(state=STATE_MANUAL, experiment_id=None)
    # The engine keeps the run but reports the override; state stays valid.
    assert STATE_MANUAL in VALID_STATES


@pytest.mark.asyncio
async def test_phase_mode_can_be_overridden_per_run() -> None:
    orch, _ = _make()
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="manual")
    state = await orch.current_state()
    active = next(p for p in state["phases"] if p["phase_id"] == "p2_precond")
    assert active["mode"] == "manual"


@pytest.mark.asyncio
async def test_an_unknown_state_is_rejected() -> None:
    orch, _ = _make()
    with pytest.raises(ValueError, match="unknown state"):
        await orch.request_state(state="TELEPORT", experiment_id="X")


@pytest.mark.asyncio
async def test_the_last_phase_completing_ends_the_run() -> None:
    """Walking off the end of a recipe completes it rather than raising."""
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 7.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    recipe = RECIPES["planta_2_step"]
    last = recipe.phases[-1]
    orch._phase_index = len(recipe.phases) - 1

    # The last phase is supervised (`p2_analysis`, manual, no timeout), so it cannot be
    # satisfied by a clock -- there is none. Its condition gates on elapsed time, so back-date
    # the entry. With the condition met, `confirm_phase` is the operator's advance.
    assert last.timeout_s is None, "the last phase now has a clock; this test can use it"
    orch._phase_entered_s = time.time() - 86_400
    await orch._tick()
    await orch.confirm_phase(requested_by="operator:test")

    state = await orch.current_state()
    assert state["state"] == STATE_IDLE


# ---------------------------------------------------------------------------
# The three modes — the behaviour that distinguishes them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_phase_advances_without_an_operator() -> None:
    """AUTO: a satisfied condition advances the phase on its own, no confirmation."""
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="auto")
    orch._phase_entered_s = time.time() - 120

    await orch._tick()

    state = await orch.current_state()
    # Advanced straight past the phase; nothing is awaiting confirmation.
    assert state["phase_id"] == "p2_step"
    assert state["awaiting_confirm"] is None


@pytest.mark.asyncio
async def test_manual_phase_does_not_auto_advance_on_condition() -> None:
    """
    MANUAL: the condition being satisfied is *not* an advance. This is the whole point of
    manual control — the operator fires each actuator and decides when the phase is over.
    """
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="manual")
    orch._phase_entered_s = time.time() - 120  # condition satisfied, well under timeout

    await orch._tick()

    state = await orch.current_state()
    # Still on the same phase — manual does not obey the threshold.
    assert state["phase_id"] == "p2_precond"
    assert state["awaiting_confirm"] is None


@pytest.mark.asyncio
async def test_manual_phase_advances_when_the_operator_forces_it() -> None:
    """MANUAL "Forzar Avance": confirm_phase is the operator's explicit advance."""
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="manual")
    orch._phase_entered_s = time.time() - 120

    await orch._tick()
    assert (await orch.current_state())["phase_id"] == "p2_precond"

    await orch.confirm_phase()
    assert (await orch.current_state())["phase_id"] == "p2_step"


@pytest.mark.asyncio
async def test_manual_phase_waits_for_the_operator_not_the_clock() -> None:
    """
    MANUAL has no safety valve because it has no clock: an abandoned bench waits.

    This replaces `test_manual_phase_still_releases_on_timeout`, which asserted the old policy.
    The trade is deliberate and belongs to the operator: `manual` means a human is driving, so
    a number that releases the phase on its own would take the run away from the person who
    asked to hold it. The exits are `confirm` and stopping the run.
    """
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="manual")
    phase = orch.active_phase
    assert phase is not None
    assert phase.timeout_s is None, "manual phases must declare no timeout; the recipe changed"

    # Never mind the condition -- 2.0 satisfies it. No amount of time moves a manual phase.
    orch._phase_entered_s = time.time() - 86_400
    await orch._tick()

    assert (await orch.current_state())["phase_id"] == "p2_precond", (
        "a manual phase released on its own; manual means the operator is the clock"
    )


@pytest.mark.asyncio
async def test_manual_phase_holds_its_commands_until_the_operator_fires_them() -> None:
    """
    A MANUAL phase does not issue its entry commands on entry; it holds them for "Disparar
    Acción".

    The bug this pins: `_enter_phase` issued every phase's commands unconditionally, so a
    manual phase fired its actuators the instant it was entered -- the automatic action the
    mode exists to prevent ("control directo"). The operator saw commands they never asked for.
    Now the entry commands wait for `trigger_phase_action`, and fire exactly once.
    """
    orch, gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    # p2_precond's entry command is `aerator stop`; the run already issued it as the *first*
    # phase under its default mode, so clear the spy and re-enter under manual to isolate.
    phase = orch.active_phase
    assert phase is not None and phase.commands, "p2_precond must carry an entry command"
    await orch.set_phase_mode(phase_id="p2_precond", mode="manual")
    gw.commands.calls.clear()

    # Re-entering under manual must hold, not fire.
    await orch._enter_phase(phase, requested_by="operator:test")
    assert gw.commands.actions() == [], "a manual phase issued its commands on entry"
    state = await orch.current_state()
    assert state["action_pending"] == "p2_precond"

    # The operator fires them.
    await orch.trigger_phase_action(requested_by="operator:test")
    assert ("aerator", "stop") in gw.commands.actions(), "Disparar Acción did not fire the phase's command"
    assert (await orch.current_state())["action_pending"] is None

    # Firing again is a no-op: the action runs once per entry, not once per click.
    gw.commands.calls.clear()
    await orch.trigger_phase_action(requested_by="operator:test")
    assert gw.commands.actions() == [], "a second Disparar Acción re-issued the setpoint"


@pytest.mark.asyncio
async def test_auto_phase_still_issues_its_commands_on_entry() -> None:
    """The hold is MANUAL-only: an AUTO phase issues on entry as before, with nothing pending."""
    orch, gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    # p2_precond defaults to hibrido (issues on entry); force auto and re-enter.
    phase = orch.active_phase
    assert phase is not None and phase.commands
    await orch.set_phase_mode(phase_id="p2_precond", mode="auto")
    gw.commands.calls.clear()

    await orch._enter_phase(phase, requested_by="operator:test")

    assert ("aerator", "stop") in gw.commands.actions(), "an auto phase stopped issuing on entry"
    assert (await orch.current_state())["action_pending"] is None


@pytest.mark.asyncio
async def test_hibrido_phase_waits_for_confirmation_when_condition_met() -> None:
    """HIBRIDO: condition met holds for the operator instead of advancing."""
    orch, _gw = _make(metrics={"od_sensor": {"od_do_mg_l": 2.0}})
    await orch.request_state(state=STATE_ACTIVE, experiment_id="EXP-2026-10-10-01")
    await orch.set_phase_mode(phase_id="p2_precond", mode="hibrido")
    orch._phase_entered_s = time.time() - 120

    await orch._tick()

    state = await orch.current_state()
    assert state["phase_id"] == "p2_precond"
    assert state["awaiting_confirm"] == "p2_precond"
