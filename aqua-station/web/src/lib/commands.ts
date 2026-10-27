/**
 * The command vocabulary, per role.
 *
 * The console used to offer one global action list, and it matched no node in particular:
 * the mixer understands `start_mixer`, not `start`, so the obvious button was rejected; the
 * pump reads `target_ml`, so the pre-filled `{"ml": 50}` was silently ignored and the dose
 * never happened. Both looked like a broken node rather than a wrong verb, because a
 * `rejected` command and a dead MQTT link are indistinguishable from the operator's seat.
 *
 * Every verb here is one a node's own command handler actually branches on, and every
 * target is a key that handler actually reads. Keep in step with:
 *
 *   od_sensor  nodo-sensor-od/src/main.cpp
 *   aerator    nodo-aerador/src/cloud_worker.cpp
 *   mixer      nodo-mezclador/src/cloud_worker.cpp
 *   pump       nodo-bomba/src/main.cpp
 *
 * The safety verbs (`emergency_stop`, `clear_estop`, `failsafe_clear`) are deliberately not
 * here: they are role-agnostic, live on their own row, and bypass the outbox. See
 * `SAFETY_ACTIONS`.
 */

export interface RoleCommand {
  action: string;
  /**
   * Default target. Present so an action that is meaningless without a parameter — a dose
   * with no volume, a speed with no setpoint — is never sent with an empty object.
   */
  target: Record<string, unknown>;
}

/**
 * `force_on` is omitted everywhere on purpose. It runs the actuator with no bound at all,
 * which for a peristaltic pump means an unbounded dose into the tank. A manual override
 * belongs behind a deliberate action, not on a dropdown next to `dose`.
 */
export const ROLE_COMMANDS: Record<string, readonly RoleCommand[]> = {
  od_sensor: [
    { action: 'start', target: { interval_sec: 5 } },
    { action: 'stop', target: {} },
    { action: 'set_sampling_rate', target: { interval_sec: 5 } },
    { action: 'manual_sample', target: {} },
  ],
  aerator: [
    { action: 'set_mode', target: { mode: 'pid' } },
    { action: 'set_speed', target: { speed_percent: 20 } },
    { action: 'start', target: { speed_percent: 20 } },
    { action: 'stop', target: {} },
    { action: 'set_state', target: { state: 'IDLE' } },
  ],
  mixer: [
    { action: 'start_mixer', target: { target_rpm: 600 } },
    { action: 'stop_mixer', target: {} },
    // `set_state` with `mixer` is the orchestrator's form of the same start/stop, and the
    // only one that carries an experiment id.
    { action: 'set_state', target: { mixer: 'on', target_rpm: 600 } },
  ],
  pump: [
    { action: 'dose', target: { target_ml: 38 } },
    { action: 'prime', target: { volume_ml: 25 } },
    { action: 'drain', target: { duration_s: 35 } },
    { action: 'suckback', target: {} },
    { action: 'stop', target: {} },
    { action: 'force_off', target: {} },
  ],
};

/**
 * Commands for a role, falling back to a generic set for a role this build has never
 * heard of — a node added to `roles.yaml` should still be reachable from the console.
 */
const GENERIC: readonly RoleCommand[] = [
  { action: 'start', target: {} },
  { action: 'stop', target: {} },
  { action: 'set_state', target: { state: 'IDLE' } },
];

export function commandsFor(role: string): readonly RoleCommand[] {
  return ROLE_COMMANDS[role] ?? GENERIC;
}
