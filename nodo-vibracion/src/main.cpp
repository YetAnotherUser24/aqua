/**
 * @file main.cpp
 * @brief Firmware principal del nodo de vibracion (ESP32-C3 + MPU6050).
 */

#include <Arduino.h>
#include <WiFi.h>

#include "config.h"
#include "vibration_bus.h"
#include "vibration_processor.h"
#include "aqua_link.h"

extern IVibrationSensor* getVibrationSensor();

namespace {

IVibrationSensor* s_sensor = nullptr;
VibrationProcessor s_processor;

uint32_t s_lastSampleMs = 0;
uint32_t s_lastFlushMs = 0;
uint32_t s_lastWifiCheckMs = 0;

void setupWifi() {
  Serial.printf("[WIFI] Conectando a red: %s (IP fija 192.168.4.14)...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.setTxPower(WIFI_TX_POWER); // 8.5 dBm para evitar brownouts y caidas de tension en ESP32-C3

  IPAddress local_IP(AQUA_STATIC_IP);
  IPAddress gateway(AQUA_STATIC_GATEWAY);
  IPAddress subnet(AQUA_STATIC_SUBNET);
  WiFi.config(local_IP, gateway, subnet);

  WiFi.setHostname(WIFI_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void checkWifi(uint32_t nowMs) {
  if (WiFi.status() == WL_CONNECTED) {
    static bool logged = false;
    if (!logged) {
      Serial.printf("[WIFI] Conectado con exito. IP: %s | RSSI: %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
      logged = true;
    }
    s_lastWifiCheckMs = nowMs;
    return;
  }

  // Si no esta conectado, no spamear reconnect() cada 5s (interrumpe la negociacion)
  // Reintentar solo si pasa un timeout razonable de 15s
  if (nowMs - s_lastWifiCheckMs > 15000) {
    s_lastWifiCheckMs = nowMs;
    Serial.println("[WIFI] Reintentando asociacion WiFi...");
    WiFi.disconnect(false, false);
    WiFi.reconnect();
  }
}

} // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("==================================================");
  Serial.println("  AquaControl — Nodo de Vibracion (ESP32-C3)      ");
  Serial.println("  Sensor: MPU6050 (+/- 4g) en I2C (SDA=1, SCL=0)   ");
  Serial.println("  Protocolo: Schema-v2 sobre MQTT                 ");
  Serial.println("==================================================");

  setupWifi();
  aquaLinkBegin();

  s_sensor = getVibrationSensor();
  if (!s_sensor->init()) {
    Serial.println("[ERR] No se pudo inicializar el sensor MPU6050.");
  }
}

void loop() {
  const uint32_t nowMs = millis();

  checkWifi(nowMs);
  aquaLinkLoop();

  // 1. Muestreo de alta frecuencia (200 Hz -> cada 5 ms)
  if (nowMs - s_lastSampleMs >= SENSOR_SAMPLE_INTERVAL_MS) {
    s_lastSampleMs = nowMs;
    s_sensor->tick(nowMs);

    VibrationSample sample;
    if (s_sensor->read(sample)) {
      s_processor.addSample(sample);
    }
  }

  // 2. Cierre de ventana y emision telemetrica (1 Hz -> cada 1000 ms)
  if (nowMs - s_lastFlushMs >= TELEMETRY_INTERVAL_MS) {
    s_lastFlushMs = nowMs;

    VibrationTelemetry telem;
    if (s_processor.finalizeWindow(telem)) {
      telem.dev_status = s_sensor->statusCode();
      const uint64_t unixMs = aquaLinkNowMs();

      Serial.printf("[VIB] RMS: %.3fg | Peak: %.3fg | Cf: %.2f | Temp: %.1fC | Muestras: %u\n",
                    telem.rms_g, telem.peak_g, telem.crest_factor, telem.temp_c,
                    (unsigned int)telem.sample_count);

      aquaLinkQueue(telem, unixMs);
      aquaLinkFlush();
    }
  }

  delay(1);
}
