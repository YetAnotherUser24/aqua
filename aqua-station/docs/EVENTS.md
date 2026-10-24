# Event taxonomy

The previous system had `motor_events.event_type` constrained to six values and `alerts.alert_type`
to seven. That was adequate when the only thing that happened was a thruster starting and stopping.
It does not describe a multi-node experiment.

Every event is one row in a single append-only stream. Three producers write to it — **firmware**,
**gateway**, and **UI** — with an identical shape, so the experiment export can merge without
special cases.

## Record shape

| Field | Type | Notes |
|---|---|---|
| `ts` | timestamptz | Gateway-stamped arrival time, always trustworthy |
| `t_dev_ms` | bigint, nullable | Device clock at origin, when the producer has one |
| `experiment_id` | text, nullable | Null for system/pipeline events outside an experiment |
| `event_type` | text | From the catalogue below |
| `severity` | enum | `info` \| `warning` \| `critical` |
| `scope` | enum | `system` \| `node` \| `experiment` \| `process` \| `operator` \| `data` |
| `source` | enum | `firmware` \| `gateway` \| `ui` |
| `node_id` | uuid, nullable | Which node, when applicable |
| `correlation_id` | text, nullable | Ties a command → ack → effect chain together |
| `action` | text, nullable | Short machine-readable verb |
| `detail` | jsonb | Free-form payload |

**Why `source` matters:** a `do_low_critical` produced by the gateway from a threshold rule is a
different claim from one reported by the probe as a hardware fault. Merging them without recording
provenance would make the event log ambiguous.

## Catalogue

### System — `scope: system`
`system_boot`, `system_shutdown`, `state_change` *(IDLE → ACTIVE_EXPERIMENT → MANUAL_OVERRIDE)*,
`failsafe_engaged`, `failsafe_cleared`, `watchdog_reset`, `brownout`, `low_voltage`,
`protocol_version_mismatch`, `protocol_error`

### Experiment — `scope: experiment`
`experiment_created`, `experiment_started`, `experiment_paused`, `experiment_resumed`,
`experiment_completed`, `experiment_aborted`, `experiment_phase_change`

### Connectivity — `scope: node`
`node_online`, `node_offline`, `node_reconnected`, `node_rebooted`,
`telemetry_gap_detected`, `telemetry_gap_recovered`, `out_of_order_batch`,
`clock_resynced`, `clock_offset_exceeded`, `clock_uncertainty_high`,
`link_quality_degraded`

> `node_online` / `node_offline` come **free from MQTT Last Will & Testament** and are retained, so
> a subscriber that connects late still learns current presence. This replaces the previous
> 60-second heartbeat heuristic entirely.

### Actuator — `scope: node`
`actuator_start`, `actuator_stop`, `setpoint_change`, `speed_reached`, `direction_change`,
`actuator_stall`, `actuator_fault`

### Dosing — `scope: node`
`dose_started`, `dose_completed`, `dose_timeout`, `dose_aborted`, `dose_calibration_changed`,
`dose_volume_unreachable`

### Hardware faults — `scope: node`
`over_current`, `over_voltage`, `under_voltage`, `over_temperature`, `encoder_error`,
`uart_timeout`, `esc_fault`, `tachometer_lost`

### Sensor — `scope: node`
`reading_out_of_range`, `reading_frozen`, `calibration_started`, `calibration_completed`,
`calibration_drift`, `probe_removed`, `battery_low`, `battery_critical`, `rtc_battery_low`,
`rtc_drift`, `sd_write_error`, `sd_full`, `sd_recovered`

### Process — `scope: process` *(gateway-derived, domain rules)*
`do_low_warning`, `do_low_critical`, `do_recovered`, `do_drop_rate_exceeded`,
`do_rise_rate_exceeded`, `do_implausible_jump`, `temp_high`, `temp_low`,
`temp_rate_exceeded`, `kla_degradation_suspected`, `aeration_ineffective`

### Operator — `scope: operator`
`manual_override_enabled`, `manual_override_disabled`, `command_issued`, `command_acknowledged`,
`command_rejected`, `command_failed`, `command_expired`, `command_superseded`,
`config_changed`, `recipe_applied`, `emergency_stop`, `experiment_export_requested`

### Data pipeline — `scope: data` *(gateway/timer only)*
`archive_file_closed`, `archive_rotate_started`, `archive_rotate_completed`,
`archive_rotate_failed`, `flush_started`, `flush_completed`, `flush_failed`,
`archive_verify_failed`, `manifest_inconsistent`, `retention_pruned`,
`retention_blocked_unflushed`, `disk_usage_warning`, `disk_usage_critical`,
`export_started`, `export_completed`, `export_failed`

## Severity guidance

| Severity | Rule of thumb | Examples |
|---|---|---|
| `info` | Expected, useful for the experiment narrative | `experiment_started`, `dose_completed`, `node_online` |
| `warning` | Degraded but self-recovering; investigate after | `telemetry_gap_detected`, `battery_low`, `clock_offset_exceeded`, `flush_failed` |
| `critical` | Requires attention now; may have lost data or safety margin | `do_low_critical`, `brownout`, `archive_verify_failed`, `disk_usage_critical` |

## Buffering and replay

Events originate on nodes that may be offline. **Nodes must buffer events to SD and replay them**
when the link returns — the same store-and-forward pattern the OD logger already uses for
measurements. An event log that silently loses its most interesting entries (the faults, the
brownouts) is worse than no event log, because it implies coverage it does not have.

Replayed events carry the original `t_dev_ms`; the gateway preserves it and adds its own `ts`, and
marks the detail with the replay window so late arrivals are visible in exports.

## Retention

Events are small and disproportionate in value, so they are retained far longer than telemetry:
they live in PostgreSQL indefinitely, and are additionally archived into each experiment bundle and
into `events/YYYY-MM.csv.gz` alongside the telemetry flush.
