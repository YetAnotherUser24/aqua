"""
Domain rules that turn telemetry into process events.

Three properties make this usable rather than noise:

**Hysteresis.** A rule fires on *transition*, not on every sample. At 5 Hz, an
ungated ``do < 3.0`` check would write 300 identical events per minute and bury
everything else. Each rule keeps a latched level and only emits when it changes,
with a deadband on the way back so a value hovering at the threshold does not flap.

**Authority.** Dissolved oxygen exists on two nodes: authoritatively on ``od_sensor``
and echoed on the aerator as ``peer_do_mg_l`` for its own control loop. Evaluating
``peer_do_mg_l`` would double every DO alert and, worse, let a stale ESP-NOW echo
raise an alert about water the sensor is not reporting. So the engine asks the
registry which role is authoritative and ignores the echo entirely.

**Rate limits are per minute, not per sample.** ``do_drop_rate_exceeded`` is expressed
in mg/L/min so the threshold means the same thing whether the node is sampling at
5 Hz during an experiment or once a minute while idle. Holding that to be true is not a
matter of units alone: normalising a *single* interval to a minute divides by its own
length, so the same sensor noise is worth 300x more at 200 ms than at 60 s and the
threshold stops meaning anything in common. The estimator is therefore a least-squares
slope over ``do_rate_window_ms``, and it reports nothing until that much history exists.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping

from ..config import Settings
from ..orchestration.conditions import least_squares_rate_per_min
from ..schema.registry import RoleSchema, SchemaRegistry
from .model import Event, EventType, Severity, Source

log = logging.getLogger("aqua.events.derived")


class Level(IntEnum):
    """Ordered so comparisons read naturally: ``level >= Level.WARNING``."""

    OK = 0
    WARNING = 1
    CRITICAL = 2


@dataclass(slots=True)
class _Latch:
    """Per-(role, rule) latched state."""

    level: Level = Level.OK
    value: float | None = None
    t_dev_ms: int | None = None
    """Device time of the previous sample; the reference for adjacent-sample checks."""
    #: Recent ``(t_dev_ms, value)`` pairs, pruned to ``do_rate_window_ms``. The rate is
    #: estimated from these rather than from the last two samples.
    recent: deque[tuple[int, float]] = field(default_factory=deque)
    #: True while a drop-rate condition is active, so it fires on transition instead of on
    #: every sample that still satisfies it.
    rate_alarm: bool = False


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Rule parameters. Defaults are overridden from settings where they exist."""

    do_warning_mg_l: float = 5.0
    do_critical_mg_l: float = 3.0
    #: Deadband on recovery, so a value sitting on the threshold does not flap.
    do_recovery_deadband_mg_l: float = 0.3
    #: Sustained fall faster than this raises a rate event.
    do_drop_rate_mg_l_per_min: float = 0.5
    #: Span the trend is estimated over, as a duration rather than a sample count, so a
    #: 5 Hz node and a 0.2 Hz node are judged on the same amount of history. Two minutes
    #: is long enough for a slope to sit well clear of sensor noise, and short enough that
    #: the alarm still leads the absolute level rules.
    do_rate_window_ms: int = 120_000
    #: A step larger than this between adjacent samples is a sensor artefact, not water.
    do_jump_mg_l: float = 1.0
    temp_warning_c: float = 30.0
    temp_recovery_deadband_c: float = 0.5
    battery_warning_v: float = 3.5


@dataclass(slots=True)
class _DerivedState:
    latches: dict[tuple[str, str], _Latch] = field(default_factory=dict)

    def latch(self, role: str, rule: str) -> _Latch:
        return self.latches.setdefault((role, rule), _Latch())


