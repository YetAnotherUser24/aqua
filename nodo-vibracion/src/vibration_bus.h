#pragma once
/**
 * @file vibration_bus.h
 * @brief Interfaz de frontera con el sensor de vibracion (hardware real vs bus simulado).
 */

#include <stdint.h>

struct VibrationSample {
  float ax_g = 0.0f;
  float ay_g = 0.0f;
  float az_g = 0.0f;
  int16_t raw_ax = 0;
  int16_t raw_ay = 0;
  int16_t raw_az = 0;
  float temp_c = 0.0f;
  bool valid = false;
};

class IVibrationSensor {
public:
  virtual ~IVibrationSensor() = default;

  /**
   * @brief Inicializa el bus y el sensor MPU6050.
   * @return true si el sensor respondio correctamente en el bus.
   */
  virtual bool init() = 0;

  /**
   * @brief Lee una muestra de aceleracion y temperatura.
   * @param out Estructura donde se almacena la lectura.
   * @return true si la lectura fue exitosa y valida.
   */
  virtual bool read(VibrationSample& out) = 0;

  /**
   * @brief Avanza el modelo de simulacion (solo activo en bus virtual).
   */
  virtual void tick(uint32_t nowMs) {}

  /**
   * @brief Calibra offsets en reposo ajustando a los valores objetivo deseados.
   */
  virtual void calibrate(float targetX = 0.0f, float targetY = 1.0f, float targetZ = 0.0f) {}

  /**
   * @brief Devuelve el codigo de estado del sensor (0 = OK).
   */
  virtual uint8_t statusCode() const = 0;
};
