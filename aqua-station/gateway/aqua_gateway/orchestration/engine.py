"""
The orchestrator.

Holds the run's place in a recipe and moves it forward. It is a *client* of the command
outbox, never a second command path: every actuator change it makes is an ordinary
``commands.issue(...)`` call, so it inherits persistence-before-publish, atomic
supersession and the safety-action fast path for free. If the orchestrator is wrong, the
worst it can do is issue a bad setpoint — which the existing outbox machinery already
records, deadline-bounds and lets a node refuse.

Two invariants make it safe to run unattended:

* **One writer.** Only the engine advances a run. The API route delegates to it rather than
  flipping ``experiments.status`` itself, so there is never a race between a button and a tick.
* **Every phase is timeout-bounded.** A condition that never fires cannot hold the run open:
  ``_tick`` checks the timeout on every pass. ``recipes.validate_recipes`` guarantees the
  timeout exists.

The split it implements is the project's stated architecture: the gateway sequences and
issues abstract setpoints, the nodes run their own control loops to satisfy them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..events.model import Event, EventType, Scope, Source
from .conditions import PhaseContext, evaluate, least_squares_quadratic_per_min, least_squares_rate_per_min
from .kla_metrics import compute_kla_metrics
from ..version import gateway_version, recipe_digest
from .recipes import RECIPES, Phase

if TYPE_CHECKING:  # pragma: no cover
    from ..main import Gateway

log = logging.getLogger("aqua.orchestration")


def _guard_overrides(phase: Phase | None, params: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Split operator overrides into ``(kept, rejected)``.

    A phase that pauses on timeout (the certified-base verification) uses its timeout as the
    time the chemistry is given to finish, not as a safety stop: shortening it only turns a
    valid run into a paused one. An override below the recipe's timeout is therefore rejected
    (and reported in the phase-entered event), whatever client sent it.
    """
    kept, rejected = dict(params), {}
    if phase is not None and phase.on_timeout == "pause" and "safety_timeout_s" in kept:
        try:
            too_short = float(kept["safety_timeout_s"]) < float(phase.timeout_s or 0)
        except (TypeError, ValueError):
            too_short = True
        if too_short:
            rejected["safety_timeout_s"] = kept.pop("safety_timeout_s")
    return kept, rejected

#: The three global states, matching the ``system_state`` JSONB in ``001_core.sql``.
STATE_IDLE = "IDLE"
STATE_ACTIVE = "ACTIVE_EXPERIMENT"
STATE_MANUAL = "MANUAL_OVERRIDE"

VALID_STATES = frozenset({STATE_IDLE, STATE_ACTIVE, STATE_MANUAL})

#: How fast the engine re-evaluates the active phase. The conditions are cheap; the cost is
#: the database read of the experiment row, so this is not 1 Hz. A 2 s cadence keeps a
#: phase transition feeling immediate without polling the database 5 times a second.
TICK_INTERVAL_S = 2.0

#: How many recent DO points the trend window keeps. Bounds memory on a long run; the DO
#: trend only ever looks back a couple of minutes.
_DO_HISTORY_MAX = 400


