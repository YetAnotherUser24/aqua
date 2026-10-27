"""
Unified event model.

Three producers write to one stream — firmware, gateway, and UI — with an
identical shape, so the experiment export can merge them without special cases.

The previous system constrained ``motor_events.event_type`` to six values and
``alerts.alert_type`` to seven. That described a thruster starting and stopping;
it does not describe a multi-node experiment. The catalogue here lives in
``docs/EVENTS.md`` and is enumerated below so an unknown type is a validation
error rather than a silently-accepted string.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Scope(str, Enum):
    SYSTEM = "system"
    NODE = "node"
    EXPERIMENT = "experiment"
    PROCESS = "process"
    OPERATOR = "operator"
    DATA = "data"


class Source(str, Enum):
    """Who observed the event. Provenance changes what the event *means*."""

    FIRMWARE = "firmware"
    GATEWAY = "gateway"
    UI = "ui"


class EventType(str, Enum):
    """The full catalogue. See docs/EVENTS.md for semantics and severity guidance."""

    # -- system
    SYSTEM_BOOT = "system_boot"
    SYSTEM_SHUTDOWN = "system_shutdown"
    STATE_CHANGE = "state_change"
    FAILSAFE_ENGAGED = "failsafe_engaged"
    FAILSAFE_CLEARED = "failsafe_cleared"
    WATCHDOG_RESET = "watchdog_reset"
    BROWNOUT = "brownout"
    LOW_VOLTAGE = "low_voltage"
    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"
    PROTOCOL_ERROR = "protocol_error"

    # -- experiment
    EXPERIMENT_CREATED = "experiment_created"
    EXPERIMENT_STARTED = "experiment_started"
    EXPERIMENT_PAUSED = "experiment_paused"
    EXPERIMENT_RESUMED = "experiment_resumed"
    EXPERIMENT_COMPLETED = "experiment_completed"
    EXPERIMENT_ABORTED = "experiment_aborted"
    EXPERIMENT_PHASE_CHANGE = "experiment_phase_change"

    # -- connectivity
    NODE_ONLINE = "node_online"
    NODE_OFFLINE = "node_offline"
    NODE_RECONNECTED = "node_reconnected"
    NODE_REBOOTED = "node_rebooted"
    TELEMETRY_GAP_DETECTED = "telemetry_gap_detected"
    TELEMETRY_GAP_RECOVERED = "telemetry_gap_recovered"
    OUT_OF_ORDER_BATCH = "out_of_order_batch"
    CLOCK_RESYNCED = "clock_resynced"
    CLOCK_OFFSET_EXCEEDED = "clock_offset_exceeded"
    CLOCK_UNCERTAINTY_HIGH = "clock_uncertainty_high"
    LINK_QUALITY_DEGRADED = "link_quality_degraded"

    # -- actuator
    ACTUATOR_START = "actuator_start"
    ACTUATOR_STOP = "actuator_stop"
    START_MIXER = "start_mixer"
    STOP_MIXER = "stop_mixer"
    SETPOINT_CHANGE = "setpoint_change"
    SPEED_REACHED = "speed_reached"
    DIRECTION_CHANGE = "direction_change"
    ACTUATOR_STALL = "actuator_stall"
    ACTUATOR_FAULT = "actuator_fault"

    # -- dosing
    DOSE_STARTED = "dose_started"
    DOSE_COMPLETED = "dose_completed"
    DOSE_FINISHED = "dose_finished"
    DOSE_TIMEOUT = "dose_timeout"
    DOSE_ABORTED = "dose_aborted"
    DOSE_CALIBRATION_CHANGED = "dose_calibration_changed"
    DOSE_VOLUME_UNREACHABLE = "dose_volume_unreachable"
    PRIME_STARTED = "prime_started"
    PRIME_COMPLETED = "prime_completed"
    DRAIN_STARTED = "drain_started"
    DRAIN_COMPLETED = "drain_completed"
    FAULT = "fault"

    # -- hardware faults
    OVER_CURRENT = "over_current"
    OVER_VOLTAGE = "over_voltage"
    UNDER_VOLTAGE = "under_voltage"
    OVER_TEMPERATURE = "over_temperature"
    ENCODER_ERROR = "encoder_error"
    UART_TIMEOUT = "uart_timeout"
    ESC_FAULT = "esc_fault"
    TACHOMETER_LOST = "tachometer_lost"

    # -- sensor
    READING_OUT_OF_RANGE = "reading_out_of_range"
    READING_FROZEN = "reading_frozen"
    CALIBRATION_STARTED = "calibration_started"
    CALIBRATION_COMPLETED = "calibration_completed"
    CALIBRATION_DRIFT = "calibration_drift"
    PROBE_REMOVED = "probe_removed"
    BATTERY_LOW = "battery_low"
    BATTERY_CRITICAL = "battery_critical"
    RTC_BATTERY_LOW = "rtc_battery_low"
    RTC_DRIFT = "rtc_drift"
    SD_WRITE_ERROR = "sd_write_error"
    SD_FULL = "sd_full"
    SD_RECOVERED = "sd_recovered"

    # -- process (gateway-derived, domain rules)
    DO_LOW_WARNING = "do_low_warning"
    DO_LOW_CRITICAL = "do_low_critical"
    DO_RECOVERED = "do_recovered"
    DO_DROP_RATE_EXCEEDED = "do_drop_rate_exceeded"
    DO_RISE_RATE_EXCEEDED = "do_rise_rate_exceeded"
    DO_IMPLAUSIBLE_JUMP = "do_implausible_jump"
    TEMP_HIGH = "temp_high"
    TEMP_LOW = "temp_low"
    TEMP_RATE_EXCEEDED = "temp_rate_exceeded"
    KLA_DEGRADATION_SUSPECTED = "kla_degradation_suspected"
    AERATION_INEFFECTIVE = "aeration_ineffective"

    # -- operator
    MANUAL_OVERRIDE_ENABLED = "manual_override_enabled"
    MANUAL_OVERRIDE_DISABLED = "manual_override_disabled"
    COMMAND_ISSUED = "command_issued"
    COMMAND_ACKNOWLEDGED = "command_acknowledged"
    COMMAND_REJECTED = "command_rejected"
    COMMAND_FAILED = "command_failed"
    COMMAND_EXPIRED = "command_expired"
    COMMAND_SUPERSEDED = "command_superseded"
    CONFIG_CHANGED = "config_changed"
    RECIPE_APPLIED = "recipe_applied"
    EMERGENCY_STOP = "emergency_stop"
    EXPERIMENT_EXPORT_REQUESTED = "experiment_export_requested"
    #: An operator-documented point on the run's timeline — a dose, a step, a manual
    #: Winkler sample. Stored as an event rather than its own table so the export, which
    #: already merges events, carries it for free, and so the chart can overlay it by
    #: querying the same stream it already reads.
    MILESTONE_RECORDED = "milestone_recorded"

    # -- data pipeline
    ARCHIVE_FILE_CLOSED = "archive_file_closed"
    ARCHIVE_ROTATE_STARTED = "archive_rotate_started"
    ARCHIVE_ROTATE_COMPLETED = "archive_rotate_completed"
    ARCHIVE_ROTATE_FAILED = "archive_rotate_failed"
    FLUSH_STARTED = "flush_started"
    FLUSH_COMPLETED = "flush_completed"
    FLUSH_FAILED = "flush_failed"
    ARCHIVE_VERIFY_FAILED = "archive_verify_failed"
    MANIFEST_INCONSISTENT = "manifest_inconsistent"
    RETENTION_PRUNED = "retention_pruned"
    RETENTION_BLOCKED_UNFLUSHED = "retention_blocked_unflushed"
    DISK_USAGE_WARNING = "disk_usage_warning"
    DISK_USAGE_CRITICAL = "disk_usage_critical"
    EXPORT_STARTED = "export_started"
    EXPORT_COMPLETED = "export_completed"
    EXPORT_FAILED = "export_failed"


#: Default severity per type, so callers only override when they mean to.
_DEFAULT_SEVERITY: dict[EventType, Severity] = {
    EventType.NODE_OFFLINE: Severity.WARNING,
    EventType.TELEMETRY_GAP_DETECTED: Severity.WARNING,
    EventType.OUT_OF_ORDER_BATCH: Severity.WARNING,
    EventType.CLOCK_OFFSET_EXCEEDED: Severity.WARNING,
    EventType.CLOCK_UNCERTAINTY_HIGH: Severity.WARNING,
    EventType.LINK_QUALITY_DEGRADED: Severity.WARNING,
    EventType.PROTOCOL_VERSION_MISMATCH: Severity.WARNING,
    EventType.PROTOCOL_ERROR: Severity.WARNING,
    EventType.DO_LOW_WARNING: Severity.WARNING,
    EventType.BATTERY_LOW: Severity.WARNING,
    EventType.RTC_DRIFT: Severity.WARNING,
    EventType.SD_FULL: Severity.WARNING,
    EventType.FLUSH_FAILED: Severity.WARNING,
    EventType.ARCHIVE_ROTATE_FAILED: Severity.WARNING,
    EventType.RETENTION_BLOCKED_UNFLUSHED: Severity.WARNING,
    EventType.DISK_USAGE_WARNING: Severity.WARNING,
    EventType.EXPORT_FAILED: Severity.WARNING,
    EventType.COMMAND_EXPIRED: Severity.WARNING,
    EventType.COMMAND_FAILED: Severity.WARNING,
    EventType.DOSE_TIMEOUT: Severity.WARNING,
    EventType.ACTUATOR_STALL: Severity.WARNING,
    EventType.TEMP_HIGH: Severity.WARNING,
    # Critical
    EventType.DO_LOW_CRITICAL: Severity.CRITICAL,
    EventType.BROWNOUT: Severity.CRITICAL,
    EventType.LOW_VOLTAGE: Severity.CRITICAL,
    EventType.BATTERY_CRITICAL: Severity.CRITICAL,
    EventType.ARCHIVE_VERIFY_FAILED: Severity.CRITICAL,
    EventType.MANIFEST_INCONSISTENT: Severity.CRITICAL,
    EventType.DISK_USAGE_CRITICAL: Severity.CRITICAL,
    EventType.SD_WRITE_ERROR: Severity.CRITICAL,
    EventType.PROBE_REMOVED: Severity.CRITICAL,
    EventType.ACTUATOR_FAULT: Severity.CRITICAL,
    EventType.FAULT: Severity.CRITICAL,
    EventType.ESC_FAULT: Severity.CRITICAL,
    EventType.OVER_CURRENT: Severity.CRITICAL,
    EventType.OVER_VOLTAGE: Severity.CRITICAL,
    EventType.OVER_TEMPERATURE: Severity.CRITICAL,
    EventType.EMERGENCY_STOP: Severity.CRITICAL,
    EventType.FAILSAFE_ENGAGED: Severity.CRITICAL,
}

#: Default scope per type.
_DEFAULT_SCOPE: dict[EventType, Scope] = {
    EventType.SYSTEM_BOOT: Scope.SYSTEM,
    EventType.SYSTEM_SHUTDOWN: Scope.SYSTEM,
    EventType.STATE_CHANGE: Scope.SYSTEM,
    EventType.PROTOCOL_ERROR: Scope.SYSTEM,
    EventType.PROTOCOL_VERSION_MISMATCH: Scope.SYSTEM,
    # An operator's annotation is about the run, but its provenance is the operator, and
    # the "operator" scope is what the dashboard filters on for manual interventions.
    EventType.MILESTONE_RECORDED: Scope.OPERATOR,
}
for _t in EventType:
    _DEFAULT_SCOPE.setdefault(
        _t,
        Scope.EXPERIMENT
        if _t.value.startswith("experiment_")
        else Scope.DATA
        if _t
        in {
            EventType.ARCHIVE_FILE_CLOSED,
            EventType.ARCHIVE_ROTATE_STARTED,
            EventType.ARCHIVE_ROTATE_COMPLETED,
            EventType.ARCHIVE_ROTATE_FAILED,
            EventType.FLUSH_STARTED,
            EventType.FLUSH_COMPLETED,
            EventType.FLUSH_FAILED,
            EventType.ARCHIVE_VERIFY_FAILED,
            EventType.MANIFEST_INCONSISTENT,
            EventType.RETENTION_PRUNED,
            EventType.RETENTION_BLOCKED_UNFLUSHED,
            EventType.DISK_USAGE_WARNING,
            EventType.DISK_USAGE_CRITICAL,
            EventType.EXPORT_STARTED,
            EventType.EXPORT_COMPLETED,
            EventType.EXPORT_FAILED,
        }
        else Scope.OPERATOR
        if _t.value.startswith(("command_", "manual_override", "recipe_", "config_"))
        else Scope.PROCESS
        if _t.value.startswith(("do_", "temp_", "kla_", "aeration_"))
        else Scope.NODE,
    )


@dataclass(frozen=True, slots=True)
class Event:
    """One row in the unified stream."""

    event_type: EventType
    severity: Severity
    scope: Scope
    source: Source
    ts: float
    """Gateway-stamped arrival time (epoch seconds). Always trustworthy."""
    experiment_id: str | None = None
    node_id: str | None = None
    t_dev_ms: int | None = None
    """Device clock at origin, when the producer had one."""
    correlation_id: str | None = None
    action: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def make(
        cls,
        event_type: EventType,
        *,
        source: Source,
        experiment_id: str | None = None,
        node_id: str | None = None,
        t_dev_ms: int | None = None,
        severity: Severity | None = None,
        scope: Scope | None = None,
        correlation_id: str | None = None,
        action: str | None = None,
        detail: Mapping[str, Any] | None = None,
        ts: float | None = None,
    ) -> "Event":
        """Construct with catalogue defaults. Severity/scope only passed when overriding."""
        return cls(
            event_type=event_type,
            severity=severity or _DEFAULT_SEVERITY.get(event_type, Severity.INFO),
            scope=scope or _DEFAULT_SCOPE[event_type],
            source=source,
            ts=ts if ts is not None else time.time(),
            experiment_id=experiment_id,
            node_id=node_id,
            t_dev_ms=t_dev_ms,
            correlation_id=correlation_id,
            action=action,
            detail=dict(detail or {}),
        )


def default_severity(event_type: EventType) -> Severity:
    return _DEFAULT_SEVERITY.get(event_type, Severity.INFO)


def default_scope(event_type: EventType) -> Scope:
    return _DEFAULT_SCOPE[event_type]
