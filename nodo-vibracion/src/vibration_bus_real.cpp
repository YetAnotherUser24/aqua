/**
 * @file vibration_bus_real.cpp
 * @brief Driver I2C para sensor fisico MPU6050 en ESP32-C3.
 */

#if defined(NODE_REAL_HARDWARE) || !defined(NODE_VIRTUAL)

#include "vibration_bus.h"
#include "config.h"

#include <Arduino.h>
#include <Wire.h>

namespace {

class RealVibrationSensor : public IVibrationSensor {
public:
  RealVibrationSensor() : _statusCode(1), _initialized(false) {}

  bool init() override {
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, I2C_FREQUENCY_HZ);
    delay(50);

    // Verificar presencia del sensor
    Wire.beginTransmission(MPU6050_I2C_ADDR);
    Wire.write(MPU6050_REG_WHO_AM_I);
    if (Wire.endTransmission(false) != 0) {
      _statusCode = 1; // Error I2C
      return false;
    }

    if (Wire.requestFrom((uint8_t)MPU6050_I2C_ADDR, (uint8_t)1) != 1) {
      _statusCode = 1;
      return false;
    }

    uint8_t whoami = Wire.read();
    if (whoami != 0x68 && whoami != 0x70 && whoami != 0x72) {
      // 0x68 es el chip MPU6050 autentico (0x70/0x72 clones o variantes MPU6500)
      Serial.printf("[VIB] WHO_AM_I inesperado: 0x%02X\n", whoami);
    }

    // Despertar sensor: reloj interno con referencia del giroscopio o cristal interno
    writeRegister(MPU6050_REG_PWR_MGMT_1, 0x01);
    delay(10);

    // Poner giroscopio en standby (bits STBY_XG, STBY_YG, STBY_ZG = 1 -> 0x07)
    // Reduce consumo y disipacion termica, solo manteniendo acelerometros activos
    writeRegister(MPU6050_REG_PWR_MGMT_2, 0x07);
    delay(5);

    // Configurar DLPF (filtro digital pasabajo)
    writeRegister(MPU6050_REG_CONFIG, MPU6050_DLPF_CFG_VAL);

    // Configurar rango del acelerometro (+/- 4g)
    writeRegister(MPU6050_REG_ACCEL_CONFIG, ACCEL_RANGE_4G_VAL);

    _initialized = true;
    _statusCode = 0;
    Serial.println("[VIB] Sensor MPU6050 inicializado con exito (+/- 4g)");
    return true;
  }

  bool read(VibrationSample& out) override {
    if (!_initialized) {
      if (!init()) {
        out.valid = false;
        return false;
      }
    }

    // Lectura en rafaga de 8 bytes consecutivos (ACCEL_X/Y/Z + TEMP)
    Wire.beginTransmission(MPU6050_I2C_ADDR);
    Wire.write(MPU6050_REG_ACCEL_XOUT_H);
    if (Wire.endTransmission(false) != 0) {
      _statusCode = 1;
      out.valid = false;
      return false;
    }

    if (Wire.requestFrom((uint8_t)MPU6050_I2C_ADDR, (uint8_t)8) != 8) {
      _statusCode = 2; // Error de recepcion
      out.valid = false;
      return false;
    }

    int16_t rawAx = (int16_t)((Wire.read() << 8) | Wire.read());
    int16_t rawAy = (int16_t)((Wire.read() << 8) | Wire.read());
    int16_t rawAz = (int16_t)((Wire.read() << 8) | Wire.read());
    int16_t rawTemp = (int16_t)((Wire.read() << 8) | Wire.read());

    out.raw_ax = rawAx;
    out.raw_ay = rawAy;
    out.raw_az = rawAz;
    out.ax_g = (float)rawAx / ACCEL_SCALE_4G;
    out.ay_g = (float)rawAy / ACCEL_SCALE_4G;
    out.az_g = (float)rawAz / ACCEL_SCALE_4G;

    // Formula MPU6050: T = (rawTemp / 340.0) + 36.53
    out.temp_c = ((float)rawTemp / 340.0f) + 36.53f;
    out.valid = true;
    _statusCode = 0;
    return true;
  }

  uint8_t statusCode() const override {
    return _statusCode;
  }

private:
  uint8_t _statusCode;
  bool _initialized;

  bool writeRegister(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(MPU6050_I2C_ADDR);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission() == 0;
  }
};

RealVibrationSensor s_realSensor;

} // namespace

IVibrationSensor* getVibrationSensor() {
  return &s_realSensor;
}

#endif
