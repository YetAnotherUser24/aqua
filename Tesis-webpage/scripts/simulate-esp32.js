/**
 * Simulador de Telemetría ESP32 para AquaControl
 * Ejecución: node scripts/simulate-esp32.js
 */

const BASE_URL = process.env.APP_URL || 'http://localhost:3000';
const SENSOR_KEY = process.env.ESP32_SENSOR_DEVICE_KEY || 'ESP32_SENSOR_KEY_2026';
const MOTOR_KEY = process.env.ESP32_MOTOR_DEVICE_KEY || 'ESP32_MOTOR_KEY_2026';

let motorState = {
  is_on: true,
  speed_percent: 65.0,
  pwm_us: 1760,
  voltage_v: 14.8,
  current_a: 8.5
};

async function sendSensorReading() {
  // Simulación de lectura con valores en la escala de microcontrolador
  // Oxígeno disuelto ~7.8 mg/L -> 7800..8100 milli_mg_l
  const baseDO = 7.8 + (Math.random() * 0.4 - 0.2);
  const doMilli = Math.round(baseDO * 1000);
  const tempCenti = Math.round((23.0 + Math.random() * 0.5) * 100);
  const satDeci = Math.round((baseDO * 12.5) * 10);
  const batteryMv = Math.round((4.2 - Math.random() * 0.1) * 1000);

  const payload = {
    datetime: new Date().toISOString(),
    seconds_since_2000: Math.floor((Date.now() - new Date('2000-01-01').getTime()) / 1000),
    water_temp_centi: tempCenti,
    do_milli_mg_l: doMilli,
    do_sat_deci_pct: satDeci,
    param3_centi: 1024,
    param4_centi: 2048,
    battery_mv: batteryMv,
    rtc_temp_centi: 2410,
    status: 0,
    sent: true
  };

  try {
    const res = await fetch(`${BASE_URL}/api/telemetry/sensor`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Device-Key': SENSOR_KEY
      },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    console.log(`[ESP32 SENSOR] Telemetría enviada: OD=${(doCenti/1000).toFixed(3)} mg/L, Temp=${(tempCenti/100).toFixed(2)}°C -> Res:`, data.success);
  } catch (err) {
    console.error('[ESP32 SENSOR] Error al enviar:', err.message);
  }
}

async function sendMotorTelemetryAndPollCommands() {
  try {
    // 1. Enviar telemetría actual
    const telemetryPayload = {
      datetime: new Date().toISOString(),
      is_on: motorState.is_on,
      speed_percent: motorState.speed_percent,
      pwm_us: motorState.pwm_us,
      voltage_v: motorState.voltage_v,
      current_a: motorState.current_a,
      power_w: Number((motorState.voltage_v * motorState.current_a).toFixed(2)),
      status_code: 0
    };

    await fetch(`${BASE_URL}/api/telemetry/motor`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Device-Key': MOTOR_KEY
      },
      body: JSON.stringify(telemetryPayload)
    });

    // 2. Consultar si la dashboard envió una orden pendiente
    const cmdRes = await fetch(`${BASE_URL}/api/commands/pending`, {
      headers: { 'X-Device-Key': MOTOR_KEY }
    });
    const cmdData = await cmdRes.json();

    if (cmdData.has_command && cmdData.command) {
      const cmd = cmdData.command;
      console.log(`[ESP32 MOTOR] *** COMANDO RECIBIDO *** -> ID: ${cmd.id}, Tipo: ${cmd.command_type}, Vel: ${cmd.speed_percent}%`);

      // Aplicar comando al estado simulado
      if (cmd.command_type === 'stop') {
        motorState.is_on = false;
        motorState.speed_percent = 0;
        motorState.pwm_us = 1500;
        motorState.current_a = 0.1;
      } else {
        motorState.is_on = true;
        motorState.speed_percent = cmd.speed_percent;
        motorState.pwm_us = cmd.pwm_us;
        motorState.current_a = Number(((cmd.speed_percent / 100) * 12.0).toFixed(2));
      }

      // 3. Confirmar ejecución (Acknowledge)
      await fetch(`${BASE_URL}/api/commands/${cmd.id}/acknowledge`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Device-Key': MOTOR_KEY
        },
        body: JSON.stringify({
          success: true,
          actual_speed_percent: motorState.speed_percent
        })
      });
      console.log(`[ESP32 MOTOR] Orden ${cmd.id} ejecutada y confirmada.`);
    }
  } catch (err) {
    console.error('[ESP32 MOTOR] Error en ciclo de motor:', err.message);
  }
}

console.log('--- INICIANDO SIMULADOR ESP32 AQUACONTROL ---');
console.log(`Servidor destino: ${BASE_URL}`);

// Enviar periódicamente
setInterval(sendSensorReading, 10000);
setInterval(sendMotorTelemetryAndPollCommands, 5000);

// Primer disparo inmediato
sendSensorReading();
sendMotorTelemetryAndPollCommands();
