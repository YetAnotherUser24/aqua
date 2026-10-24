/**
 * ODrive S1 Status & Error Decoder (Protocolo ASCII / v0.6+)
 * 
 * Traduce estados de eje (axis0.current_state), máscaras de bits de error
 * (axis0.active_errors) y códigos de desarme (axis0.disarm_reason)
 * a descripciones claras en lenguaje humano (español) para el operador.
 */

export interface ODriveErrorDef {
  mask: number;
  name: string;
  label: string;
  advice: string;
  critical: boolean;
}

export const ODRIVE_ERRORS: Record<number, ODriveErrorDef> = {
  0x00000001: {
    mask: 0x00000001,
    name: 'INITIALIZING',
    label: 'Inicializando ODrive',
    advice: 'El controlador ODrive S1 está iniciando periféricos internos.',
    critical: false,
  },
  0x00000002: {
    mask: 0x00000002,
    name: 'SYSTEM_LEVEL',
    label: 'Falla interna del sistema',
    advice: 'Error grave en el microcontrolador de ODrive. Requiere reinicio.',
    critical: true,
  },
  0x00000004: {
    mask: 0x00000004,
    name: 'TIMING_ERROR',
    label: 'Error de sincronización FOC',
    advice: 'Sobrecarga de cómputo en el lazo FOC.',
    critical: true,
  },
  0x00000008: {
    mask: 0x00000008,
    name: 'MISSING_ESTIMATE',
    label: 'Falta estimación de velocidad/posición',
    advice: 'El encoder magnético no reporta posición válida. Revise el cable del encoder.',
    critical: true,
  },
  0x00000010: {
    mask: 0x00000010,
    name: 'BAD_CONFIG',
    label: 'Configuración inválida',
    advice: 'Uno de los parámetros de motor o control está fuera de rango.',
    critical: true,
  },
  0x00000020: {
    mask: 0x00000020,
    name: 'DRV_FAULT',
    label: 'Falla de transistores de potencia (DRV)',
    advice: 'El driver de compuerta DRV detectó cortocircuito o sobrecorriente en fases U/V/W.',
    critical: true,
  },
  0x00000040: {
    mask: 0x00000040,
    name: 'MISSING_INPUT',
    label: 'Falta consigna de entrada',
    advice: 'No se ha provisto consigna válida de velocidad.',
    critical: false,
  },
  0x00000080: {
    mask: 0x00000080,
    name: 'DC_BUS_OVER_VOLTAGE',
    label: 'Sobretensión en bus DC (> Vbus_max)',
    advice: 'Tensión de alimentación por encima del límite de seguridad. Verifique la fuente.',
    critical: true,
  },
  0x00000100: {
    mask: 0x00000100,
    name: 'DC_BUS_UNDER_VOLTAGE',
    label: 'Subtensión en bus DC (< 8V)',
    advice: 'Tensión insuficiente. Fuente de poder de 12V apagada o batería agotada.',
    critical: true,
  },
  0x00000200: {
    mask: 0x00000200,
    name: 'DC_BUS_OVER_CURRENT',
    label: 'Sobrecorriente en bus DC',
    advice: 'El consumo total excedió el límite máximo admitido en la entrada de energía.',
    critical: true,
  },
  0x00000400: {
    mask: 0x00000400,
    name: 'DC_BUS_OVER_REGEN_CURRENT',
    label: 'Sobrecorriente regenerativa',
    advice: 'Corriente excesiva inyectada hacia la fuente durante desaceleración brusca.',
    critical: false,
  },
  0x00000800: {
    mask: 0x00000800,
    name: 'CURRENT_LIMIT_VIOLATION',
    label: 'Violación de límite de corriente motor',
    advice: 'La corriente de fase superó el límite de seguridad (posible sobrecarga o atasco).',
    critical: true,
  },
  0x00001000: {
    mask: 0x00001000,
    name: 'MOTOR_OVER_TEMP',
    label: 'Sobrecalentamiento del motor',
    advice: 'Temperatura de bobinados excesiva. Espere enfriamiento.',
    critical: true,
  },
  0x00002000: {
    mask: 0x00002000,
    name: 'INVERTER_OVER_TEMP',
    label: 'Sobrecalentamiento del inversor ODrive',
    advice: 'MOSFETs del ODrive calientes. Verifique la ventilación del disipador.',
    critical: true,
  },
  0x00004000: {
    mask: 0x00004000,
    name: 'VELOCITY_LIMIT_VIOLATION',
    label: 'Sobrevelocidad (> límite físico)',
    advice: 'La velocidad real superó el límite configurado (65 rev/s).',
    critical: true,
  },
  0x00008000: {
    mask: 0x00008000,
    name: 'POSITION_LIMIT_VIOLATION',
    label: 'Violación de límite de posición',
    advice: 'Desplazamiento fuera del recorrido permitido.',
    critical: false,
  },
  0x00010000: {
    mask: 0x00010000,
    name: 'WATCHDOG_TIMER_EXPIRED',
    label: 'Watchdog UART expiró',
    advice: 'Pérdida de paquetes de comunicación continua entre ESP32 y ODrive S1.',
    critical: true,
  },
  0x00020000: {
    mask: 0x00020000,
    name: 'ESTOP_REQUESTED',
    label: 'Parada de emergencia (E-Stop) activada',
    advice: 'El motor fue detenido de inmediato por botón de emergencia o comando.',
    critical: true,
  },
  0x00040000: {
    mask: 0x00040000,
    name: 'SPINOUT_DETECTED',
    label: 'Atasco mecánico / Spinout (Hélice trabada)',
    advice: 'El rotor no puede seguir el campo magnético. Revise si la hélice está atascada o trabada.',
    critical: true,
  },
  0x00080000: {
    mask: 0x00080000,
    name: 'BRAKE_RESISTOR_DISARMED',
    label: 'Resistencia de frenado desarmada',
    advice: 'Circuito de frenado deshabilitado.',
    critical: false,
  },
  0x00100000: {
    mask: 0x00100000,
    name: 'THERMISTOR_DISCONNECTED',
    label: 'Sensor de temperatura desconectado',
    advice: 'El termistor de motor o placa no responde.',
    critical: false,
  },
  0x00200000: {
    mask: 0x00200000,
    name: 'CALIBRATION_ERROR',
    label: 'Error de calibración',
    advice: 'Falla durante la rutina de calibración de fase o encoder.',
    critical: true,
  },
};

