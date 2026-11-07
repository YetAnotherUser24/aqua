#include "dht_sensor.h"
#include "config.h"
#include "board.h"
#include <math.h>

static bool s_present = false;
static float s_lastTempC = NAN;
static float s_lastHumidityPct = NAN;

#if (!defined(AERADOR_VIRTUAL) || !AERADOR_VIRTUAL) && (!defined(ODRIVE_VIRTUAL) || !ODRIVE_VIRTUAL) && defined(DHT_ENABLED) && DHT_ENABLED
#include <DHT.h>
static DHT s_dht(DHT_PIN, DHT_TYPE);
static bool s_driverStarted = false;
#endif

void dhtSensorInit() {
#if (!defined(AERADOR_VIRTUAL) || !AERADOR_VIRTUAL) && (!defined(ODRIVE_VIRTUAL) || !ODRIVE_VIRTUAL) && defined(DHT_ENABLED) && DHT_ENABLED
  s_dht.begin();
  s_driverStarted = true;
  Serial.printf("[DHT] Driver initialized on GPIO %d (Type: DHT%d, Optional)\n", DHT_PIN, DHT_TYPE);
#elif (defined(AERADOR_VIRTUAL) && AERADOR_VIRTUAL) || (defined(ODRIVE_VIRTUAL) && ODRIVE_VIRTUAL)
  s_present = true;
  Serial.println("[DHT] Virtual mode active: Simulated ambient environment enabled.");
#else
  Serial.println("[DHT] Sensor support disabled by build configuration.");
#endif
}

bool dhtSensorRead(float& outTempC, float& outHumidityPct) {
#if (defined(AERADOR_VIRTUAL) && AERADOR_VIRTUAL) || (defined(ODRIVE_VIRTUAL) && ODRIVE_VIRTUAL)
  // Virtual simulation: plausible ambient room conditions
  const unsigned long now = millis();
  outTempC = 24.0f + 0.4f * sinf((float)now / 60000.0f);
  outHumidityPct = 50.0f + 1.5f * cosf((float)now / 90000.0f);
  s_lastTempC = outTempC;
  s_lastHumidityPct = outHumidityPct;
  s_present = true;
  return true;

#elif defined(DHT_ENABLED) && DHT_ENABLED
  if (!s_driverStarted) {
    outTempC = NAN;
    outHumidityPct = NAN;
    s_present = false;
    return false;
  }

  // Non-blocking for Core 1 (called strictly from Core 0 background task)
  float h = s_dht.readHumidity();
  float t = s_dht.readTemperature();

  if (isnan(h) || isnan(t)) {
    // Sensor not connected or transient single-wire timeout
    s_present = false;
    outTempC = NAN;
    outHumidityPct = NAN;
    return false;
  }

  s_present = true;
  s_lastTempC = t;
  s_lastHumidityPct = h;
  outTempC = t;
  outHumidityPct = h;
  return true;

#else
  outTempC = NAN;
  outHumidityPct = NAN;
  s_present = false;
  return false;
#endif
}

bool dhtSensorIsPresent() {
  return s_present;
}

