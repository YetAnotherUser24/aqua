/**
 * @file modbus_probe.cpp
 * @brief Sonda automática del bus RS-485 del sensor de OD (herramienta de banco).
 *
 * Se compila solo en `[env:od-logger-probe]` (flag MODBUS_PROBE_TOOL); el resto de entornos
 * compilan un archivo vacío, siguiendo el mismo patrón que `i2c_scanner.cpp`.
 *
 * Uso: con el nodo estable, conecta el sensor y ejecuta
 *     pio run -e od-logger-probe -t upload
 * La sonda barre el pin DE/RE del transceptor, su polaridad y la velocidad Modbus, y reporta
 * la combinación que obtiene respuesta. Sirve para validar el cableado del HAT SP3485EN.
 */

#include <Arduino.h>

#ifdef MODBUS_PROBE_TOOL

#include <HardwareSerial.h>
#include <stdarg.h>
#include "log.h"
#include "modbus_rtu.h"

/* El firmware principal implementa estas funciones en log.cpp; la sonda las aporta aquí. */
void Log(const char* s) { Serial.print(s); }
void Logln(const char* s) { Serial.println(s); }
void Logf(const char* fmt, ...) {
  char buf[192];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  Serial.print(buf);
}

static HardwareSerial odSerial(2);

static const int kDePins[] = {0, 25, 2, 19, 21, 22, 13, 14, 27, 26, -1};
static const uint32_t kDePinCount = sizeof(kDePins) / sizeof(kDePins[0]);

/** Intenta leer 2 registros del esclavo; devuelve true si hubo respuesta válida o excepción. */
static bool probeOnce(int dePin, bool txActiveHigh, uint8_t slave, uint32_t baud) {
  odSerial.end();
  delay(30);

  modbus_rtu::ModbusRtu bus(odSerial);
  modbus_rtu::ModbusRtu::Config cfg;
  cfg.directionPin = (int8_t)dePin;
  cfg.manualDirection = (dePin >= 0);
  cfg.txActiveHigh = txActiveHigh;
  cfg.responseTimeoutMs = 400;
  cfg.txEnableGuardUs = 250;
  cfg.txDisableGuardUs = 250;
  cfg.interFrameGapMs = 4;
  cfg.rxTurnaroundUs = 200;
  bus.configure(cfg);
  bus.begin(baud, 39, 4);

  auto r = bus.readRegisters(slave, 0x03, 0x0000, 2);
  const bool responded = (r.success && r.count > 0) || (r.count > 0);
  Serial.printf("[PROBE] de=%-3d pol=%s slave=0x%02X baud=%lu -> success=%d count=%d %s\n", dePin,
                txActiveHigh ? "HI" : "LO", slave, (unsigned long)baud, (int)r.success, (int)r.count,
                responded ? "<<< RESPONDE" : "");
  Serial.flush();
  odSerial.end();
  return responded;
}

void setup() {
  Serial.begin(115200);
  delay(1200);

  Serial.println();
  Serial.println("=========================================");
  Serial.println(" RS-485 MODBUS PROBE (sensor OD)         ");
  Serial.println("=========================================");
  Serial.flush();

  const uint8_t kSlave = 0x0A;
  int foundPin = -1;
  bool foundPolarity = true;

  Serial.println("[PROBE] Pasada 1: pines DE/RE con polaridad activa-alta");
  for (uint32_t i = 0; i < kDePinCount; i++) {
    if (probeOnce(kDePins[i], true, kSlave, 9600)) {
      foundPin = kDePins[i];
      break;
    }
  }

  if (foundPin < 0) {
    Serial.println("[PROBE] Pasada 2: pines DE/RE con polaridad activa-baja");
    for (uint32_t i = 0; i < kDePinCount; i++) {
      if (probeOnce(kDePins[i], false, kSlave, 9600)) {
        foundPin = kDePins[i];
        foundPolarity = false;
        break;
      }
    }
  }

  if (foundPin < 0) {
    Serial.println("[PROBE] Sin respuesta en ningun pin/posibilidad. Barrido de velocidades (de=0/25):");
    const uint32_t bauds[] = {19200, 38400, 4800, 1200};
    for (uint32_t b = 0; b < 4; b++) {
      probeOnce(0, true, kSlave, bauds[b]);
      probeOnce(25, true, kSlave, bauds[b]);
    }
    Serial.println("[PROBE] FIN: el sensor no responde (revisar conexion/alimentacion del OPTOD).");
  } else {
    Serial.printf("[PROBE] Respuesta con de=%d pol=%s. Barriendo direcciones Modbus 1..15:\n", foundPin,
                  foundPolarity ? "HI" : "LO");
    for (uint8_t addr = 1; addr <= 15; addr++) {
      probeOnce(foundPin, foundPolarity, addr, 9600);
    }
    Serial.printf("[PROBE] FIN: usar OD_DE_RE_PIN=%d (pol=%s) si la direccion 0x%02X responde.\n", foundPin,
                  foundPolarity ? "HI" : "LO", kSlave);
  }
  Serial.flush();
}

void loop() {
  delay(5000);
}

#endif // MODBUS_PROBE_TOOL
