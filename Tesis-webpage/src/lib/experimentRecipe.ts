/**
 * Receta de actuadores derivada de un experimento (AquaControl V4).
 *
 * Única fuente de verdad de la intención por actuador. Antes esta lógica estaba duplicada
 * dentro de `/api/experiments` y el orquestador (`broadcastState`) no la aplicaba en absoluto:
 * enviaba un `set_state` genérico y cada nodo adivinaba. El T-200 interpretaba que todo
 * `ACTIVE_EXPERIMENT` encendía el mixer y el ODrive no se armaba nunca.
 *
 * Mapa de protocolos (ADD §3):
 *   - Planta 1 (desoxigenación química): mixer ON (disuelve el sulfito), ODrive OFF.
 *   - Planta 2 (respuesta al escalón / KLa): mixer OFF, ODrive en MANUAL al escalón de RPM.
 *   - Caso B (lazo cerrado): mixer OFF, ODrive en PID con el setpoint de OD.
 *
 * El mixer se apaga explícitamente en los casos de aireación porque su agitación contamina la
 * identificación de KLa. La superposición mezcla/aireación es experimental (AGENTS.md regla 11),
 * por lo que el operador puede encenderlo de forma directa durante la fase de preparación.
 */

export type MotorMode = 'off' | 'manual' | 'pid';

export interface ActuatorRecipe {
  /** El mixer solo se enciende para la Planta 1; en el resto de casos se apaga. */
  mixer: 'on' | 'off';
  motor: {
    mode: MotorMode;
    /** Setpoint de OD (mg/L) cuando `mode === 'pid'`. */
    target_do?: number;
    /** Escalón de apertura (0-100 %) cuando `mode === 'manual'`. */
    throttle_pct?: number;
  };
}

export type ExperimentCaseType = 'planta_1_deox' | 'planta_2_step' | 'closed_loop';

/** Escalón por defecto si el experimento no lo especifica (%). */
export const DEFAULT_STEP_THROTTLE_PCT = 50;

/**
 * Resuelve la receta canónica de un experimento del registro.
 * Tolerante a registros parciales: cualquier campo ausente cae a un valor seguro (actuadores OFF).
 */
export function resolveActuatorRecipe(exp: any): ActuatorRecipe {
  const caseType = String(exp?.case_type || exp?.parameters?.case_type || '');
  const plant = String(exp?.plant_target || 'both');
  const controllerType = String(exp?.controller_type || 'none');
  const stepThrottle = Number(exp?.parameters?.step_throttle_pct ?? DEFAULT_STEP_THROTTLE_PCT);
  const setpoint = exp?.setpoint_do;

  // Planta 1 es el único caso que exige mezcla continua para disolver el Na2SO3.
  const mixer: 'on' | 'off' = caseType === 'planta_1_deox' ? 'on' : 'off';

  // El ODrive se arma cuando el protocolo involucra aireación mecánica.
  const wantsOdrive =
    caseType === 'planta_2_step' ||
    caseType === 'closed_loop' ||
    plant === 'planta_2' ||
    plant === 'both';

  if (!wantsOdrive) {
    return { mixer, motor: { mode: 'off' } };
  }

  const usePid = controllerType === 'pid' || caseType === 'closed_loop';
  if (usePid && setpoint !== null && setpoint !== undefined && Number.isFinite(Number(setpoint))) {
    return { mixer, motor: { mode: 'pid', target_do: Number(setpoint) } };
  }

  const throttle = Number.isFinite(stepThrottle) ? stepThrottle : DEFAULT_STEP_THROTTLE_PCT;
  return {
    mixer,
    motor: { mode: 'manual', throttle_pct: Math.min(100, Math.max(0, throttle)) }
  };
}

/**
 * Traduce la receta al payload plano que viaja en `set_state`.
 *
 * Va plano (no anidado) a propósito: los parsers de firmware usan `StaticJsonDocument`
 * de tamaño ajustado y ya leen campos planos (`mode`, `target_do`, `manual_throttle_pct`).
 * En IDLE y MANUAL_OVERRIDE todo se fuerza a OFF: una anulación manual debe abortar cualquier
 * receta automática (ADD §2.3).
 */
export function buildActuatorIntent(
  state: 'IDLE' | 'ACTIVE_EXPERIMENT' | 'MANUAL_OVERRIDE',
  recipe: ActuatorRecipe
): Record<string, string | number> {
  if (state !== 'ACTIVE_EXPERIMENT') {
    return { mixer: 'off', motor_mode: 'off' };
  }

  const intent: Record<string, string | number> = {
    mixer: recipe.mixer,
    motor_mode: recipe.motor.mode
  };
  if (recipe.motor.mode === 'pid' && recipe.motor.target_do !== undefined) {
    intent.motor_target_do = recipe.motor.target_do;
  }
  if (recipe.motor.mode === 'manual' && recipe.motor.throttle_pct !== undefined) {
    intent.motor_throttle_pct = recipe.motor.throttle_pct;
  }
  return intent;
}