class Orchestrator:
    """Sequence a run through a recipe, issuing setpoints and recording transitions."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        #: ``experiment_id`` of the active run, or ``None``.
        self._experiment_id: str | None = None
        #: ``recipe_id`` of the active run.
        self._recipe_id: str | None = None
        #: Index into ``recipe.phases`` of the phase currently running.
        self._phase_index: int = 0
        #: Per-phase operator mode overrides, keyed by ``phase_id``.
        self._mode_overrides: dict[str, str] = {}
        #: Per-phase operator parameter overrides (e.g. mixer_rpm, step_rpm), keyed by ``phase_id``.
        self._param_overrides: dict[str, dict[str, Any]] = {}
        #: Overrides refused by ``_guard_overrides``, per phase, reported when the phase is entered.
        self._rejected_overrides: dict[str, dict[str, Any]] = {}
        #: ``phase_id`` values already marked completed this run.
        self._completed: set[str] = set()
        #: ``phase_id`` awaiting an operator's visto bueno (HIBRIDO).
        self._awaiting_confirm: str | None = None
        #: ``phase_id`` whose entry commands are still waiting for the operator to fire them.
        #: Set on entering a MANUAL phase and cleared when the operator triggers it (or the
        #: phase advances). In AUTO/HIBRIDO the commands are issued on entry and this stays
        #: ``None``. Without it, a MANUAL phase's commands fired the instant the phase was
        #: entered -- the opposite of "control directo" -- which is what the operator saw.
        self._action_pending: str | None = None
        #: Recent ``(t_dev_ms, do_mg_l)``, oldest first.
        self._do_history: list[tuple[int, float]] = []

        #: Accumulated samples for control benchmark KPI evaluation during abc_run
        self._abc_samples: list[dict[str, float]] = []
        self._last_drv_power_w: float = 0.0
        self._last_control_kpis: dict[str, Any] | None = None

        #: Samples accumulated during reaeration step (abk_acq, pd_acq, p2_acq) for ASCE 2-22 KLa fit
        self._kla_samples: list[dict[str, float]] = []
        self._kla_temps: list[float] = []
        self._last_kla_metrics: dict[str, Any] | None = None

        #: Adaptive pulse control state during residual sulfite verification (abk_verify, abc_verify)
        self._pulse_active: bool = False
        self._pulse_start_s: float | None = None
        self._last_pulse_end_s: float | None = None
        self._pulse_trigger_was_low_do: bool = False
        self._pulse_count: int = 0
        #: Last aerator speed and water temperature seen, for the initial-condition record.
        self._last_drv_rpm: float | None = None
        self._last_od_temp_c: float | None = None
        #: Initial condition captured when the loop/step is armed (abc_arm, abk_step), with its
        #: conformity verdict against the declared base and tolerance.
        self._initial_condition: dict[str, Any] | None = None

        #: True while the run is paused. Pause holds the timer and stops phase advancement;
        #: it deliberately does NOT disarm the actuators. Whether a paused run should also
        #: stop the physical loop is a separate decision, and conflating the two would make
        #: pause and stop the same thing.
        self._paused = False
        #: Total seconds spent paused, subtracted from the relative timer so ``t-t0`` measures
        #: run time rather than wall time. Without this a two-minute interruption would look
        #: like two minutes of experiment.
        self._paused_accum_s = 0.0
        #: Wall clock when the current pause began, or ``None`` when running.
        self._paused_at_s: float | None = None

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.transitions = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._tick_forever(), name="orchestrator")

    async def reconcile_on_boot(self) -> None:
        """
        Force a defined, safe state at gateway startup: IDLE, no run, actuators disarmed.

        A process that died mid-run leaves ``system_state`` saying ``ACTIVE_EXPERIMENT`` and a
        node that may still be armed. On the next boot neither is true any more: this process
        has no experiment in flight, and an actuator that outlived its supervisor is a hazard,
        not a run. So the boot reconciles rather than resumes -- it clears the in-memory run,
        persists IDLE, and issues a stop to every role, so the console opens on a state that is
        both defined and safe (the ODrive ends disarmed, not spinning). Best-effort per role:
        a node that is offline must not stop the gateway from coming up.
        """
        self._experiment_id = None
        self._recipe_id = None
        self._phase_index = 0
        self._completed = set()
        self._awaiting_confirm = None
        self._action_pending = None
        self._mode_overrides = {}
        self._param_overrides = {}
        self._abc_samples = []
        self._last_drv_power_w = 0.0
        self._last_control_kpis = None
        self._initial_condition = None
        self._kla_samples = []
        self._kla_temps = []
        self._last_kla_metrics = None
        self._pulse_active = False
        self._pulse_start_s = None
        self._last_pulse_end_s = None
        self._pulse_trigger_was_low_do = False
        self._paused = False
        self._paused_accum_s = 0.0
        self._paused_at_s = None
        # Disarm every role the system knows, not just this run's: at boot there is no run, and
        # an actuator left armed by a crashed process is exactly what this call exists to clear.
        # Wait (bounded) for the outbox's MQTT publisher first: `commands.issue` marks a command
        # `failed` -- permanently -- when the link is down, and a stop that dies because the boot
        # race was lost is a motor still spinning. Called after `ingest.start()`, so this is only
        # a guard against a slow link, not the ordering fix itself.
        for _ in range(50):  # ~5 s ceiling
            if getattr(self.gateway.commands, "publisher", None) is not None:
                break
            await asyncio.sleep(0.1)
        else:
            log.warning("boot reconcile: MQTT publisher not bound after 5 s; disarms may fail")
        for role, action in (
            ("aerator", "stop"),
            ("mixer", "stop_mixer"),
            ("pump", "force_off"),
        ):
            try:
                await self.gateway.commands.issue(
                    role=role,
                    action=action,
                    target={},
                    experiment_id=None,
                    requested_by="gateway:boot",
                    deadline_s=30,
                )
            except Exception:
                log.warning("boot disarm %s/%s failed", role, action, exc_info=True)
        await self._persist_state(STATE_IDLE)
        log.info("boot reconcile: state=IDLE, actuators disarmed")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -- inputs ------------------------------------------------------------

    def observe(self, *, role: str, row: Mapping[str, Any], t_dev_ms: int) -> None:
        """
        Fold one sample into the orchestrator's view.

        Called from the MQTT ingest loop. Keeps the bounded DO history and captures
        aerator electrical telemetry for benchmark KPI integration.
        """
        if role == "aerator":
            p = row.get("drv_power_w")
            if p is not None:
                self._last_drv_power_w = float(p)
            rpm = row.get("drv_rpm")
            if rpm is not None:
                self._last_drv_rpm = float(rpm)
            return

        if role != "od_sensor":
            return
        value = row.get("od_do_mg_l")
        if value is None:
            return
        do_f = float(value)
        temp = row.get("od_temp_c")
        if temp is not None:
            self._last_od_temp_c = float(temp)
        self._do_history.append((t_dev_ms, do_f))
        if len(self._do_history) > _DO_HISTORY_MAX:
            del self._do_history[: len(self._do_history) - _DO_HISTORY_MAX]

        # Record control benchmark sample during abc_run
        phase = self.active_phase
        if phase and phase.phase_id == "abc_run":
            now = time.time()
            t_rel = now - getattr(self, "_phase_entered_s", now)
            self._abc_samples.append({
                "t_s": max(0.0, t_rel),
                "do_mg_l": do_f,
                "drv_power_w": self._last_drv_power_w,
            })

        # Record reaeration samples during acquisition phases for ASCE 2-22 KLa fit
        if phase and phase.phase_id in ("abk_acq", "pd_acq", "p2_acq"):
            now = time.time()
            t_rel = now - getattr(self, "_phase_entered_s", now)
            self._kla_samples.append({
                "t_s": max(0.0, t_rel),
                "do_mg_l": do_f,
            })
            temp_val = row.get("od_temp_c")
            if temp_val is not None:
                self._kla_temps.append(float(temp_val))

    # -- state machine -----------------------------------------------------

    @property
    def state(self) -> str:
        if self._experiment_id is None:
            return STATE_IDLE
        return STATE_ACTIVE

    async def current_state(self) -> dict[str, Any]:
        """
        What the dashboard shows. Reads ``system_state`` from settings, which is the
        durable record; the in-memory fields are the engine's own fast path.
        """
        return {
            "state": self.state,
            "paused": self._paused,
            "experiment_id": self._experiment_id,
            "recipe_id": self._recipe_id,
            "phase_id": self.active_phase.phase_id if self.active_phase else None,
            "phase_num": self.active_phase.num if self.active_phase else None,
            "awaiting_confirm": self._awaiting_confirm,
            "action_pending": self._action_pending,
            "started_at": self._started_at_iso,
            "t_rel_s": self._t_rel_s(),
            "phases": self._phase_views(),
            "control_kpis": self._last_control_kpis,
            "kla_metrics": self._last_kla_metrics,
            "initial_condition": self._initial_condition,
        }

    @property
    def active_phase(self) -> Phase | None:
        if self._recipe_id is None:
            return None
        recipe = RECIPES.get(self._recipe_id)
        if recipe is None or self._phase_index >= len(recipe.phases):
            return None
        return recipe.phases[self._phase_index]

    # -- transitions -------------------------------------------------------

    async def request_state(
        self,
        *,
        state: str,
        experiment_id: str | None,
        requested_by: str = "operator:web",
    ) -> dict[str, Any]:
        """
        Handle ``POST /api/state``.

        IDLE→ACTIVE starts (or resumes) a run; ACTIVE→IDLE stops it; MANUAL_OVERRIDE
        suspends phase advancement without stopping the physical loop. The route returns
        whatever this returns.
        """
        state = state.upper()
        if state not in VALID_STATES:
            raise ValueError(f"unknown state {state!r}; valid: {sorted(VALID_STATES)}")

        if state == STATE_ACTIVE:
            await self._begin(experiment_id=experiment_id, requested_by=requested_by)
        elif state == STATE_IDLE:
            await self._end(requested_by=requested_by)
        elif state == STATE_MANUAL:
            # Suspend sequencing; leave the loop running. This is supervisory only by
            # design — see the lifecycle plan.
            await self._emit_state_change(previous=self.state, new=STATE_MANUAL, requested_by=requested_by)

        return await self.current_state()

    async def confirm_phase(self, *, requested_by: str = "operator:web") -> dict[str, Any]:
        """
        Operator's visto bueno: advance the active phase on the operator's say-so.

        Two cases reach here, and both are the operator overriding the clock:

        * A HIBRIDO phase whose condition is met and which is waiting for confirmation.
        * A MANUAL phase, where the operator's "Forzar Avance" is the only way past a phase
          whose condition fired but which manual mode deliberately refuses to auto-advance.

        A no-op when there is nothing to advance, so a double-click is harmless.
        """
        phase = self.active_phase
        if phase is None:
            return await self.current_state()
        mode = self._mode_overrides.get(phase.phase_id, phase.mode)
        # Only advance if a confirm is actually pending (HIBRIDO) or the phase is manual
        # (the operator's explicit advance). Anything else is a stray click.
        if self._awaiting_confirm != phase.phase_id and mode != "manual":
            return await self.current_state()
        await self._advance(phase, requested_by=requested_by, reason="operator_confirmed")
        return await self.current_state()

    async def set_phase_mode(
        self,
        *,
        phase_id: str,
        mode: str,
        experiment_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Operator overrides a phase's mode (auto/hibrido/manual).

        Two cases reach here, and the difference is *when* the choice is made:

        * **During a run** — the active experiment is the target, and the override rides in
          memory (``_mode_overrides``) and is persisted alongside the phase row.
        * **Before launch** — the operator prepares the plan for an experiment that has not
          started. The override is written to ``experiment_phases.mode`` so it survives until
          the run begins; ``_begin`` reads it back rather than resetting to the recipe default.
          This is the case the UI previously disabled outright ("no podía editar un experimento
          antes de lanzarlo").

        The persisted choice is always the source of truth: a pre-launch override is written to
        the row, and the in-memory map only ever mirrors the active run.
        """
        if mode not in ("auto", "hibrido", "manual"):
            raise ValueError(f"bad mode {mode!r}")

        # Target: an explicit experiment (pre-launch) or the active run. Writing to a row
        # requires a database; a missing one is not an error here, only a lost persistence --
        # the in-memory override still holds for the session.
        target_experiment = experiment_id or self._experiment_id

        # The active run is mirrored in memory too, because `_tick` reads it on every pass and
        # a database round-trip per tick is the one cost this engine deliberately avoids.
        if target_experiment is not None and target_experiment == self._experiment_id:
            self._mode_overrides[phase_id] = mode

        if target_experiment is not None and self.gateway.db.available:
            await self._persist_phase_mode(
                experiment_id=target_experiment,
                phase_id=phase_id,
                mode=mode,
            )
        if target_experiment is not None:
            await self._emit(
                EventType.EXPERIMENT_PHASE_CHANGE,
                experiment_id=target_experiment,
                detail={"phase_id": phase_id, "mode": mode, "event": "mode_changed"},
            )
        return await self.current_state()

    async def set_phase_params(self, phase_id: str, params: Mapping[str, Any]) -> None:
        """Override parameters for a phase (e.g. mixer_rpm, step_rpm, dose_ml)."""
        recipe = RECIPES.get(self._recipe_id or "")
        pool = recipe.phases if recipe else [p for r in RECIPES.values() for p in r.phases]
        phase = next((p for p in pool if p.phase_id == phase_id), None)
        params, rejected = _guard_overrides(phase, params)
        if rejected:
            log.warning("override rejected for %s: %s", phase_id, rejected)
            self._rejected_overrides.setdefault(phase_id, {}).update(rejected)
        self._param_overrides.setdefault(phase_id, {}).update(params)
        if self._experiment_id and self.gateway.db.available:
            try:
                import json
                effective = json.dumps(self._param_overrides[phase_id])
                await self.gateway.db.execute(
                    """
                    UPDATE experiment_phases
                       SET params = params || $3::jsonb
                     WHERE experiment_id = $1 AND phase_id = $2
                    """,
                    self._experiment_id,
                    phase_id,
                    effective,
                )
            except Exception:
                pass

    async def _persist_phase_mode(self, *, experiment_id: str, phase_id: str, mode: str) -> None:
        """
        Write one phase's mode to ``experiment_phases``.

        An ``UPDATE``, not an upsert: the row is seeded by the create route, and inventing one
        here would need ``phase_num`` (a display ordinal) that the mode endpoint does not carry.
        A phase id that does not belong to the experiment is a no-op rather than an insert of a
        ghost phase.
        """
        try:
            await self.gateway.db.execute(
                """
                UPDATE experiment_phases
                   SET mode = $3
                 WHERE experiment_id = $1
                   AND phase_id = $2
                """,
                experiment_id,
                phase_id,
                mode,
            )
        except Exception:
            log.warning("phase mode persist failed for %s/%s", experiment_id, phase_id, exc_info=True)

    async def pause(self, *, requested_by: str = "operator:web") -> dict[str, Any]:
        """
        Pause the run: hold the timer, stop phase advancement, leave the loop running.

        A no-op when already paused or idle, so a double-click is harmless.
        """
        if self._experiment_id is None or self._paused:
            return await self.current_state()
        self._paused = True
        self._paused_at_s = time.time()
        self._pause_reason = requested_by
        if self._pulse_active:
            # A pause freezes phase logic, so the pulse brake would never run: stop the pulse
            # now or the aerator keeps spinning for the whole pause.
            self._pulse_active = False
            self._pulse_start_s = None
            self._last_pulse_end_s = self._paused_at_s
            try:
                await self.gateway.commands.issue(
                    role="aerator",
                    action="stop",
                    target={},
                    experiment_id=self._experiment_id,
                    requested_by="orchestrator:pause_stops_pulse",
                    deadline_s=30,
                )
            except Exception:
                log.warning("failed to stop aerator pulse on pause", exc_info=True)
        await self._mark_experiment(self._experiment_id, status="paused")
        await self._emit(
            EventType.EXPERIMENT_PAUSED,
            experiment_id=self._experiment_id,
            detail={"requested_by": requested_by, "t_rel_s": self._t_rel_s()},
        )
        return await self.current_state()

    async def resume(self, *, requested_by: str = "operator:web") -> dict[str, Any]:
        """Resume a paused run. Accumulates the paused span so the timer does not jump."""
        if self._experiment_id is None or not self._paused:
            return await self.current_state()
        if self._paused_at_s is not None:
            span = time.time() - self._paused_at_s
            self._paused_accum_s += span
            # The phase clock stops while paused: a pause must not eat into a phase's duration
            # or its timeout (abc_run would otherwise end the moment it resumes).
            if hasattr(self, "_phase_entered_s"):
                self._phase_entered_s += span
            if self._last_pulse_end_s is not None:
                self._last_pulse_end_s += span
            if getattr(self, "_pause_reason", "") == "orchestrator:timeout":
                # Paused because the phase ran out of time: resuming grants a fresh window
                # instead of timing out again on the next tick.
                self._phase_entered_s = time.time()
        self._pause_reason = None
        self._paused = False
        self._paused_at_s = None
        await self._mark_experiment(self._experiment_id, status="active")
        await self._emit(
            EventType.EXPERIMENT_RESUMED,
            experiment_id=self._experiment_id,
            detail={
                "requested_by": requested_by,
                "phase_id": self.active_phase.phase_id if self.active_phase else None,
                "t_rel_s": self._t_rel_s(),
            },
        )
        return await self.current_state()

    # -- internals ---------------------------------------------------------

    @property
    def _started_at_iso(self) -> str | None:
        started_at = getattr(self, "_started_at", None)
        return started_at.isoformat() if started_at else None

    def _t_rel_s(self) -> float | None:
        started = getattr(self, "_started_s", None)
        if not started:
            return None
        # Subtract accumulated pauses so the timer measures run time, and count the open
        # pause up to now while one is in progress.
        paused = self._paused_accum_s
        if self._paused and self._paused_at_s is not None:
            paused += time.time() - self._paused_at_s
        return round(time.time() - started - paused, 1)

    async def _begin(self, *, experiment_id: str | None, requested_by: str) -> None:
        """Start or resume a run, entering its first phase."""
        if experiment_id is None:
            experiment_id = self._experiment_id
        if experiment_id is None:
            raise ValueError("ACTIVE_EXPERIMENT requires an experiment_id")

        # Load the experiment to discover its target recipe. The recipe_id is stored in
        # the experiment's notes/calibration by the create route; fall back to the default
        # protocol so an experiment created before recipes existed still runs.
        recipe_id, phases_status = await self._load_experiment(experiment_id)
        if recipe_id not in RECIPES:
            recipe_id = "planta_2_step"

        resuming = experiment_id == self._experiment_id
        self._experiment_id = experiment_id
        if not resuming:
            self._recipe_id = recipe_id
            self._phase_index = 0
            self._completed = set()
            self._awaiting_confirm = None
            self._action_pending = None
            self._abc_samples = []
            self._last_control_kpis = None
            self._initial_condition = None
            self._kla_samples = []
            self._kla_temps = []
            self._last_kla_metrics = None
            self._pulse_active = False
            self._pulse_start_s = None
            self._last_pulse_end_s = None
            existing_param_overrides = dict(self._param_overrides)
            self._param_overrides = {}
            for row in phases_status:
                raw_params = row.get("params")
                if raw_params:
                    try:
                        import json
                        loaded = json.loads(raw_params) if isinstance(raw_params, str) else dict(raw_params)
                        if loaded:
                            self._param_overrides[str(row["phase_id"])] = loaded
                    except Exception:
                        pass
            # Preserve in-memory parameter overrides (e.g. set by set_phase_params before launch)
            for p_id, p_dict in existing_param_overrides.items():
                self._param_overrides.setdefault(p_id, {}).update(p_dict)
            existing_rejected = dict(self._rejected_overrides)
            self._rejected_overrides = {}
            for ph in RECIPES[recipe_id].phases:
                if ph.phase_id in self._param_overrides:
                    kept, rejected = _guard_overrides(ph, self._param_overrides[ph.phase_id])
                    self._param_overrides[ph.phase_id] = kept
                    rejected = {**existing_rejected.get(ph.phase_id, {}), **rejected}
                    if rejected:
                        log.warning("override rejected for %s: %s", ph.phase_id, rejected)
                        self._rejected_overrides[ph.phase_id] = rejected
            # Seed the mode overrides from the persisted plan instead of clearing them. The
            # operator may have chosen each phase's mode *before* launching -- that choice lives
            # in ``experiment_phases.mode`` -- and the run must honour it. Only a mode that
            # differs from the recipe default is kept as an override; matching values are left
            # out so ``_mode_overrides`` stays a sparse diff and the recipe stays authoritative
            # for anything the operator did not touch.
            recipe = RECIPES[recipe_id]
            recipe_modes = {phase.phase_id: phase.mode for phase in recipe.phases}
            self._mode_overrides = {
                str(row["phase_id"]): str(row["mode"])
                for row in phases_status
                if row.get("mode") is not None and row.get("mode") != recipe_modes.get(str(row["phase_id"]))
            }
            self._started_s = time.time()
            self._started_at = datetime.now(UTC)
            self._paused = False
            self._paused_accum_s = 0.0
            self._paused_at_s = None
            if self.gateway.db.available:
                try:
                    await self.gateway.db.execute(
                        """
                        UPDATE experiments
                           SET status = $2,
                               ended_at = now(),
                               notes = COALESCE(notes, '') || ' [Auto-aborted: superseded by new run]'
                         WHERE status = 'active' AND id != $1
                        """,
                        experiment_id,
                        "aborted",
                    )
                except Exception:
                    log.warning("failed to auto-abort prior active experiments", exc_info=True)
            await self._mark_experiment(experiment_id, status="active", started=True)
            await self._emit(
                EventType.EXPERIMENT_STARTED,
                experiment_id=experiment_id,
                detail={
                    "recipe_id": recipe_id,
                    "requested_by": requested_by,
                    "gateway_version": gateway_version(),
                    "recipe_sha256_16": recipe_digest(RECIPES[recipe_id]),
                },
            )
            # Ensure the OD sensor starts active sampling for the experiment with a unique SD filename.
            try:
                # Synchronize clocks across the station on experiment launch
                if hasattr(self.gateway, "ingest") and self.gateway.ingest is not None:
                    try:
                        await self.gateway.ingest._publish_time()
                    except Exception:
                        log.warning("failed to broadcast aqua/time on experiment start", exc_info=True)

                clean_id = "".join(c for c in experiment_id if c.isalnum())[-6:].upper()
                sd_name = f"E_{clean_id}.CSV" if clean_id else "EXP_RUN.CSV"
                now_epoch_s = int(time.time())
                now_epoch_ms = int(now_epoch_s * 1000)
                await self.gateway.commands.issue(
                    role="od_sensor",
                    action="start_experiment",
                    target={
                        "interval_sec": 5,
                        "experiment_id": experiment_id,
                        "name": experiment_id,
                        "csv_filename": sd_name,
                        "epoch_s": now_epoch_s,
                        "t_dev_ms": now_epoch_ms,
                    },
                    experiment_id=experiment_id,
                    requested_by=requested_by,
                    deadline_s=30,
                )
            except Exception:
                log.warning("failed to start od_sensor for experiment %s", experiment_id, exc_info=True)
            # Enter the first phase, issuing its commands.
            await self._enter_phase(self.active_phase, requested_by=requested_by)
        else:
            # Resume: the phase is unchanged, only the clock and the state flip.
            await self._mark_experiment(experiment_id, status="active", started=False)
            await self._emit(
                EventType.EXPERIMENT_RESUMED,
                experiment_id=experiment_id,
                detail={"phase_id": self.active_phase.phase_id if self.active_phase else None},
            )
        await self._persist_state(STATE_ACTIVE)

    async def _end(self, *, requested_by: str) -> None:
        """
        Stop the run: disarm actuators FIRST, then flip status.

        The order matters. A completed run must never leave an actuator spinning, and the
        disarm commands must be recorded against the run before its status makes them
        look like they belong to nothing.
        """
        experiment_id = self._experiment_id
        if experiment_id is None:
            return
        await self._disarm(experiment_id=experiment_id, requested_by=requested_by)
        await self._mark_experiment(experiment_id, status="completed", ended=True)
        await self._emit(
            EventType.EXPERIMENT_COMPLETED,
            experiment_id=experiment_id,
            detail={"requested_by": requested_by, "t_rel_s": self._t_rel_s()},
        )
        asyncio.create_task(
            self._auto_export_and_upload(
                experiment_id,
                kpis=self._last_control_kpis,
                kla_metrics=self._last_kla_metrics,
            )
        )
        self._experiment_id = None
        self._recipe_id = None
        self._phase_index = 0
        self._awaiting_confirm = None
        self._action_pending = None
        self._pulse_active = False
        self._pulse_start_s = None
        self._last_pulse_end_s = None
        self._pulse_trigger_was_low_do = False
        self._paused = False
        self._paused_accum_s = 0.0
        self._paused_at_s = None
        await self._persist_state(STATE_IDLE)

    async def _auto_export_and_upload(
        self,
        experiment_id: str,
        kpis: dict[str, Any] | None = None,
        kla_metrics: dict[str, Any] | None = None,
    ) -> None:
        """
        Build exports and sync them to Cloudflare R2 immediately after experiment completion.
        Runs in background without delaying state transition to IDLE.
        """
        try:
            # Short grace period for trailing telemetry in-flight to arrive
            await asyncio.sleep(4.0)

            # Force an immediate disk flush of all open telemetry buffers so low-cadence
            # nodes (like od_sensor at 0.2 Hz) have all their final samples written to CSV.
            if hasattr(self.gateway, "archive") and hasattr(self.gateway.archive, "flush_all"):
                try:
                    flushed_count = await self.gateway.archive.flush_all(sync=True)
                    log.info("auto-export: flushed %d telemetry buffer(s) to disk before export", flushed_count)
                except Exception as e:
                    log.warning("auto-export: failed to flush telemetry buffers: %s", e)

            log.info("auto-export: building aligned and native bundles for %s", experiment_id)

            from ..export.bundle import build_bundle
            from ..export.flat import build_flat_export

            # 1. Aligned 1 Hz export
            aligned_dir = self.gateway.settings.export_root / experiment_id
            try:
                await build_bundle(
                    self.gateway.settings,
                    self.gateway.manifest,
                    self.gateway.db,
                    self.gateway.registry,
                    experiment_id,
                    out_dir=aligned_dir,
                    grid_seconds=1.0,
                    compress=True,
                )
            except Exception as e:
                log.warning("auto-export: build_bundle failed for %s: %s", experiment_id, e)

            # Control benchmark KPIs export if available
            if kpis is not None:
                try:
                    import json
                    kpi_path = aligned_dir / "control_kpis.json"
                    kpi_path.write_text(json.dumps(kpis, indent=2))
                    log.info("auto-export: saved control_kpis.json for %s", experiment_id)
                except Exception as e:
                    log.warning("auto-export: failed to write control_kpis.json: %s", e)

            # KLa reaeration metrics export if available
            if kla_metrics is not None:
                try:
                    import json
                    kla_path = aligned_dir / "kla_metrics.json"
                    kla_path.write_text(json.dumps(kla_metrics, indent=2))
                    log.info("auto-export: saved kla_metrics.json for %s", experiment_id)
                except Exception as e:
                    log.warning("auto-export: failed to write kla_metrics.json: %s", e)

            # 2. Native rate export
            native_dir = self.gateway.settings.export_root / f"{experiment_id}_native"
            try:
                await build_flat_export(
                    self.gateway.settings,
                    self.gateway.manifest,
                    self.gateway.db,
                    self.gateway.registry,
                    experiment_id,
                    out_dir=native_dir,
                    compress=True,
                )
            except Exception as e:
                log.warning("auto-export: build_flat_export failed for %s: %s", experiment_id, e)

            # 3. Trigger Cloudflare R2 sync
            log.info("auto-export: triggering R2 upload for %s", experiment_id)
            import shutil
            import subprocess

            sync_cmd = "/usr/local/bin/aqua-r2-sync"
            if shutil.which(sync_cmd) or Path(sync_cmd).exists():
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: subprocess.run([sync_cmd], check=False),
                )
                log.info("auto-export: successfully uploaded %s to Cloudflare R2", experiment_id)
        except Exception as exc:
            log.error("auto-export: unhandled exception for %s: %s", experiment_id, exc)

    async def _disarm(self, *, experiment_id: str, requested_by: str) -> None:
        """Issue a stop to every role this recipe touched. Best-effort per role."""
        for role, action in (
            ("aerator", "stop"),
            ("mixer", "stop_mixer"),
            ("pump", "force_off"),
            ("od_sensor", "stop"),
        ):
            try:
                await self.gateway.commands.issue(
                    role=role,
                    action=action,
                    target={},
                    experiment_id=experiment_id,
                    requested_by=requested_by,
                    deadline_s=30,
                )
            except Exception:
                # A role that is offline or unknown must not block the others, and above
                # all must not block the status flip that makes the run over.
                log.warning("disarm %s/%s failed", role, action, exc_info=True)

    async def _enter_phase(self, phase: Phase | None, *, requested_by: str) -> None:
        if phase is None:
            # Past the last phase: the recipe is done.
            await self._end(requested_by=requested_by)
            return
        self._awaiting_confirm = None
        self._action_pending = None
        await self._persist_phase(phase, status="active", entered=True)
        await self._emit(
            EventType.EXPERIMENT_PHASE_CHANGE,
            experiment_id=self._experiment_id,
            detail={
                "phase_id": phase.phase_id,
                "num": phase.num,
                "title": phase.title,
                "event": "entered",
                "mode": self._mode_overrides.get(phase.phase_id, phase.mode),
                "params": {**phase.params, **self._param_overrides.get(phase.phase_id, {})},
                "condition": dict(phase.condition),
                "timeout_s": phase.timeout_s,
                "on_timeout": phase.on_timeout,
                **({"overrides_rejected": self._rejected_overrides[phase.phase_id]}
                   if phase.phase_id in self._rejected_overrides else {}),
            },
        )
        if phase.phase_id in ("abc_arm", "abk_step"):
            await self._record_initial_condition(phase)
        if phase.phase_id == "abc_run":
            self._abc_samples = []
        elif phase.phase_id == "abc_eval":
            from .control_metrics import compute_control_kpis
            effective = {**phase.params, **self._param_overrides.get(phase.phase_id, {})}
            target_sp = float(effective.get("target_do", 5.00))
            self._last_control_kpis = compute_control_kpis(self._abc_samples, target_sp=target_sp)
            if self._initial_condition is not None:
                self._last_control_kpis = {**self._last_control_kpis, "initial_condition": self._initial_condition}
            await self._emit(
                EventType.EXPERIMENT_PHASE_CHANGE,
                experiment_id=self._experiment_id,
                detail={"phase_id": phase.phase_id, "event": "kpis_computed", "kpis": self._last_control_kpis},
            )
        elif phase.phase_id in ("abk_acq", "pd_acq", "p2_acq"):
            self._kla_samples = []
            self._kla_temps = []
        elif phase.phase_id in ("abk_eval", "pd_analysis", "p2_eval"):
            effective = {**phase.params, **self._param_overrides.get(phase.phase_id, {})}
            c_star = float(effective.get("kla_c_star", 6.70))
            avg_temp = (sum(self._kla_temps) / len(self._kla_temps)) if self._kla_temps else 20.0
            self._last_kla_metrics = compute_kla_metrics(self._kla_samples, c_star=c_star, water_temp_c=avg_temp)
            await self._emit(
                EventType.EXPERIMENT_PHASE_CHANGE,
                experiment_id=self._experiment_id,
                detail={"phase_id": phase.phase_id, "event": "kla_computed", "kla": self._last_kla_metrics},
            )
        elif phase.phase_id == "abc_dose":
            # OD before the dose: with the volume dosed, it gives this run's own mL per mg/L.
            self._c_pre_dose = self._do_history[-1][1] if self._do_history else None
        elif phase.phase_id == "abc_deox":
            self._redose_count = 0
            self._last_redose_s = None
            prev = next((p for p in (RECIPES.get(self._recipe_id or "").phases if self._recipe_id else ())
                         if p.phase_id == "abc_dose"), None)
            self._dosed_total_ml = float({**(prev.params if prev else {}),
                                          **self._param_overrides.get("abc_dose", {})}.get("dose_ml", 80.0))
        elif phase.phase_id in ("abk_verify", "abc_verify"):
            self._pulse_active = False
            self._pulse_start_s = None
            self._last_pulse_end_s = None
            self._pulse_count = 0
            self._verify_entered_s = time.time()
        # MANUAL means "control directo": the operator fires each actuator. Issuing the phase's
        # commands on entry is exactly the automatic action the mode forbids, so they are held
        # until the operator asks for them (``trigger_phase_action``). AUTO and HIBRIDO issue
        # on entry as before -- HIBRIDO still gates only the *advance*, not the entry action.
        mode = self._mode_overrides.get(phase.phase_id, phase.mode)
        if mode == "manual" and phase.commands:
            self._action_pending = phase.phase_id
            await self._emit(
                EventType.EXPERIMENT_PHASE_CHANGE,
                experiment_id=self._experiment_id,
                detail={"phase_id": phase.phase_id, "event": "awaiting_action"},
            )
            return
        await self._issue_phase_commands(phase, requested_by=requested_by)

    async def _record_initial_condition(self, phase: Phase) -> None:
        """
        Capture the state the loop (abc_arm) or the step (abk_step) starts from, and judge it.

        Conforming means: measured DO in [base, base + tol] and rising at least at the certified
        free-reaeration slope (no residual sulfite). Base, tolerance and slope come from the
        preceding verification phase, so the verdict uses exactly what the run was told to reach.
        Non-conforming runs still complete -- their data are kept -- but are flagged so they stay
        out of comparisons.
        """
        recipe = RECIPES.get(self._recipe_id or "")
        verify = next((p for p in recipe.phases if p.phase_id in ("abc_verify", "abk_verify")), None) if recipe else None
        vp = {**verify.params, **self._param_overrides.get(verify.phase_id, {})} if verify else {}
        base = vp.get("base_do_mg_l")
        tol = float(vp.get("base_tol_mg_l", 0.10))
        min_slope = vp.get("min_slope_mg_l_per_min", (verify.condition if verify else {}).get("min_slope_mg_l_per_min"))
        do_now = self._do_history[-1][1] if self._do_history else None
        rate = None
        if self._do_history:
            pts = [p for p in self._do_history if p[0] >= self._do_history[-1][0] - 60_000]
            if len(pts) >= 2:
                rate = least_squares_rate_per_min(pts)
        # Curvature (quadratic fit) and the diagnostics it enables: the true rate C' = y' + τ·y''
        # (first-order measurement lag) and the residual demand r = k_s·(C* − C) − C'. They are
        # recorded for every run; only the curvature takes part in the verdict.
        min_curv = vp.get("min_curvature_mg_l_per_min2")
        cw_ms = int(float(vp.get("curvature_window_s", 120)) * 1000)
        quad = None
        if self._do_history:
            pts2 = [p for p in self._do_history if p[0] >= self._do_history[-1][0] - cw_ms]
            if len(pts2) >= 4 and (pts2[-1][0] - pts2[0][0]) >= cw_ms * 0.85:
                quad = least_squares_quadratic_per_min(pts2)
        true_rate = demand = None
        if quad is not None:
            tau_min = float(vp.get("sensor_tau_s", 18.0)) / 60.0
            true_rate = quad[0] + tau_min * quad[1]
            if do_now is not None:
                ks_min = float(vp.get("mixer_ks_per_h", 1.24)) / 60.0
                demand = ks_min * (float(vp.get("c_star_mg_l", 6.5)) - do_now) - true_rate
        checks: dict[str, bool] = {}
        if base is not None and do_now is not None:
            checks["do_in_band"] = float(base) <= do_now <= float(base) + tol
        if min_slope is not None and rate is not None:
            checks["slope_ok"] = rate >= float(min_slope)
        if min_curv is not None:
            checks["curvature_ok"] = quad is not None and quad[1] >= float(min_curv)
        rpm = self._last_drv_rpm
        if rpm is not None:
            checks["aerator_at_rest"] = abs(rpm) <= 5.0
        verify_s = time.time() - getattr(self, "_verify_entered_s", time.time())
        self._initial_condition = {
            "phase_id": phase.phase_id,
            "do_mg_l": None if do_now is None else round(do_now, 3),
            "slope_mg_l_per_min": None if rate is None else round(rate, 4),
            "slope_end_quad_mg_l_per_min": None if quad is None else round(quad[0], 4),
            "curvature_mg_l_per_min2": None if quad is None else round(quad[1], 4),
            "min_curvature_mg_l_per_min2": min_curv,
            "true_rate_mg_l_per_min": None if true_rate is None else round(true_rate, 4),
            "residual_demand_mg_l_per_min": None if demand is None else round(demand, 4),
            "temp_c": self._last_od_temp_c,
            "aerator_rpm": rpm,
            "base_do_mg_l": base,
            "base_tol_mg_l": tol if base is not None else None,
            "min_slope_mg_l_per_min": min_slope,
            "pulses_in_verify": self._pulse_count,
            "verify_duration_s": round(verify_s, 1),
            "checks": checks,
            "conforming": bool(checks) and all(checks.values()) if base is not None else None,
        }
        await self._emit(
            EventType.EXPERIMENT_PHASE_CHANGE,
            experiment_id=self._experiment_id,
            detail={"phase_id": phase.phase_id, "event": "initial_condition", **self._initial_condition},
        )

    async def _issue_phase_commands(
        self,
        phase: Phase,
        *,
        requested_by: str,
        params_override: Mapping[str, Any] | None = None,
    ) -> None:
        """Issue a phase's entry commands through the outbox, in order."""
        effective_params = dict(phase.params)
        effective_params.update(self._param_overrides.get(phase.phase_id, {}))
        if params_override:
            effective_params.update(params_override)

        for role, action, target in phase.commands:
            try:
                cmd_target = dict(target)
                if role == "aerator" and action in ("set_speed", "start"):
                    step_rpm = effective_params.get("step_rpm")
                    if step_rpm is not None:
                        cmd_target["step_rpm"] = step_rpm
                        cmd_target["target_rpm"] = step_rpm
                        cmd_target["speed_percent"] = round((float(step_rpm) / 600.0) * 100)
                elif role == "aerator" and action in ("set_config", "set_mode"):
                    for key in ("target_do", "setpoint_mg_l", "kp", "ki", "kd", "beta", "tt", "tau_lag", "mode"):
                        if key in effective_params:
                            cmd_target[key] = effective_params[key]
                    if "ctrl_type" in effective_params:
                        cmd_target["mode"] = effective_params["ctrl_type"]
                    if "setpoint_mg_l" in cmd_target and "target_do" not in cmd_target:
                        cmd_target["target_do"] = cmd_target["setpoint_mg_l"]
                    if "target_do" in cmd_target and "setpoint_mg_l" not in cmd_target:
                        cmd_target["setpoint_mg_l"] = cmd_target["target_do"]
                elif role == "mixer" and action in ("start_mixer", "set_speed", "start"):
                    mixer_enable = effective_params.get("mixer_enable", True)
                    if isinstance(mixer_enable, str):
                        mixer_enable = mixer_enable.strip().upper() not in ("APAGADO", "OFF", "0", "FALSE", "")
                    mixer_rpm = effective_params.get("mixer_rpm")
                    if not mixer_enable or (mixer_rpm is not None and float(mixer_rpm) <= 0):
                        action = "stop_mixer"
                        cmd_target = {}
                    else:
                        action = "start_mixer"
                        if mixer_rpm is not None:
                            rpm_f = float(mixer_rpm)
                            cmd_target["target_rpm"] = rpm_f
                            cmd_target["mixer_rpm"] = rpm_f
                            cmd_target["actual_rpm"] = rpm_f
                elif role == "pump" and action == "dose":
                    stock_conc_gpl = float(effective_params.get("stock_conc_gpl", 100.0))
                    density_g_per_ml = float(effective_params.get("density_g_per_ml", 1.10))
                    sulfite_g = effective_params.get("sulfite_g")
                    target_g = effective_params.get("target_g") or effective_params.get("grams_total")
                    dose_ml = effective_params.get("dose_ml") or effective_params.get("target_ml")

                    override = params_override or {}
                    if "target_g" in override or "grams_total" in override:
                        dose_ml = round(float(override.get("target_g") or override.get("grams_total")) / density_g_per_ml, 2)
                    elif "dose_ml" in override or "target_ml" in override:
                        dose_ml = float(override.get("dose_ml") or override.get("target_ml"))
                    elif "sulfite_g" in override:
                        dose_ml = round(float(override["sulfite_g"]) / (stock_conc_gpl / 1000.0), 2)
                    elif sulfite_g is not None:
                        # Pure Na2SO3 grams to volume (mL): 100 g/L = 0.100 g/mL => V = g / 0.100
                        dose_ml = round(float(sulfite_g) / (stock_conc_gpl / 1000.0), 2)
                    elif dose_ml is not None:
                        dose_ml = float(dose_ml)
                    elif target_g is not None:
                        # Solution total grams to volume (mL): V = g / density
                        dose_ml = round(float(target_g) / density_g_per_ml, 2)

                    if dose_ml is not None:
                        dose_ml = float(dose_ml)
                        cmd_target["target_ml"] = dose_ml
                        cmd_target["dose_ml"] = dose_ml
                    if sulfite_g is not None:
                        cmd_target["sulfite_g"] = float(sulfite_g)
                    elif dose_ml is not None:
                        cmd_target["sulfite_g"] = round(dose_ml * (stock_conc_gpl / 1000.0), 2)
                    if target_g is not None:
                        cmd_target["target_g"] = float(target_g)
                    elif dose_ml is not None:
                        cmd_target["target_g"] = round(dose_ml * density_g_per_ml, 2)

                # The command's own deadline is bounded by the phase's timeout so a command
                # cannot outlive the phase that issued it. A supervised phase may declare no
                # timeout, and then there is nothing to bound it against -- fall back to the
                # outbox's own default (300 s) rather than inventing a number here, because
                # that default is what the rest of the system already uses when a caller
                # expresses no preference. Clamping a `None` would crash; guessing would
                # silently pick a different bound than the API's.
                command_deadline_s = max(30, min(phase.timeout_s, 3600)) if phase.timeout_s is not None else 300
                await self.gateway.commands.issue(
                    role=role,
                    action=action,
                    target=cmd_target,
                    experiment_id=self._experiment_id,
                    requested_by=requested_by,
                    deadline_s=command_deadline_s,
                )
            except Exception:
                log.warning("phase %s command %s/%s failed", phase.phase_id, role, action, exc_info=True)

    async def trigger_phase_action(
        self,
        *,
        requested_by: str = "operator:web",
        params_override: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Operator fires the active MANUAL phase's entry commands ("Disparar Acción").

        A no-op unless a MANUAL phase is waiting for its action, so a double-click is harmless
        and a stray click in AUTO/HIBRIDO changes nothing. Firing clears the pending flag, so
        the action runs once per entry -- the operator re-enters the phase or re-modes it to
        repeat it, rather than every click re-issuing the setpoint.
        """
        phase = self.active_phase
        if phase is None or self._action_pending != phase.phase_id:
            return await self.current_state()
        if params_override:
            self._param_overrides.setdefault(phase.phase_id, {}).update(params_override)
        self._action_pending = None
        await self._emit(
            EventType.EXPERIMENT_PHASE_CHANGE,
            experiment_id=self._experiment_id,
            detail={
                "phase_id": phase.phase_id,
                "event": "action_triggered",
                "requested_by": requested_by,
                "params": params_override or {},
            },
        )
        await self._issue_phase_commands(phase, requested_by=requested_by, params_override=params_override)
        return await self.current_state()

    async def _advance(self, phase: Phase, *, requested_by: str, reason: str) -> None:
        """Mark the current phase done and enter the next."""
        self._completed.add(phase.phase_id)
        self.transitions += 1
        await self._persist_phase(phase, status="completed", exited=True)
        await self._emit(
            EventType.EXPERIMENT_PHASE_CHANGE,
            experiment_id=self._experiment_id,
            detail={"phase_id": phase.phase_id, "num": phase.num, "event": "completed", "reason": reason},
        )
        self._phase_index += 1
        await self._enter_phase(self.active_phase, requested_by=requested_by)

    async def _tick_forever(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(TICK_INTERVAL_S)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("orchestrator tick failed")

    async def _tick(self) -> None:
        phase = self.active_phase
        if phase is None or self._experiment_id is None:
            return
        # A paused run advances nothing: neither phase nor timer. That is what makes it a
        # pause rather than a slower run.
        if self._paused:
            return
        # A phase awaiting the operator's visto bueno does not auto-advance: the operator is
        # the clock now, and a supervisor who has been asked a question is exactly who should
        # decide when it is answered. So it waits.
        #
        # It used to wait *unconditionally* -- the early return skipped the timeout entirely --
        # which meant a prompt could outlive its own `timeout_s` forever while `manual`, which
        # never raises a prompt, kept its valve. That was backwards. Now a prompt respects the
        # phase's timeout when one is declared, and a supervised phase declaring none waits
        # until the operator answers or stops the run. Either way the rule is the same rule:
        # a supervised phase is released by its operator, and by a clock only if it has one.
        if self._awaiting_confirm == phase.phase_id:
            held = self._context()
            if phase.timeout_s is None or held.now_s - held.phase_entered_s < phase.timeout_s:
                return
            await self._advance(
                phase,
                requested_by="orchestrator",
                reason="timeout_released_pending_confirmation",
            )
            return

        mode = self._mode_overrides.get(phase.phase_id, phase.mode)
        ctx = self._context()

        effective = {**phase.params, **self._param_overrides.get(phase.phase_id, {})}
        cond = dict(phase.condition)
        kind = cond.get("kind")
        if kind == "dose_complete":
            conc = float(effective.get("stock_conc_gpl", 100.0))
            if "sulfite_g" in effective:
                cond["target_ml"] = round(float(effective["sulfite_g"]) / (conc / 1000.0), 2)
            elif "dose_ml" in effective:
                cond["target_ml"] = float(effective["dose_ml"])
        elif kind == "do_below_and_stable":
            if "do_floor_mg_l" in effective:
                cond["below_mg_l"] = float(effective["do_floor_mg_l"])
            if "max_delta_mg_l" in effective:
                cond["max_delta_mg_l"] = float(effective["max_delta_mg_l"])
            if "window_s" in effective:
                cond["window_s"] = float(effective["window_s"])
            if "eps_mg_l_per_min" in effective:
                cond["eps_mg_l_per_min"] = float(effective["eps_mg_l_per_min"])
        elif kind == "do_above":
            if "stop_do_mg_l" in effective:
                cond["threshold_mg_l"] = float(effective["stop_do_mg_l"])
        elif kind == "do_below":
            if "do_start_max_mg_l" in effective:
                cond["threshold_mg_l"] = float(effective["do_start_max_mg_l"])
            if "settle_time_s" in effective:
                cond["min_elapsed_s"] = float(effective["settle_time_s"])
        elif kind == "setpoint_reached":
            if "step_rpm" in effective:
                cond["target"] = float(effective["step_rpm"])
        elif kind == "do_stable":
            if "warm_time_s" in effective:
                cond["min_elapsed_s"] = float(effective["warm_time_s"])
        elif kind == "elapsed":
            if "run_time_s" in effective and phase.phase_id in ("cl_run", "abc_run"):
                cond["seconds"] = float(effective["run_time_s"])
        elif kind == "loop_settled":
            if "target_do" in effective:
                cond["setpoint"] = float(effective["target_do"])
            for key in ("min_elapsed_s", "band_mg_l", "band_below_mg_l", "window_s", "eps_mg_l_per_min"):
                if f"settle_{key}" in effective:
                    cond[key] = float(effective[f"settle_{key}"])
        elif kind == "sulfite_exhausted_post_hold":
            if "hold_time_s" in effective:
                cond["hold_time_s"] = float(effective["hold_time_s"])
                cond["min_elapsed_s"] = float(effective["hold_time_s"])
            if "min_do_mg_l" in effective:
                cond["min_do_mg_l"] = float(effective["min_do_mg_l"])
            if "max_delta_mg_l" in effective:
                cond["max_delta_mg_l"] = float(effective["max_delta_mg_l"])
            if "min_slope_mg_l_per_min" in effective:
                cond["min_slope_mg_l_per_min"] = float(effective["min_slope_mg_l_per_min"])
            if "rest_cooldown_s" in effective:
                cond["rest_cooldown_s"] = float(effective["rest_cooldown_s"])
            if "min_curvature_mg_l_per_min2" in effective:
                cond["min_curvature_mg_l_per_min2"] = float(effective["min_curvature_mg_l_per_min2"])
            if "curvature_window_s" in effective:
                cond["curvature_window_s"] = float(effective["curvature_window_s"])
            # Pass orchestrator pulse state to condition
            if self._last_pulse_end_s is not None:
                cond["last_pulse_end_s"] = self._last_pulse_end_s
            if self._pulse_active:
                # If pulse is active right now, require_rest_motor will fail, and we force last_pulse_end_s = now
                cond["last_pulse_end_s"] = ctx.now_s

        # A supervised phase (`manual`/`hibrido`) may declare no timeout: the operator is the
        # clock, so there is nothing to run out. `auto` always declares one (enforced by
        # `validate_recipes`), so a mode that advances without a human is always bounded.
        effective_timeout = phase.timeout_s
        if "safety_timeout_s" in effective:
            effective_timeout = int(effective["safety_timeout_s"])
        elif "run_time_s" in effective and phase.phase_id in ("cl_run", "abc_run"):
            effective_timeout = int(effective["run_time_s"])
        timed_out = effective_timeout is not None and ctx.now_s - ctx.phase_entered_s >= effective_timeout
        cond_met = evaluate(cond, ctx)

        # Handle timeout policies: abort / pause on critical phase timeout
        if timed_out and not cond_met:
            on_timeout = getattr(phase, "on_timeout", "advance")
            if on_timeout == "abort":
                log.warning(
                    "phase %s TIMED OUT after %s s with condition unsatisfied. Aborting experiment.",
                    phase.phase_id,
                    effective_timeout,
                )
                await self._disarm(experiment_id=self._experiment_id, requested_by="orchestrator")
                await self._mark_experiment(self._experiment_id, status="aborted", ended=True)
                await self._emit(
                    EventType.EXPERIMENT_ABORTED,
                    experiment_id=self._experiment_id,
                    detail={
                        "phase_id": phase.phase_id,
                        "reason": f"timeout_in_critical_phase_{phase.phase_id}",
                        "timeout_s": effective_timeout,
                    },
                )
                self._experiment_id = None
                self._recipe_id = None
                self._phase_index = 0
                self._awaiting_confirm = None
                self._action_pending = None
                self._pulse_active = False
                self._paused = False
                await self._persist_state(STATE_IDLE)
                return
            elif on_timeout == "pause":
                log.warning(
                    "phase %s TIMED OUT after %s s. Pausing experiment for operator review.",
                    phase.phase_id,
                    effective_timeout,
                )
                await self.pause(requested_by="orchestrator:timeout")
                return

        # Complementary dose (base recipe): the deoxygenation stalled above the floor, so the dose
        # was short (effective yield 58-87 %). Waiting out the timeout only wastes the run; dose the
        # missing oxygen equivalent instead. The volume comes from the O2 balance, with a margin.
        if phase.phase_id == "abc_deox" and effective.get("redose_enable") and not cond_met:
            await self._maybe_redose(phase, effective, ctx)

        # Adaptive pulse control in residual sulfite verification phases (abk_verify, abc_verify)
        if phase.phase_id in ("abk_verify", "abc_verify") and not cond_met:
            pulse_rpm = float(effective.get("pulse_rpm", 100))
            pulse_trigger_slope = float(effective.get("pulse_trigger_slope", -0.015))
            pulse_trigger_do = float(effective.get("pulse_trigger_do", 0.45))
            pulse_brake_do = float(effective.get("pulse_brake_do", 0.60))
            pulse_tau_lag_s = float(effective.get("pulse_tau_lag_s", 20.0))
            max_pulse_s = float(effective.get("max_pulse_s", 20))

            current_do = ctx.value("od_sensor", "od_do_mg_l")
            if current_do is None and ctx.do_history:
                current_do = ctx.do_history[-1][1]

            # Calculate recent rate
            recent_rate = None
            if ctx.do_history:
                window_ms = 60_000
                recent_pts = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - window_ms]
                if len(recent_pts) >= 2:
                    recent_rate = least_squares_rate_per_min(recent_pts)

            # Predictive DO estimating sensor lag and micro-bubble hold-up
            pred_do = current_do
            if current_do is not None and recent_rate is not None and recent_rate > 0.0:
                # rate in mg/L/min converted to mg/L/s: recent_rate / 60.0
                pred_do = current_do + (recent_rate / 60.0) * pulse_tau_lag_s

            now_s = ctx.now_s

            if self._pulse_active:
                pulse_duration = now_s - (self._pulse_start_s or now_s)
                # Predictive braking condition:
                # Brake if max duration elapsed, OR if either current_do or pred_do reaches pulse_brake_do
                should_brake = False
                brake_reason = ""
                if pulse_duration >= max_pulse_s:
                    should_brake = True
                    brake_reason = f"max_duration_{pulse_duration:.1f}s"
                elif pred_do is not None and pred_do >= pulse_brake_do:
                    should_brake = True
                    brake_reason = f"pred_do_reached_{pred_do:.2f}_meas_{current_do:.2f}"
                elif current_do is not None and current_do >= pulse_brake_do:
                    should_brake = True
                    brake_reason = f"do_reached_{current_do:.2f}"

                if should_brake:
                    log.info(
                        "phase %s: stopping adaptive pulse (%s), returning to rest hold",
                        phase.phase_id,
                        brake_reason,
                    )
                    self._pulse_active = False
                    self._pulse_start_s = None
                    self._last_pulse_end_s = now_s
                    try:
                        await self.gateway.commands.issue(
                            role="aerator",
                            action="stop",
                            target={},
                            experiment_id=self._experiment_id,
                            requested_by="orchestrator:pulse_brake",
                            deadline_s=30,
                        )
                    except Exception as e:
                        log.warning("failed to issue aerator stop in pulse control: %s", e)
            else:
                # Aerator is currently at 0 RPM. Check if DO drop triggers a new gentle pulse.
                # Allow a short rest cooldown of 15 s after a previous pulse before triggering another.
                cooldown_ok = self._last_pulse_end_s is None or (now_s - self._last_pulse_end_s >= 15.0)
                falling_chemically = recent_rate is not None and recent_rate < pulse_trigger_slope
                below_trigger = current_do is not None and current_do < pulse_trigger_do

                below_brake = current_do is None or current_do < pulse_brake_do
                if cooldown_ok and below_brake and (falling_chemically or below_trigger):
                    log.info(
                        "phase %s: triggering gentle adaptive pulse at %d RPM (rate=%.4f mg/L/min, DO=%.2f mg/L)",
                        phase.phase_id,
                        pulse_rpm,
                        recent_rate if recent_rate is not None else 0.0,
                        current_do if current_do is not None else 0.0,
                    )
                    self._pulse_active = True
                    self._pulse_start_s = now_s
                    self._pulse_count += 1
                    self._pulse_trigger_was_low_do = bool(below_trigger)
                    try:
                        await self.gateway.commands.issue(
                            role="aerator",
                            action="set_mode",
                            target={"mode": "manual"},
                            experiment_id=self._experiment_id,
                            requested_by="orchestrator:pulse_mode",
                            deadline_s=30,
                        )
                        await self.gateway.commands.issue(
                            role="aerator",
                            action="set_speed",
                            target={"step_rpm": pulse_rpm, "target_rpm": pulse_rpm, "speed_percent": round((pulse_rpm / 600.0) * 100)},
                            experiment_id=self._experiment_id,
                            requested_by="orchestrator:pulse_speed",
                            deadline_s=30,
                        )
                    except Exception as e:
                        log.warning("failed to trigger aerator pulse: %s", e)

        satisfied = timed_out or cond_met

        if not satisfied:
            return

        # MANUAL: the operator is the clock. A satisfied condition is *not* an advance — the
        # whole point of manual control is that the operator fires each actuator and decides
        # when the phase is over. The phase advances on the timeout only if one was declared;
        # a supervised phase may declare none, in which case it waits until the operator acts
        # or stops the run. That is not a deadlock to be rescued: a `manual` phase with nobody
        # at the bench is a run nobody is performing, and `pause`/`IDLE` end it.
        if mode == "manual" and not timed_out:
            return

        # HIBRIDO: condition met, but hold for the operator. Reaching here means no prompt is
        # pending (the awaiting branch above handled that), so either the condition just
        # fired or the clock expired -- and a supervised phase without a timeout has no clock
        # to expire.
        if mode == "hibrido" and not timed_out:
            self._awaiting_confirm = phase.phase_id
            await self._persist_phase(phase, status="awaiting_confirm", entered=False)
            await self._emit(
                EventType.EXPERIMENT_PHASE_CHANGE,
                experiment_id=self._experiment_id,
                detail={"phase_id": phase.phase_id, "event": "awaiting_confirm"},
            )
            return

        await self._advance(
            phase,
            requested_by="orchestrator",
            reason="timeout" if timed_out else "condition_met",
        )

    async def _maybe_redose(self, phase: Phase, effective: Mapping[str, Any], ctx: PhaseContext) -> None:
        count = getattr(self, "_redose_count", 0)
        if count >= int(effective.get("redose_max_count", 2)):
            return
        elapsed = ctx.now_s - ctx.phase_entered_s
        last = getattr(self, "_last_redose_s", None)
        wait_s = float(effective.get("redose_after_s", 360))
        if elapsed < wait_s or (last is not None and ctx.now_s - last < wait_s):
            return
        do = ctx.value("od_sensor", "od_do_mg_l")
        if do is None or do < float(effective.get("redose_min_do_mg_l", 0.7)):
            return
        win_ms = int(float(effective.get("redose_window_s", 120)) * 1000)
        pts = [p for p in ctx.do_history if p[0] >= ctx.do_history[-1][0] - win_ms] if ctx.do_history else []
        if len(pts) < 4 or (pts[-1][0] - pts[0][0]) < win_ms * 0.85:
            return
        rate = least_squares_rate_per_min(pts)
        if rate is None or rate < float(effective.get("redose_stall_slope", -0.04)):
            return  # still falling: the dose is still working
        floor = float(effective.get("do_floor_mg_l", 0.5))
        need = do - floor + float(effective.get("redose_margin_mg_l", 0.8))
        # mL per mg/L: the larger of the recipe's (low observed yield) and this run's own
        # (volume dosed so far over the drop it produced; ignoring reaeration makes it conservative).
        ml_per = float(effective.get("redose_ml_per_mg_l", 10.6))
        c_pre = getattr(self, "_c_pre_dose", None)
        dosed = getattr(self, "_dosed_total_ml", None)
        if c_pre is not None and dosed and c_pre - do > 0.5:
            ml_per = max(ml_per, dosed / (c_pre - do))
        ml = need * ml_per
        ml = round(min(max(ml, float(effective.get("redose_min_ml", 10))), float(effective.get("redose_max_ml", 40))), 1)
        self._redose_count = count + 1
        self._last_redose_s = ctx.now_s
        self._dosed_total_ml = (dosed or 0.0) + ml
        log.info("phase %s: deoxygenation stalled at %.2f mg/L (%.3f mg/L/min): complementary dose %.1f mL",
                 phase.phase_id, do, rate, ml)
        await self._issue_dose(ml, effective, requested_by="orchestrator:redose")
        await self._emit(
            EventType.EXPERIMENT_PHASE_CHANGE,
            experiment_id=self._experiment_id,
            detail={"phase_id": phase.phase_id, "event": "complementary_dose", "n": self._redose_count,
                    "do_mg_l": round(do, 3), "slope_mg_l_per_min": round(rate, 4), "dose_ml": ml,
                    "ml_per_mg_l": round(ml_per, 2), "c_pre_dose_mg_l": None if c_pre is None else round(c_pre, 3),
                    "elapsed_s": round(elapsed, 1)},
        )

    async def _issue_dose(self, ml: float, effective: Mapping[str, Any], *, requested_by: str) -> None:
        conc = float(effective.get("stock_conc_gpl", 100.0))
        dens = float(effective.get("density_g_per_ml", 1.10))
        try:
            await self.gateway.commands.issue(
                role="pump",
                action="dose",
                target={"dose_ml": ml, "target_ml": ml, "sulfite_g": round(ml * conc / 1000.0, 2),
                        "target_g": round(ml * dens, 2)},
                experiment_id=self._experiment_id,
                requested_by=requested_by,
                deadline_s=300,
            )
        except Exception as e:  # pragma: no cover - logged, the run goes on
            log.warning("failed to issue dose (%s): %s", requested_by, e)

    def _context(self) -> PhaseContext:
        live = self.gateway.live.snapshot()
        metrics: dict[tuple[str, str], float] = {}
        for role, node in (live.get("nodes") or {}).items():
            for field, value in (node.get("metrics") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metrics[(role, field)] = float(value)
        now = time.time()
        return PhaseContext(
            metrics=metrics,
            do_history=self._do_history,
            now_s=now,
            phase_entered_s=getattr(self, "_phase_entered_s", now),
            run_started_s=getattr(self, "_started_s", now),
        )

    def _phase_views(self) -> list[dict[str, Any]]:
        if self._recipe_id is None:
            return []
        recipe = RECIPES.get(self._recipe_id)
        if recipe is None:
            return []
        views = []
        for index, phase in enumerate(recipe.phases):
            if phase.phase_id in self._completed:
                status = "completed"
            elif index == self._phase_index:
                status = "awaiting_confirm" if self._awaiting_confirm == phase.phase_id else "active"
            else:
                status = "pending"
            cond_text = phase.condition_text
            effective_params = {**phase.params, **self._param_overrides.get(phase.phase_id, {})}
            effective_timeout = phase.timeout_s
            if "safety_timeout_s" in effective_params:
                effective_timeout = int(effective_params["safety_timeout_s"])
            elif phase.phase_id in ("cl_run", "abc_run") and "run_time_s" in effective_params:
                effective_timeout = int(effective_params["run_time_s"])

            if phase.phase_id in ("p1_dose", "pd_dose", "abk_dose", "abc_dose") and "sulfite_g" in effective_params:
                sg = float(effective_params["sulfite_g"])
                conc = float(effective_params.get("stock_conc_gpl", 100.0))
                v_ml = round(sg / (conc / 1000.0), 1)
                cond_text = f"Masa dispensada = {sg:.2f} g Na₂SO₃ ({v_ml:.1f} mL ± 0.2 mL) registrado por encoder"
            elif phase.phase_id in ("p1_deox", "pd_deox", "abk_deox", "abc_deox") and "do_floor_mg_l" in effective_params:
                floor = float(effective_params["do_floor_mg_l"])
                delta = float(effective_params.get("max_delta_mg_l", 0.04))
                w_s = int(effective_params.get("window_s", 60))
                cond_text = f"DO < {floor:.1f} mg/L Y estabilizado (ΔDO ≤ {delta:.2f} mg/L en {w_s} s) Y t > 180 s"
            elif phase.phase_id in ("abk_verify", "abc_verify") and "min_do_mg_l" in effective_params:
                m_do = float(effective_params["min_do_mg_l"])
                h_s = int(effective_params.get("hold_time_s", 60))
                cond_text = f"DO ≥ {m_do:.2f} mg/L sostenido y sin caída química durante {h_s} s"
            elif "stop_do_mg_l" in effective_params:
                stop_do = float(effective_params["stop_do_mg_l"])
                cond_text = f"DO ≥ {stop_do:.2f} mg/L ó timeout"
            elif "do_start_max_mg_l" in effective_params:
                max_do = float(effective_params["do_start_max_mg_l"])
                settle = int(effective_params.get("settle_time_s", 60))
                cond_text = f"DO inicial < {max_do:.1f} mg/L Y estanque en reposo durante ≥ {settle} s"
            elif phase.phase_id in ("p1_prep", "pd_prep", "abk_prep", "abc_prep") and "warm_time_s" in effective_params:
                w_s = int(effective_params["warm_time_s"])
                cond_text = f"DO estabilizado (|d[DO]/dt| < 0.05 mg/L/min) tras mezcla ≥ {w_s} s"
            elif phase.phase_id in ("cl_run", "abc_run") and "run_time_s" in effective_params:
                r_s = int(effective_params["run_time_s"])
                cond_text = f"Tiempo transcurrido ≥ duración fijada ({r_s} s)"
            elif phase.phase_id in ("p2_step", "pd_step", "abk_step") and "step_rpm" in effective_params:
                rpm = int(effective_params["step_rpm"])
                cond_text = f"ODrive alcanza consigna ({rpm} RPM)"

            views.append(
                {
                    "phase_id": phase.phase_id,
                    "num": phase.num,
                    "title": phase.title,
                    "desc": phase.desc,
                    "mode": self._mode_overrides.get(phase.phase_id, phase.mode),
                    "status": status,
                    "condition_text": cond_text,
                    "params": effective_params,
                    "timeout_s": effective_timeout,
                }
            )
        return views

    # -- persistence -------------------------------------------------------

    async def _load_experiment(self, experiment_id: str) -> tuple[str, list[Mapping[str, Any]]]:
        if not self.gateway.db.available:
            return "planta_2_step", []
        row = await self.gateway.db.fetchrow("SELECT notes, calibration FROM experiments WHERE id = $1", experiment_id)
        if row is None:
            raise ValueError(f"experiment {experiment_id!r} does not exist")
        notes = row.get("notes") or ""
        # The recipe id rides in the notes as ``recipe:<id>`` — a free-text field the create
        # route writes. Parsed here rather than in a dedicated column because the experiments
        # table is shared with the legacy flow and adding a column for a refactor would be a
        # migration for no reader other than this one.
        recipe_id = "planta_2_step"
        for token in str(notes).replace(",", " ").split():
            if token.startswith("recipe:"):
                recipe_id = token.split(":", 1)[1]
        # Read ``mode`` as well as ``status``: the mode the operator set before launch lives in
        # this row, and the whole point of persisting it is to seed the run from it rather than
        # from the recipe default. Fetching only ``phase_id, status`` was why a pre-launch mode
        # could never survive to the run.
        phases = await self.gateway.db.fetch(
            "SELECT phase_id, mode, status, params FROM experiment_phases WHERE experiment_id = $1",
            experiment_id,
        )
        for p in phases:
            raw_params = p.get("params")
            if raw_params:
                try:
                    loaded = json.loads(raw_params) if isinstance(raw_params, str) else dict(raw_params)
                    self._param_overrides.setdefault(p["phase_id"], {}).update(loaded)
                except Exception:
                    pass
        return recipe_id, phases

    async def _mark_experiment(
        self, experiment_id: str, *, status: str, started: bool = False, ended: bool = False
    ) -> None:
        if not self.gateway.db.available:
            return
        try:
            await self.gateway.db.execute(
                """
                UPDATE experiments
                   SET status = $2,
                       started_at = CASE WHEN $3 THEN COALESCE(started_at, now()) ELSE started_at END,
                       ended_at   = CASE WHEN $4 THEN now() ELSE ended_at END,
                       updated_at = now()
                 WHERE id = $1
                """,
                experiment_id,
                status,
                started,
                ended,
            )
        except Exception:
            log.warning("experiment status update failed for %s", experiment_id, exc_info=True)

    async def _persist_phase(self, phase: Phase, *, status: str, entered: bool = False, exited: bool = False) -> None:
        if not self.gateway.db.available or self._experiment_id is None:
            return
        mode = self._mode_overrides.get(phase.phase_id, phase.mode)
        try:
            await self.gateway.db.execute(
                """
                INSERT INTO experiment_phases
                    (experiment_id, phase_id, phase_num, mode, status, params, entered_at, exited_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb,
                        CASE WHEN $7 THEN now() ELSE NULL END,
                        CASE WHEN $8 THEN now() ELSE NULL END)
                ON CONFLICT (experiment_id, phase_id) DO UPDATE
                   SET status = EXCLUDED.status,
                       mode = EXCLUDED.mode,
                       entered_at = COALESCE(experiment_phases.entered_at, EXCLUDED.entered_at),
                       exited_at = COALESCE(EXCLUDED.exited_at, experiment_phases.exited_at)
                """,
                self._experiment_id,
                phase.phase_id,
                phase.num,
                mode,
                status,
                json.dumps(dict(phase.params)),
                entered,
                exited,
            )
            if entered:
                self._phase_entered_s = time.time()
        except Exception:
            log.warning("phase persist failed for %s", phase.phase_id, exc_info=True)

    async def _persist_state(self, state: str) -> None:
        """
        Write the global state to ``system_settings.system_state``.

        Best-effort: the in-memory fields are authoritative for this process, and losing
        the cookie-cutter row to a database blip must not abort a run that is physically
        fine. The next successful write reconciles it.
        """
        if not self.gateway.db.available:
            return
        try:
            await self.gateway.db.execute(
                """
                INSERT INTO system_settings (key, value, description, updated_at)
                VALUES ('system_state', $1::jsonb, 'Global orchestrator state', now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                json.dumps(
                    {
                        "state": state,
                        "experiment_id": self._experiment_id,
                        "recipe_id": self._recipe_id,
                        "since": datetime.now(UTC).isoformat(),
                    }
                ),
            )
        except Exception:
            log.warning("system_state persist failed", exc_info=True)

    # -- events ------------------------------------------------------------

    async def _emit_state_change(self, *, previous: str, new: str, requested_by: str) -> None:
        await self._emit(
            EventType.STATE_CHANGE,
            experiment_id=self._experiment_id,
            detail={"from": previous, "to": new, "requested_by": requested_by},
            scope=Scope.SYSTEM,
        )

    async def _emit(
        self,
        event_type: EventType,
        *,
        experiment_id: str | None,
        detail: Mapping[str, Any],
        scope: Scope | None = None,
    ) -> None:
        event = Event.make(
            event_type,
            source=Source.GATEWAY,
            experiment_id=experiment_id,
            detail=detail,
            scope=scope,
        )
        try:
            await self.gateway.events.emit(event, live=self.gateway.live)
        except Exception:
            log.warning("orchestrator event %s failed", event_type.value, exc_info=True)
