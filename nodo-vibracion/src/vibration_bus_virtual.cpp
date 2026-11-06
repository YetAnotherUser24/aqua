/**
 * @file vibration_bus_virtual.cpp
 * @brief Simulador de senales de vibracion para pruebas de software (SIL).
 */

#if defined(NODE_VIRTUAL)

#include "vibration_bus.h"
#include <Arduino.h>
#include <math.h>

namespace {

class VirtualVibrationSensor : public IVibrationSensor {
public:
  VirtualVibrationSensor() : _timeSec(0.0f), _temp(24.5f) {}

  bool init() override {
    Serial.println("[VIB] Bus virtual MPU6050 inicializado");
    return true;
  }

  void tick(uint32_t nowMs) override {
    _timeSec = (float)nowMs / 1000.0f;
  }

  bool read(VibrationSample& out) override {
    // Generacion de vibracion sintetica:
    // Componente estatica: gravedad 1.0g en Z (tanque apoyado)
    // Componente dinamica: armonico a 25 Hz (banco/motor) + ruido blanco
    float t = _timeSec;
    float noiseX = ((float)(rand() % 100) - 50.0f) / 1000.0f; // +/- 0.05g ruido
    float noiseY = ((float)(rand() % 100) - 50.0f) / 1000.0f;
    float noiseZ = ((float)(rand() % 100) - 50.0f) / 1000.0f;

    // Vibracion estructural tipica: 0.1g - 0.3g
    out.ax_g = 0.15f * sinf(2.0f * (float)M_PI * 25.0f * t) + noiseX;
    out.ay_g = 0.18f * cosf(2.0f * (float)M_PI * 25.0f * t) + noiseY;
    out.az_g = 1.0f + 0.22f * sinf(2.0f * (float)M_PI * 50.0f * t) + noiseZ;

    // Temperatura de 24.5 C con suave deriva
    out.temp_c = _temp + 0.5f * sinf(0.05f * t);
    out.valid = true;
    return true;
  }

  uint8_t statusCode() const override {
    return 0; // Siempre OK en virtual
  }

private:
  float _timeSec;
  float _temp;
};

VirtualVibrationSensor s_virtualSensor;

} // namespace

IVibrationSensor* getVibrationSensor() {
  return &s_virtualSensor;
}

#endif
