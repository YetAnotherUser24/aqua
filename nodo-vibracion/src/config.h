#pragma once
/**
 * @file config.h
 * @brief Configuracion estatica del nodo de vibracion (ESP32-C3 + MPU6050).
 */

#include <stdint.h>
#include <stddef.h>
#include <WiFi.h>

// -----------------------------------------------------------------------------
// RED Y MQTT
// -----------------------------------------------------------------------------
#ifndef AQUA_WIFI_SSID
#define AQUA_WIFI_SSID          "AquaStation"
#endif
#ifndef AQUA_WIFI_PASSWORD
#define AQUA_WIFI_PASSWORD      "AquaPass"
#endif
#define WIFI_SSID               AQUA_WIFI_SSID
#define WIFI_PASSWORD           AQUA_WIFI_PASSWORD
#define WIFI_HOSTNAME           "nodo-vibracion"

// Red estatica AquaControl (192.168.4.14 para nodo-vibracion)
#ifndef AQUA_STATIC_IP
#define AQUA_STATIC_IP          192, 168, 4, 14
#endif
#ifndef AQUA_STATIC_GATEWAY
#define AQUA_STATIC_GATEWAY     0, 0, 0, 0
#endif
#ifndef AQUA_STATIC_SUBNET
#define AQUA_STATIC_SUBNET      255, 255, 255, 0
#endif

// Potencia RF de transmision WiFi (Crucial para estabilidad del ESP32-C3)
// 8.5 dBm previene caidas de tension en el riel de 3.3V y brownouts
#define WIFI_TX_POWER       WIFI_POWER_8_5dBm

#define MQTT_BROKER_HOST    "192.168.4.1"
#define MQTT_BROKER_PORT    1883
#define MQTT_CLIENT_ID      "nodo-vibracion"

// Credenciales MQTT autenticadas para Mosquitto en aqua-pi
#ifndef AQUA_MQTT_USER
#define AQUA_MQTT_USER      "vibration"
#endif
#ifndef AQUA_MQTT_PASSWORD
#define AQUA_MQTT_PASSWORD  "vibPass7xK9mQ2wL4pRt1Za8"
#endif
#define MQTT_USER           AQUA_MQTT_USER
#define MQTT_PASSWORD       AQUA_MQTT_PASSWORD

// Schema-v2 Transport
#define AQUA_TRANSPORT_MQTT 1

// -----------------------------------------------------------------------------
// CALIBRACION / TARA EN REPOSO (OPCIONAL)
// -----------------------------------------------------------------------------
// Activa la tara en setup() calculando el offset para: X -> 0g, Y -> 1.0g, Z -> 0g
#define ENABLE_AUTO_CALIBRATION   true
#define CALIBRATION_SAMPLES       100

// -----------------------------------------------------------------------------
// HARDWARE I2C & MPU6050
// -----------------------------------------------------------------------------
#define PIN_I2C_SDA         1
#define PIN_I2C_SCL         0
#define I2C_FREQUENCY_HZ    400000UL

// Direccion I2C: 0x68 (AD0 a GND) o 0x69 (AD0 a VCC)
#define MPU6050_I2C_ADDR    0x68

// Registros MPU6050
#define MPU6050_REG_SMPLRT_DIV    0x19
#define MPU6050_REG_CONFIG        0x1A
#define MPU6050_REG_GYRO_CONFIG   0x1B
#define MPU6050_REG_ACCEL_CONFIG  0x1C
#define MPU6050_REG_ACCEL_XOUT_H  0x3B
#define MPU6050_REG_TEMP_OUT_H    0x41
#define MPU6050_REG_PWR_MGMT_1    0x6B
#define MPU6050_REG_PWR_MGMT_2    0x6C
#define MPU6050_REG_WHO_AM_I      0x75

// Rango del acelerometro: +/- 4g (FS_SEL = 1 -> AFS_SEL = 1)
// Sensibilidad = 8192 LSB/g
#define ACCEL_RANGE_4G_VAL        0x08
#define ACCEL_SCALE_4G            8192.0f

// DLPF (Filtro pasabajo digital interno):
// 2 = 94 Hz pasabajo (retraso 3.0 ms)
// 3 = 44 Hz pasabajo (retraso 4.9 ms)
#define MPU6050_DLPF_CFG_VAL      0x02

// -----------------------------------------------------------------------------
// CADENCIA DE MUESTREO Y TRANSMISION
// -----------------------------------------------------------------------------
// Frecuencia de muestreo del sensor: 200 Hz (5 ms)
constexpr uint32_t SENSOR_SAMPLE_INTERVAL_MS = 5;

// Ventana de calculo y publicacion telemetrica: 1 Hz (1000 ms)
constexpr uint32_t TELEMETRY_INTERVAL_MS = 1000;

// Intervalo en reposo / idle: 10 segundos (0.1 Hz)
constexpr uint32_t IDLE_TELEMETRY_INTERVAL_MS = 10000;

// Limite maximo de muestras acumuladas por segundo
constexpr size_t MAX_WINDOW_SAMPLES = 250;
