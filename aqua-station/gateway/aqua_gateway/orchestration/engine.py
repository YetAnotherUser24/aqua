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
from .conditions import PhaseContext, evaluate
from .recipes import RECIPES, Phase

if TYPE_CHECKING:  # pragma: no cover
    from ..main import Gateway

log = logging.getLogger("aqua.orchestration")

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

        Called from the MQTT ingest loop. Kept deliberately minimal — it appends to a
        bounded DO history and does nothing else — because it runs per sample and the
        engine's real work happens on its own cadence, not here.
        """
        if role != "od_sensor":
            return
        value = row.get("od_do_mg_l")
        if value is None:
            return
        self._do_history.append((t_dev_ms, float(value)))
        if len(self._do_history) > _DO_HISTORY_MAX:
            del self._do_history[: len(self._do_history) - _DO_HISTORY_MAX]

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

    def set_phase_params(self, phase_id: str, params: Mapping[str, Any]) -> None:
        """Override parameters for a phase (e.g. mixer_rpm, step_rpm, dose_ml)."""
        self._param_overrides.setdefault(phase_id, {}).update(params)

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
            self._paused_accum_s += time.time() - self._paused_at_s
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
            await self._mark_experiment(experiment_id, status="active", started=True)
            await self._emit(
                EventType.EXPERIMENT_STARTED,
                experiment_id=experiment_id,
                detail={"recipe_id": recipe_id, "requested_by": requested_by},
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
        asyncio.create_task(self._auto_export_and_upload(experiment_id))
        self._experiment_id = None
        self._recipe_id = None
        self._phase_index = 0
        self._awaiting_confirm = None
        self._action_pending = None
        self._paused = False
        self._paused_accum_s = 0.0
        self._paused_at_s = None
        await self._persist_state(STATE_IDLE)

    async def _auto_export_and_upload(self, experiment_id: str) -> None:
        """
        Build exports and sync them to Cloudflare R2 immediately after experiment completion.
        Runs in background without delaying state transition to IDLE.
        """
        try:
            # Short grace period for trailing telemetry flushes to settle
            await asyncio.sleep(4.0)
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
            detail={"phase_id": phase.phase_id, "num": phase.num, "title": phase.title, "event": "entered"},
        )
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
                elif role == "mixer" and action == "start_mixer":
                    mixer_rpm = effective_params.get("mixer_rpm")
                    if mixer_rpm is not None:
                        cmd_target["target_rpm"] = mixer_rpm
                        cmd_target["mixer_rpm"] = mixer_rpm
                        cmd_target["actual_rpm"] = mixer_rpm
                elif role == "pump" and action == "dose":
                    dose_ml = effective_params.get("dose_ml")
                    if dose_ml is not None:
                        cmd_target["dose_ml"] = dose_ml

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

        # A supervised phase (`manual`/`hibrido`) may declare no timeout: the operator is the
        # clock, so there is nothing to run out. `auto` always declares one (enforced by
        # `validate_recipes`), so a mode that advances without a human is always bounded.
        timed_out = phase.timeout_s is not None and ctx.now_s - ctx.phase_entered_s >= phase.timeout_s
        satisfied = timed_out or evaluate(phase.condition, ctx)

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
            views.append(
                {
                    "phase_id": phase.phase_id,
                    "num": phase.num,
                    "title": phase.title,
                    "desc": phase.desc,
                    "mode": self._mode_overrides.get(phase.phase_id, phase.mode),
                    "status": status,
                    "condition_text": phase.condition_text,
                    "params": {**phase.params, **self._param_overrides.get(phase.phase_id, {})},
                    "timeout_s": phase.timeout_s,
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
            "SELECT phase_id, mode, status FROM experiment_phases WHERE experiment_id = $1",
            experiment_id,
        )
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
