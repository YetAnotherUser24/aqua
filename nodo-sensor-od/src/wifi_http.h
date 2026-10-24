/**
 * @file wifi_http.h
 * @brief WiFi connectivity and HTTPS telemetry upload to AquaControl dashboard.
 *
 * Guarded by #ifdef WIFI_MODULE — disabled when WiFi feature is toggled off.
 */

#pragma once
#include <Arduino.h>

#ifdef WIFI_MODULE

/**
 * @brief Handle a command received over MQTT.
 *
 * Defined in `wifi_http.cpp` because it reuses the existing command dispatcher rather than
 * reimplementing it. It reshapes the MQTT command into the legacy response envelope and calls
 * `processTelemetryResponse`. Keeping one dispatcher means the MQTT path and the HTTPS path
 * cannot disagree about what an action does.
 *
 * @param payload raw MQTT payload, NOT necessarily NUL-terminated
 * @param length  payload length in bytes
 */
void wifiHttpHandleMqttCommand(const char* payload, size_t length);

/**
 * @brief Connects to WiFi using credentials from config.h.
 * @param block If true, block until connected or timeout. If false, connect asynchronously.
 * @return true if connected, false on timeout or async.
 */
bool wifiConnect(bool block = true);

/**
 * @brief Non-blocking WiFi maintainer. Checks connection and handles 30s timeout fallback.
 */
void wifiMaintain();

/**
 * @brief Disconnects WiFi cleanly before deep sleep.
 */
void wifiDisconnect();

/**
 * @brief Posts a single sensor reading to AquaControl via HTTPS.
 *
 * Builds the flat JSON the /api/telemetry/sensor endpoint expects
 * from the raw sensorReadings array.
 *
 * @param seconds2000  Timestamp as seconds since 2000-01-01.
 * @param readings     Array of MAX_SENSOR_READINGS int16_t values.
 * @param rtcTempCenti RTC temperature in centi-°C.
 * @param status       32-bit status bitmask.
 * @return true if HTTP 200 received, false otherwise.
 */
bool publishToAquaControl(uint32_t seconds2000, int16_t* readings,
                          int16_t rtcTempCenti, uint32_t status);

/**
 * @brief Encola una muestra en el buffer RAM (V4 §2.2) sin bloquear por red.
 */
void queueTelemetrySample(uint32_t seconds2000, int16_t* readings, int16_t rtcTempCenti, uint32_t status);

/**
 * @brief Envía todo el buffer RAM como un único paquete bulk. Preserva el buffer si falla.
 * @return true si el POST se confirmó (buffer vacío).
 */
bool flushTelemetryBulk();

/**
 * @brief Número de muestras actualmente retenidas en el buffer RAM.
 */
uint8_t telemetryQueueCount();

/**
 * @brief Synchronizes ESP32 internal clock via WiFi NTP, updating rtcNow and DS3231 if present.
 * @param timeoutMs Maximum milliseconds to wait for NTP sync (default 5000).
 * @return true if time is synchronized, false otherwise.
 */
bool syncTimeNtp(uint32_t timeoutMs = 5000);

/**
 * @brief Callback signature for commands received in telemetry responses.
 * @param cmdId Unique command identifier.
 * @param action Action string (e.g. "start_experiment", "stop_monitor", etc.)
 * @param payloadJson Raw JSON string containing action parameters.
 */
typedef void (*SensorCommandCallback)(const char* cmdId, const char* action, const char* payloadJson);

/**
 * @brief Registers callback function to execute received commands.
 */
void setSensorCommandCallback(SensorCommandCallback cb);

/**
 * @brief Sends command acknowledgement back to AquaControl dashboard.
 * @param cmdId UUID of the executed control command.
 * @param success True if executed successfully, false otherwise.
 * @param sensorStateJson Optional JSON object string representing updated sensor state.
 * @return True if acknowledged with HTTP 200.
 */
bool acknowledgeSensorCommand(const char* cmdId, bool success, const char* sensorStateJson = nullptr);

/**
 * @brief Checks for pending commands on AquaControl dashboard while logger is in idle state.
 * @return True if a command was received, false otherwise.
 */
bool pollPendingCommands();

#else

static inline bool wifiConnect(bool block = true) { return false; }
static inline void wifiMaintain() {}
static inline void wifiDisconnect() {}
static inline bool syncTimeNtp(uint32_t = 0) { return false; }
static inline bool publishToAquaControl(uint32_t, int16_t*, int16_t, uint32_t) {
  return false;
}
static inline void queueTelemetrySample(uint32_t, int16_t*, int16_t, uint32_t) {}
static inline bool flushTelemetryBulk() { return true; }
static inline uint8_t telemetryQueueCount() { return 0; }
typedef void (*SensorCommandCallback)(const char*, const char*, const char*);
static inline void setSensorCommandCallback(SensorCommandCallback) {}
static inline bool acknowledgeSensorCommand(const char*, bool, const char* = nullptr) { return false; }
static inline bool pollPendingCommands() { return false; }

#endif // WIFI_MODULE