export const ODRIVE_AXIS_STATES: Record<number, { name: string; label: string; active: boolean }> = {
  0: { name: 'UNDEFINED', label: 'Sin Definir', active: false },
  1: { name: 'IDLE', label: 'En Reposo (Desarmado)', active: false },
  2: { name: 'STARTUP', label: 'Iniciando', active: false },
  3: { name: 'FULL_CALIBRATION_SEQUENCE', label: 'Calibración Completa', active: false },
  4: { name: 'MOTOR_CALIBRATION', label: 'Calibrando Motor', active: false },
  6: { name: 'ENCODER_INDEX_SEARCH', label: 'Búsqueda Índice Encoder', active: false },
  7: { name: 'ENCODER_OFFSET_CALIBRATION', label: 'Calibrando Encoder', active: false },
  8: { name: 'CLOSED_LOOP_CONTROL', label: 'Lazo Cerrado (Armado)', active: true },
  9: { name: 'LOCKIN_SPIN', label: 'Giro Síncrono (Lock-in)', active: true },
  10: { name: 'ENCODER_DIR_FIND', label: 'Detección Dirección', active: false },
  11: { name: 'HOMING', label: 'Posicionando a Cero', active: false },
};

export interface DecodedODriveStatus {
  badgeText: string;
  badgeClass: string;
  badgeDotClass: string;
  stateLabel: string;
  hasErrors: boolean;
  isDisarmedByFault: boolean;
  errors: ODriveErrorDef[];
  disarmReason: ODriveErrorDef | null;
  summaryText: string;
}

export function decodeODriveErrors(mask: number): ODriveErrorDef[] {
  if (!mask || mask <= 0) return [];
  const found: ODriveErrorDef[] = [];
  for (const [key, def] of Object.entries(ODRIVE_ERRORS)) {
    const bit = Number(key);
    if ((mask & bit) === bit) {
      found.push(def);
    }
  }
  return found;
}

export function decodeDisarmReason(reasonCode: number): ODriveErrorDef | null {
  if (!reasonCode || reasonCode <= 0) return null;
  return ODRIVE_ERRORS[reasonCode] ?? {
    mask: reasonCode,
    name: 'UNKNOWN_REASON',
    label: `Desarme por error 0x${reasonCode.toString(16).toUpperCase()}`,
    advice: 'Código de error no documentado en esta versión de firmware.',
    critical: true,
  };
}