class DerivedRules:
    """
    Evaluates domain rules against incoming samples.

    Pure with respect to I/O: it returns events and never touches the database, which
    is what makes every rule testable by feeding it a sequence of values.
    """

    def __init__(
        self,
        settings: Settings,
        registry: SchemaRegistry,
        thresholds: Thresholds | None = None,
    ) -> None:
        self.registry = registry
        self.thresholds = thresholds or Thresholds(
            do_warning_mg_l=settings.do_warning_mg_l,
            do_critical_mg_l=settings.do_critical_mg_l,
            temp_warning_c=settings.temp_warning_c,
        )
        self._state = _DerivedState()
        self.events_emitted = 0

    # -- public entry point ------------------------------------------------

    def evaluate(
        self,
        schema: RoleSchema,
        row: Mapping[str, Any],
        *,
        t_dev_ms: int,
        node_id: str | None,
        experiment_id: str | None,
    ) -> list[Event]:
        """Run every applicable rule for one sample. Returns zero or more events."""
        events: list[Event] = []
        events.extend(self._evaluate_do(schema, row, t_dev_ms, node_id, experiment_id))
        events.extend(self._evaluate_temperature(schema, row, t_dev_ms, node_id, experiment_id))
        events.extend(self._evaluate_battery(schema, row, t_dev_ms, node_id, experiment_id))
        self.events_emitted += len(events)
        return events

    # -- dissolved oxygen --------------------------------------------------

    def _authoritative(self, quantity: str, schema: RoleSchema) -> bool:
        """
        Whether this role owns the quantity.

        Enforced here rather than trusted upstream because the alternative — an alert
        raised from the aerator's ESP-NOW echo — looks identical in the event log and
        would be silently wrong.
        """
        owner = self.registry.authoritative_role_for(quantity)
        if owner is None:
            # No role declares it, so accept any role that actually reports the field.
            return True
        return owner.role == schema.role

    def _evaluate_do(
        self,
        schema: RoleSchema,
        row: Mapping[str, Any],
        t_dev_ms: int,
        node_id: str | None,
        experiment_id: str | None,
    ) -> list[Event]:
        value = row.get("od_do_mg_l")
        if value is None or not self._authoritative("dissolved_oxygen", schema):
            return []

        level = float(value)
        latch = self._state.latch(schema.role, "do")
        events: list[Event] = []

        # -- jump, on adjacent samples -----------------------------------------
        if latch.value is not None and latch.t_dev_ms is not None:
            elapsed_ms = t_dev_ms - latch.t_dev_ms
            if elapsed_ms > 0:
                delta = level - latch.value

                if abs(delta) >= self.thresholds.do_jump_mg_l:
                    # Adjacent samples deliberately: a jump *is* a step between neighbours,
                    # so smoothing it would hide exactly what this looks for.
                    # Deliberately not latched either: a step change is an instantaneous
                    # observation, not a condition, and each one is worth recording.
                    events.append(
                        Event.make(
                            EventType.DO_IMPLAUSIBLE_JUMP,
                            source=Source.GATEWAY,
                            node_id=node_id,
                            experiment_id=experiment_id,
                            t_dev_ms=t_dev_ms,
                            detail={"from": latch.value, "to": level, "delta": round(delta, 3)},
                        )
                    )

        # -- rate, over a span rather than over a single interval ---------------
        rate_per_min = self._do_trend(latch, t_dev_ms, level)
        if rate_per_min is not None:
            exceeded = rate_per_min <= -self.thresholds.do_drop_rate_mg_l_per_min
            # Latched, like the level rules: a decline that continues is one condition, not
            # one event per sample. Unlatched, this wrote an event every five seconds for as
            # long as the water kept falling -- and, with the window above, no longer for a
            # single noisy sample.
            if exceeded and not latch.rate_alarm:
                events.append(
                    Event.make(
                        EventType.DO_DROP_RATE_EXCEEDED,
                        source=Source.GATEWAY,
                        node_id=node_id,
                        experiment_id=experiment_id,
                        t_dev_ms=t_dev_ms,
                        detail={"rate_mg_l_per_min": round(rate_per_min, 3), "value": level},
                    )
                )
            latch.rate_alarm = exceeded

        # -- latched level, emits only on transition --------------------------
        if level < self.thresholds.do_critical_mg_l:
            new_level = Level.CRITICAL
        elif level < self.thresholds.do_warning_mg_l:
            new_level = Level.WARNING
        elif level >= self.thresholds.do_warning_mg_l + self.thresholds.do_recovery_deadband_mg_l:
            new_level = Level.OK
        else:
            # Inside the deadband: hold the current level rather than flapping.
            new_level = latch.level if latch.level != Level.OK else Level.OK

        if new_level != latch.level:
            if new_level is Level.CRITICAL:
                events.append(
                    self._do_event(EventType.DO_LOW_CRITICAL, schema, level, t_dev_ms, node_id, experiment_id)
                )
            elif new_level is Level.WARNING:
                events.append(self._do_event(EventType.DO_LOW_WARNING, schema, level, t_dev_ms, node_id, experiment_id))
            elif latch.level is Level.WARNING:
                events.append(self._do_event(EventType.DO_RECOVERED, schema, level, t_dev_ms, node_id, experiment_id))
            elif latch.level is Level.CRITICAL:
                # Coming out of critical passes through warning, which is itself worth
                # recording: "recovered to warning" is not "recovered".
                events.append(self._do_event(EventType.DO_LOW_WARNING, schema, level, t_dev_ms, node_id, experiment_id))
            latch.level = new_level

        latch.value = level
        latch.t_dev_ms = t_dev_ms
        return events

    def _do_trend(self, latch: _Latch, t_dev_ms: int, level: float) -> float | None:
        """
        Least-squares slope of the recent DO points, in mg/L per minute.

        ``None`` until the retained points span ``do_rate_window_ms``. Both halves of that
        matter: a slope needs a *span* to be a rate at all, and holding the span in the time
        domain -- rather than as a sample count -- is what makes the threshold mean the same
        thing at 5 Hz and at one sample a minute.

        The slope, rather than the difference between the window's two endpoints, is the
        filter. Endpoint differencing over two minutes still reads a 0.5 mg/L wobble as a
        0.5 mg/L/min *difference*; a regression over the same points spreads that noise
        across every sample and puts the floor far below the threshold. That was the failure
        being fixed here: dividing a single interval by its own length multiplied the noise
        by ``60000 / elapsed_ms``, so at 5 Hz a 0.0017 mg/L wobble looked like a
        0.5 mg/L/min drop.
        """
        window_ms = self.thresholds.do_rate_window_ms
        recent = latch.recent
        recent.append((t_dev_ms, level))

        horizon = t_dev_ms - window_ms
        while recent and recent[0][0] < horizon:
            recent.popleft()

        if len(recent) < 2:
            return None
        if recent[-1][0] - recent[0][0] < window_ms:
            return None

        # Shared with the orchestrator's phase conditions so the two cannot disagree about
        # what "the rate of change" means. See orchestration/conditions.py.
        return least_squares_rate_per_min(recent)

    def _do_event(
        self,
        event_type: EventType,
        schema: RoleSchema,
        value: float,
        t_dev_ms: int,
        node_id: str | None,
        experiment_id: str | None,
    ) -> Event:
        return Event.make(
            event_type,
            source=Source.GATEWAY,
            node_id=node_id,
            experiment_id=experiment_id,
            t_dev_ms=t_dev_ms,
            severity=Severity.CRITICAL
            if event_type is EventType.DO_LOW_CRITICAL
            else Severity.WARNING
            if event_type is EventType.DO_LOW_WARNING
            else Severity.INFO,
            detail={
                "value_mg_l": round(value, 3),
                "warning_mg_l": self.thresholds.do_warning_mg_l,
                "critical_mg_l": self.thresholds.do_critical_mg_l,
                "source_role": schema.role,
            },
        )

    # -- temperature -------------------------------------------------------

    def _evaluate_temperature(
        self,
        schema: RoleSchema,
        row: Mapping[str, Any],
        t_dev_ms: int,
        node_id: str | None,
        experiment_id: str | None,
    ) -> list[Event]:
        value = row.get("od_temp_c")
        if value is None or not self._authoritative("water_temperature", schema):
            return []

        temperature = float(value)
        latch = self._state.latch(schema.role, "temp")
        events: list[Event] = []

        if latch.level is Level.OK and temperature > self.thresholds.temp_warning_c:
            latch.level = Level.WARNING
            events.append(
                Event.make(
                    EventType.TEMP_HIGH,
                    source=Source.GATEWAY,
                    node_id=node_id,
                    experiment_id=experiment_id,
                    t_dev_ms=t_dev_ms,
                    detail={"value_c": round(temperature, 2), "threshold_c": self.thresholds.temp_warning_c},
                )
            )
        elif latch.level is Level.WARNING and temperature < (
            self.thresholds.temp_warning_c - self.thresholds.temp_recovery_deadband_c
        ):
            latch.level = Level.OK
            events.append(
                Event.make(
                    EventType.TEMP_LOW,
                    source=Source.GATEWAY,
                    node_id=node_id,
                    experiment_id=experiment_id,
                    t_dev_ms=t_dev_ms,
                    detail={"value_c": round(temperature, 2), "note": "returned below the high-temperature threshold"},
                )
            )

        latch.value = temperature
        latch.t_dev_ms = t_dev_ms
        return events

    # -- battery -----------------------------------------------------------

    def _evaluate_battery(
        self,
        schema: RoleSchema,
        row: Mapping[str, Any],
        t_dev_ms: int,
        node_id: str | None,
        experiment_id: str | None,
    ) -> list[Event]:
        value = row.get("pwr_battery_v")
        if value is None:
            return []

        voltage = float(value)
        latch = self._state.latch(schema.role, "battery")
        events: list[Event] = []

        # Two levels, and the critical one is one-way: a logger battery that recovers
        # without intervention means someone changed it, which is an operator fact, not
        # a telemetry one.
        if latch.level is Level.OK and voltage < self.thresholds.battery_warning_v:
            latch.level = Level.WARNING
            events.append(
                Event.make(
                    EventType.BATTERY_LOW,
                    source=Source.GATEWAY,
                    node_id=node_id,
                    experiment_id=experiment_id,
                    t_dev_ms=t_dev_ms,
                    detail={"voltage_v": round(voltage, 3), "threshold_v": self.thresholds.battery_warning_v},
                )
            )
        elif latch.level is Level.WARNING and voltage < self.thresholds.battery_warning_v - 0.2:
            latch.level = Level.CRITICAL
            events.append(
                Event.make(
                    EventType.BATTERY_CRITICAL,
                    source=Source.GATEWAY,
                    node_id=node_id,
                    experiment_id=experiment_id,
                    t_dev_ms=t_dev_ms,
                    detail={"voltage_v": round(voltage, 3)},
                )
            )

        latch.value = voltage
        latch.t_dev_ms = t_dev_ms
        return events

    # -- introspection -----------------------------------------------------

    def current_levels(self) -> dict[str, dict[str, str]]:
        """Latched state, for ``/healthz`` and for debugging a quiet alert."""
        return {
            role: {
                rule: Level(latch.level).name
                for rule, latch in ((k[1], v) for k, v in self._state.latches.items() if k[0] == role)
            }
            for role in {key[0] for key in self._state.latches}
        }

    def stats(self) -> dict[str, Any]:
        return {"events_emitted": self.events_emitted, "latches": len(self._state.latches)}