export function getODriveHumanStatus(params: {
  online: boolean;
  running?: boolean | null;
  state?: number | null;
  statusCode?: number | null;
  disarmReason?: number | null;
  failsafe?: boolean | null;
  vbus?: number | null;
}): DecodedODriveStatus {
  const {
    online,
    running = false,
    state = null,
    statusCode = 0,
    disarmReason = 0,
    failsafe = false,
    vbus = 0,
  } = params;

  // 1. Nodo desconectado
  if (!online) {
    return {
      badgeText: 'DESCONECTADO',
      badgeClass: 'bg-rose-950/70 border-rose-800 text-rose-300',
      badgeDotClass: 'bg-rose-500',
      stateLabel: 'Sin conexión MQTT / WiFi',
      hasErrors: false,
      isDisarmedByFault: false,
      errors: [],
      disarmReason: null,
      summaryText: 'El microcontrolador ESP32 no responde en el broker MQTT.',
    };
  }

  const activeErrors = decodeODriveErrors(statusCode ?? 0);
  const disarm = decodeDisarmReason(disarmReason ?? 0);
  const hasErrors = activeErrors.length > 0;
  const isState8 = state === 8;
  const isState1 = state === 1;

  // 2. Falla crítica activa
  if (hasErrors) {
    const first = activeErrors[0];
    return {
      badgeText: `FALLA: ${first.label.toUpperCase()}`,
      badgeClass: 'bg-rose-950/90 border-rose-600 text-rose-200 animate-pulse',
      badgeDotClass: 'bg-rose-400',
      stateLabel: `Error ODrive (0x${(statusCode ?? 0).toString(16).toUpperCase()})`,
      hasErrors: true,
      isDisarmedByFault: true,
      errors: activeErrors,
      disarmReason: disarm,
      summaryText: `${first.label}. ${first.advice}`,
    };
  }

  // 3. Desarmado por falla previa
  if (isState1 && disarm && disarm.mask !== 0) {
    return {
      badgeText: `DESARMADO: ${disarm.label.toUpperCase()}`,
      badgeClass: 'bg-amber-950/80 border-amber-600 text-amber-200',
      badgeDotClass: 'bg-amber-400',
      stateLabel: `En Reposo tras falla previa (0x${disarm.mask.toString(16).toUpperCase()})`,
      hasErrors: false,
      isDisarmedByFault: true,
      errors: [],
      disarmReason: disarm,
      summaryText: `El motor se detuvo por ${disarm.label}. ${disarm.advice}`,
    };
  }

  // 4. Modo Failsafe
  if (failsafe) {
    return {
      badgeText: 'FAILSAFE DE SEGURIDAD',
      badgeClass: 'bg-amber-950/80 border-amber-600 text-amber-200',
      badgeDotClass: 'bg-amber-400',
      stateLabel: 'Failsafe activo (sin señal del Sensor OD > 35s)',
      hasErrors: false,
      isDisarmedByFault: false,
      errors: [],
      disarmReason: null,
      summaryText: 'Pérdida prolongada del sensor de oxígeno disuelto. Velocidad fijada a modo seguro.',
    };
  }

  // 5. Tensión muy baja en reposo (< 8V)
  if ((vbus ?? 0) > 0 && (vbus ?? 0) < 8.0) {
    return {
      badgeText: 'BUS DC BAJO (< 8V)',
      badgeClass: 'bg-amber-950/80 border-amber-600 text-amber-200',
      badgeDotClass: 'bg-amber-400',
      stateLabel: 'Tensión de bus insuficiente para armar',
      hasErrors: false,
      isDisarmedByFault: false,
      errors: [],
      disarmReason: null,
      summaryText: `Tensión medida ${vbus?.toFixed(1)}V. Conecte o encienda la fuente de alimentación de 12V.`,
    };
  }

  // 6. Armado en Lazo Cerrado
  if (isState8 || running) {
    return {
      badgeText: 'CONTROL CERRADO (ARMADO)',
      badgeClass: 'bg-emerald-950/80 border-emerald-600 text-emerald-200',
      badgeDotClass: 'bg-emerald-400',
      stateLabel: 'Lazo Cerrado Activo (State 8)',
      hasErrors: false,
      isDisarmedByFault: false,
      errors: [],
      disarmReason: null,
      summaryText: 'ODrive S1 controlando motor M8325s activamente en lazo cerrado FOC.',
    };
  }

  // 7. En Reposo (IDLE normal)
  const stateInfo = state != null ? ODRIVE_AXIS_STATES[state] : null;
  const stateName = stateInfo ? stateInfo.label : 'En Reposo (IDLE)';

  return {
    badgeText: 'EN REPOSO (IDLE)',
    badgeClass: 'bg-slate-800/80 border-slate-600 text-slate-300',
    badgeDotClass: 'bg-slate-400',
    stateLabel: stateName,
    hasErrors: false,
    isDisarmedByFault: false,
    errors: [],
    disarmReason: null,
    summaryText: 'Motor ODrive en reposo listo para recibir consigna.',
  };
}

