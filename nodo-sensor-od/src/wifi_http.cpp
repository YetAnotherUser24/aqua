/**
 * @file wifi_http.cpp
 * @brief WiFi connectivity and HTTPS POST to AquaControl dashboard.
 *
 * Uses ESP32-native WiFi + HTTPClient (built into Arduino framework,
 * zero external dependencies).
 *
 * Guarded by #ifdef WIFI_MODULE.
 */

#ifdef WIFI_MODULE

#include "wifi_http.h"
#include "aqua_link.h"
#include "config.h"
#include "log.h"
#include "n_logger_config.h"
#include "rtc_utils.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <RtcDS3231.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_sntp.h>
#include <esp_task_wdt.h>
#include <time.h>
#include <ArduinoOTA.h>

extern RtcDS3231<TwoWire> rtc;
extern RtcDateTime rtcNow;
extern char dateStr[20];

/* ----------------------------- WiFi Connect ----------------------------- */

static unsigned long s_lastWifiRetry = 0;
static bool s_wasConnected = false;

bool wifiConnect(bool block) {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  Logf("WiFi connecting to \"%s\"...", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setTxPower(WIFI_POWER_8_5dBm); // Crucial para la estabilidad
  IPAddress local_IP(AQUA_STATIC_IP);
  IPAddress gateway(AQUA_STATIC_GATEWAY);
  IPAddress subnet(AQUA_STATIC_SUBNET);
  WiFi.config(local_IP, gateway, subnet);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  s_lastWifiRetry = millis();

  if (!block) {
    Logln(" (Async mode)");
    return false;
  }

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > WIFI_CONNECT_TIMEOUT_MS) {
      Logln(" timeout.");
      WiFi.disconnect(true);
      WiFi.mode(WIFI_OFF);
      return false;
    }
    delay(250);
    Log(".");
  }

  Logf(" connected (IP: %s)\n", WiFi.localIP().toString().c_str());
  s_wasConnected = true;
  syncTimeNtp(3000);

  return true;
}

void wifiMaintain() {
  if (WiFi.status() != WL_CONNECTED) {
    if (s_wasConnected) {
      Logln("[Red] Desconectado. Auto-reconnect activo...");
      // digitalWrite(LED_BUILTIN, HIGH);
      s_wasConnected = false;
      s_lastWifiRetry = millis();
    }
    
    // SEGURO DE VIDA: Si el AutoReconnect interno falla por 30 segundos
    if (millis() - s_lastWifiRetry > 30000) {
      Logln("[Red] Pila WiFi atascada. Reiniciando hardware de red...");
      WiFi.disconnect(true);
      IPAddress local_IP(AQUA_STATIC_IP);
      IPAddress gateway(AQUA_STATIC_GATEWAY);
      IPAddress subnet(AQUA_STATIC_SUBNET);
      WiFi.config(local_IP, gateway, subnet);
      vTaskDelay(pdMS_TO_TICKS(100));
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      s_lastWifiRetry = millis();
    }
  } else {
    if (!s_wasConnected) {
      Logf("[Red] Conectado. IP: %s\n", WiFi.localIP().toString().c_str());
      // digitalWrite(LED_BUILTIN, LOW);
      s_wasConnected = true;
      syncTimeNtp(3000);
    }
  }
}

/* ----------------------------- NTP Time Sync ----------------------------- */

bool syncTimeNtp(uint32_t timeoutMs) {
  if (WiFi.status() != WL_CONNECTED) {
    if (!wifiConnect()) {
      Logln("[NTP] Cannot sync time: WiFi connection failed.");
      return false;
    }
  }

  Logln("[NTP] Synchronizing time via NTP...");
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");

  unsigned long t0 = millis();
  while (sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED && (millis() - t0 < timeoutMs)) {
    delay(100);
    esp_task_wdt_reset();
  }

  time_t utcNow = time(nullptr);
  if (sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED || utcNow >= 1577836800ULL) {
    // Convert authoritative UTC time to local timezone (UTC-5)
    time_t localNow = utcNow - LOCAL_TZ_OFFSET_SEC;
    uint32_t sec2000 = (uint32_t)(localNow - UNIX_OFFSET_2000);
    RtcDateTime ntpDt(sec2000);
    setRtcDateTime(ntpDt);
    formatDateTime(rtcNow);
    Logf("[NTP] Time synchronized successfully: %s %s\n", dateStr, LOCAL_TZ_NAME);

    if (isRtcAvailable()) {
      Logln("[NTP] Hardware DS3231 RTC updated with NTP local time.");
    }
    return true;
  }

  Logln("[NTP] Time sync timed out.");
  return false;
}

#include <lwip/sockets.h>

/* ----------------------------- Persistent HTTP Client ----------------------------- */

static WiFiClientSecure s_client;
static HTTPClient s_http;
static bool s_clientConfigured = false;

static void closeHttpConnection() {
  s_http.end();
  int fd = s_client.fd();
  if (fd >= 0) {
    struct linger sl = { 1, 0 }; // Linger on with timeout 0 -> sends RST, avoids 60s TIME_WAIT
    setsockopt(fd, SOL_SOCKET, SO_LINGER, &sl, sizeof(sl));
  }
  s_client.stop();
}

/* ----------------------------- WiFi Disconnect ----------------------------- */

void wifiDisconnect() {
  closeHttpConnection();

#if !defined(DISABLE_ESPNOW)
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_STA); // ponytail: preserve STA mode so ESP-NOW direct link does not terminate
  Logln("WiFi disconnected from AP (Radio retained in STA for ESP-NOW).");
#else
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Logln("WiFi disconnected from AP (Radio powered OFF, ESP-NOW disabled).");
#endif
}

/* ----------------------------- Command Processing & Ack ----------------------------- */

static SensorCommandCallback s_commandCallback = nullptr;

void setSensorCommandCallback(SensorCommandCallback cb) {
  s_commandCallback = cb;
}

static void processTelemetryResponse(const String& response) {
  if (response.indexOf("has_command") == -1 && response.indexOf("command") == -1) {
    return; // Fast-path: neither has_command nor command present
  }

  StaticJsonDocument<1536> doc;
  DeserializationError err = deserializeJson(doc, response);
  if (err) {
    Logf("[CMD] Response JSON parse error: %s\n", err.c_str());
    return;
  }

  bool hasCommand = doc["has_command"].as<bool>() || 
                    !doc["command"].isNull() || 
                    !doc["pending_command"].isNull();

  if (hasCommand) {
    JsonObject pending = !doc["pending_command"].isNull() 
                           ? doc["pending_command"].as<JsonObject>() 
                           : doc["command"].as<JsonObject>();
    if (pending.isNull()) {
      return;
    }

    const char* cmdId = pending["id"] | pending["command_id"] | "";
    const char* cmdType = pending["command_type"] | "";
    
    JsonObject payload;
    StaticJsonDocument<384> fallbackPayloadDoc;
    if (pending["payload"].is<JsonObject>()) {
      payload = pending["payload"].as<JsonObject>();
    } else if (pending["payload"].is<const char*>()) {
      deserializeJson(fallbackPayloadDoc, pending["payload"].as<const char*>());
      payload = fallbackPayloadDoc.as<JsonObject>();
    }

    const char* action = payload["action"] | "";

    String effectiveAction = action;
    if (effectiveAction.length() == 0) {
      if (strcmp(cmdType, "start") == 0) effectiveAction = "start_monitor";
      else if (strcmp(cmdType, "stop") == 0) effectiveAction = "stop_monitor";
      else effectiveAction = cmdType;
    }

    if (!payload["interval_sec"].is<int>() && effectiveAction == "start_monitor") {
      payload["interval_sec"] = 5;
    }

    String payloadStr;
    serializeJson(payload, payloadStr);

    Logf("[CMD] Received command ID: %s | Action: %s (Type: %s)\n",
         cmdId ? cmdId : "null", effectiveAction.c_str(), cmdType);
    if (s_commandCallback && cmdId && strlen(cmdId) > 0) {
      s_commandCallback(cmdId, effectiveAction.c_str(), payloadStr.c_str());
    }
  }
}

bool acknowledgeSensorCommand(const char* cmdId, bool success, const char* sensorStateJson) {
#if AQUA_TRANSPORT_MQTT
  // The MQTT ack carries only the result; `sensor_state` was a REST-ism for a response body the
  // broker has no way to return. The gateway learns the same thing from the node's next
  // telemetry envelope, which is strictly fresher.
  (void)sensorStateJson;
  return aquaLinkAck(cmdId, success);
#else
  if (WiFi.status() != WL_CONNECTED || !cmdId || strlen(cmdId) == 0) {
    return false;
  }

  String ackUrl = String("https://") + AQUACONTROL_HOST + "/api/commands/" + cmdId + "/acknowledge";

  StaticJsonDocument<512> ackDoc;
  ackDoc["success"] = success;
  if (sensorStateJson && strlen(sensorStateJson) > 0) {
    StaticJsonDocument<256> stateDoc;
    if (deserializeJson(stateDoc, sensorStateJson) == DeserializationError::Ok) {
      ackDoc["sensor_state"] = stateDoc.as<JsonObject>();
    }
  }

  String jsonOut;
  serializeJson(ackDoc, jsonOut);

  WiFiClientSecure ackClient;
  ackClient.setInsecure();
  ackClient.setHandshakeTimeout(4);
  ackClient.setTimeout(4);

  HTTPClient ackHttp;
  ackHttp.begin(ackClient, ackUrl);
  ackHttp.addHeader("Content-Type", "application/json");
  ackHttp.addHeader("X-Device-Key", AQUACONTROL_DEVICE_KEY);

  esp_task_wdt_reset();
  int code = ackHttp.POST(jsonOut);
  esp_task_wdt_reset();

  bool ok = (code >= 200 && code < 300);
#ifdef DEBUG
  Logf("[ACK] Command %s -> HTTP %d (%s)\n", cmdId, code, ok ? "OK" : "FAIL");
#endif
  ackHttp.end();
  ackClient.stop();
  return ok;
#endif
}

/**
 * Handle a command that arrived over MQTT.
 *
 * Reshapes the MQTT payload into the legacy response envelope and hands it to the existing
 * dispatcher. The alternative — a second dispatcher for MQTT — would be two copies of the same
 * action semantics, free to drift apart. Only the transport differs; the actions do not.
 */
#if AQUA_TRANSPORT_MQTT
void wifiHttpHandleMqttCommand(const char* payload, size_t length) {
  StaticJsonDocument<1024> in;
  if (deserializeJson(in, payload, length) != DeserializationError::Ok) {
    Logln("[AQUA] command payload was not valid JSON; ignored");
    return;
  }

  const char* cmdId  = in["cmd_id"] | in["id"] | "";
  const char* action = in["action"] | "";
  if (action[0] == '\0') {
    // A command with no action cannot be acted on or acknowledged meaningfully. Reporting it
    // is better than dropping it silently, because a malformed command means the gateway and
    // the node disagree about the format.
    Logln("[AQUA] command had no action field");
    return;
  }

  StaticJsonDocument<768> legacy;
  legacy["has_command"] = true;
  JsonObject pc = legacy.createNestedObject("pending_command");
  pc["id"] = cmdId;
  JsonObject pl = pc.createNestedObject("payload");
  pl["action"] = action;
  for (JsonPairConst kv : in["target"].as<JsonObjectConst>()) {
    pl[kv.key()] = kv.value();
  }

  // Schema v2 puts the experiment at the *envelope* level (`exp`); the legacy handler reads it
  // from inside the payload as `experiment_id`. Dropping it here is not a visible failure:
  // the node falls back to its own "exp_default", the command is acknowledged, and every run
  // in the archive carries the same label. That is precisely the outcome the experiment id
  // exists to prevent, so it is mapped explicitly instead of looking like a missing feature.
  // A null `exp` is meaningful and must stay absent -- it means "no experiment", which is
  // what a monitor run sends, and the legacy default would invent one to group rows under.
  if (!in["exp"].isNull()) {
    pl["experiment_id"] = in["exp"];
  }

  String out;
  serializeJson(legacy, out);
  processTelemetryResponse(out);
}
#endif

bool pollPendingCommands() {
#if AQUA_TRANSPORT_MQTT
  // Commands are pushed over MQTT now. Keeping an idle HTTPS poll would reintroduce on this node
  // the exact TLS-handshake cost the migration exists to remove — for a queue that is almost
  // always empty. The subscription in aqua_link replaces it.
  return false;
#else
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  String url = String("https://") + AQUACONTROL_HOST + "/api/commands/pending";

  WiFiClientSecure pollClient;
  pollClient.setInsecure();
  pollClient.setHandshakeTimeout(3);
  pollClient.setTimeout(3);

  HTTPClient pollHttp;
  pollHttp.begin(pollClient, url);
  pollHttp.addHeader("X-Device-Key", AQUACONTROL_DEVICE_KEY);

  esp_task_wdt_reset();
  int httpCode = pollHttp.GET();
  esp_task_wdt_reset();

  if (httpCode > 0) {
    String response = pollHttp.getString();
    pollHttp.end();
    pollClient.stop();
    if (httpCode >= 200 && httpCode < 300) {
      processTelemetryResponse(response);
      return true;
    }
    return false;
  }

  pollHttp.end();
  pollClient.stop();
  return false;
#endif
}

/* ----------------------------- HTTPS POST ----------------------------- */

/* ------------------ V4: Telemetry RAM ring buffer + Bulk flush ------------------ */

#define TELEMETRY_QUEUE_CAPACITY  32
#define TELEMETRY_FLUSH_THRESHOLD 12

struct QueuedSample {
  uint32_t seconds2000;
  int16_t  readings[MAX_SENSOR_READINGS];
  int16_t  rtcTempCenti;
  uint32_t status;
  uint16_t msRemainder;
};

static QueuedSample s_q[TELEMETRY_QUEUE_CAPACITY];
static uint8_t s_qHead = 0;
static uint8_t s_qTail = 0;
static uint8_t s_qCount = 0;

uint8_t telemetryQueueCount() {
#if AQUA_TRANSPORT_MQTT
  return (uint8_t)aquaLinkQueued();
#else
  return s_qCount;
#endif
}

static void queuePush(const QueuedSample& s) {
  if (s_qCount >= TELEMETRY_QUEUE_CAPACITY) {
    s_qHead = (uint8_t)((s_qHead + 1) % TELEMETRY_QUEUE_CAPACITY);
    s_qCount--;
  }
  s_q[s_qTail] = s;
  s_qTail = (uint8_t)((s_qTail + 1) % TELEMETRY_QUEUE_CAPACITY);
  s_qCount++;
}

/** Rellena un objeto JSON con una muestra. Emite rtc_timestamp_ms como epoch UTC ms (ADR-3). */
static void buildSampleObject(JsonObject obj, uint32_t seconds2000, const int16_t* readings,
                              int16_t rtcTempCenti, uint32_t status, uint16_t msRemainder) {
  uint64_t unixTs = (uint64_t)seconds2000 + UNIX_OFFSET_2000;
  // Si el RTC trae una fecha inválida (< 2020), cae a la hora de sistema sincronizada por NTP.
  if (unixTs < 1577836800ULL) {
    time_t sysNow = time(nullptr);
    if (sysNow >= 1577836800) {
      unixTs = (uint64_t)(sysNow - LOCAL_TZ_OFFSET_SEC);
      seconds2000 = (uint32_t)(unixTs - UNIX_OFFSET_2000);
    }
  }

  time_t t = (time_t)unixTs;
  struct tm* tm = gmtime(&t);
  char datetime[30];
  snprintf(datetime, sizeof(datetime), "%04d-%02d-%02dT%02d:%02d:%02d-05:00",
           tm->tm_year + 1900, tm->tm_mon + 1, tm->tm_mday,
           tm->tm_hour, tm->tm_min, tm->tm_sec);

  obj["datetime"]           = datetime;
  obj["seconds_since_2000"] = seconds2000;
  // ADR-3: instante absoluto (epoch Unix UTC) con resolución de milisegundos, base del Reloj Maestro.
  obj["rtc_timestamp_ms"]   = (unixTs + LOCAL_TZ_OFFSET_SEC) * 1000ULL + (uint64_t)msRemainder;
  obj["water_temp_centi"]   = readings[0];
  obj["do_sat_deci_pct"]    = readings[1];
  obj["do_milli_mg_l"]      = readings[2];
  obj["param3_centi"]       = readings[3];
  obj["param4_centi"]       = readings[4];
#if defined(SENSOR_BATTERY_ADC)
  obj["battery_mv"]         = (uint16_t)readings[MAX_SENSOR_READINGS - 1];
#endif
  obj["rtc_temp_centi"]     = rtcTempCenti;
  obj["status"]             = status;
  obj["sent"]               = true;
}

/** POST genérico de un payload JSON ya serializado (keep-alive + un reintento). */
static bool postTelemetryJson(const String& jsonOut) {
#ifdef DEBUG
  Logln("HTTPS -> AquaControl:");
  Log("   Payload: ");
  Logln(jsonOut.c_str());
#endif

  if (!s_clientConfigured) {
    s_client.setInsecure(); // ponytail: skip cert pin — Vercel rotates certs, pinning would break
    s_client.setHandshakeTimeout(4); // 4s TLS handshake timeout
    s_client.setTimeout(4);          // 4s socket stream timeout
    s_clientConfigured = true;
  }

  String url = String("https://") + AQUACONTROL_HOST + AQUACONTROL_PATH;

  // If not connected, initialize HTTPClient with persistent Keep-Alive
  if (!s_http.connected()) {
    s_http.begin(s_client, url);
    s_http.setReuse(true); // HTTP/1.1 Keep-Alive persistent connection
    s_http.setConnectTimeout(4000);
    s_http.setTimeout(4000);
  }

  s_http.addHeader("Content-Type", "application/json");
  s_http.addHeader("X-Device-Key", AQUACONTROL_DEVICE_KEY);

  esp_task_wdt_reset();
  int httpCode = s_http.POST(jsonOut);
  esp_task_wdt_reset();

  if (httpCode > 0) {
    String response = s_http.getString(); // Consumes body to keep stream aligned for next request
#ifdef DEBUG
    Logf("   HTTP %d: %s\n", httpCode, response.c_str());
#endif
    if (httpCode >= 200 && httpCode < 300) {
      processTelemetryResponse(response);
      return true;
    }
    return false;
  }

  // If POST failed (e.g. idle keep-alive timed out on Vercel), reset socket and retry once
  Logf("   Persistent connection dropped (err %d), reconnecting...\n", httpCode);
  closeHttpConnection();

  s_http.begin(s_client, url);
  s_http.setReuse(true);
  s_http.setConnectTimeout(4000);
  s_http.setTimeout(4000);
  s_http.addHeader("Content-Type", "application/json");
  s_http.addHeader("X-Device-Key", AQUACONTROL_DEVICE_KEY);

  esp_task_wdt_reset();
  httpCode = s_http.POST(jsonOut);
  esp_task_wdt_reset();

  if (httpCode > 0) {
    String response = s_http.getString();
#ifdef DEBUG
    Logf("   HTTP %d (retry): %s\n", httpCode, response.c_str());
#endif
    if (httpCode >= 200 && httpCode < 300) {
      processTelemetryResponse(response);
      return true;
    }
    return false;
  }

  Logf("   HTTP POST retry failed: %s (err %d)\n", s_http.errorToString(httpCode).c_str(), httpCode);
  closeHttpConnection();
  return false;
}

/** Encola una muestra para el próximo flush bulk (no bloqueante). */
void queueTelemetrySample(uint32_t seconds2000, int16_t* readings, int16_t rtcTempCenti, uint32_t status) {
#if AQUA_TRANSPORT_MQTT
  // Tag the batch with the run it belongs to. A monitor run and an experiment sample
  // identically, so this field is the only thing that tells them apart downstream.
  extern char activeExperimentId[40];
  extern bool experimentActive;
  aquaLinkSetExperiment(experimentActive ? activeExperimentId : nullptr);

  // The MQTT path keeps its own batch. `msRemainder` is not legacy baggage: schema v2's
  // `t_dev_ms` is epoch milliseconds, and the RTC resolves only whole seconds, so without
  // it every sample inside one second shares a timestamp -- invisible at 1 Hz, and five
  // points stacked on one instant at the 5 Hz experiment rate.
  aquaLinkQueue(seconds2000, (uint16_t)(millis() % 1000), readings, rtcTempCenti, status);
  return;
#endif
  QueuedSample s;
  s.seconds2000 = seconds2000;
  s.rtcTempCenti = rtcTempCenti;
  s.status = status;
  s.msRemainder = (uint16_t)(millis() % 1000);
  for (int i = 0; i < MAX_SENSOR_READINGS; i++) s.readings[i] = readings[i];
  queuePush(s);
}

/**
 * Envía todo el buffer RAM como un único paquete bulk (V4 §2.2).
 * Devuelve true si el POST se confirmó y el buffer quedó vacío. Si falla, el buffer se preserva.
 */
bool flushTelemetryBulk() {
#if AQUA_TRANSPORT_MQTT
  // Returns false when the broker is down, which is what makes the caller retain the batch:
  // an outage costs latency, not data.
  return aquaLinkFlush();
#else
  if (s_qCount == 0) return true;
  if (WiFi.status() != WL_CONNECTED) return false;

  extern char activeExperimentId[40];
  extern bool experimentActive;

  const size_t capacity = 320 + (size_t)s_qCount * 240;
  DynamicJsonDocument doc(capacity);
  JsonObject root = doc.to<JsonObject>();
  root["experiment_id"] = (experimentActive && strlen(activeExperimentId) > 0) ? activeExperimentId : "idle";

  JsonArray bulkArr = root.createNestedArray("payload");
  for (uint8_t i = 0; i < s_qCount; i++) {
    const QueuedSample& s = s_q[(uint8_t)((s_qHead + i) % TELEMETRY_QUEUE_CAPACITY)];
    JsonObject obj = bulkArr.createNestedObject();
    buildSampleObject(obj, s.seconds2000, s.readings, s.rtcTempCenti, s.status, s.msRemainder);
  }

  String jsonOut;
  serializeJson(doc, jsonOut);
  const uint8_t batch = s_qCount;

  if (postTelemetryJson(jsonOut)) {
    s_qHead = 0;
    s_qTail = 0;
    s_qCount = 0;
    Logf("Bulk flush OK (%u samples).\n", (unsigned)batch);
    return true;
  }
  Logf("Bulk flush failed; %u samples kept in RAM.\n", (unsigned)s_qCount);
  return false;
#endif
}

/** Publica una muestra individual de inmediato (replay de SD y muestras manuales). */
bool publishToAquaControl(uint32_t seconds2000, int16_t* readings, int16_t rtcTempCenti, uint32_t status) {
#if AQUA_TRANSPORT_MQTT
  // Replayed samples must reach the same place live ones do. If a batch is already in flight we
  // simply join it: ordering within the batch is preserved by insertion, and the envelope's
  // device timestamp is what the archive sorts on anyway.
  extern char activeExperimentId[40];
  extern bool experimentActive;
  aquaLinkSetExperiment(experimentActive ? activeExperimentId : nullptr);
  aquaLinkQueue(seconds2000, (uint16_t)(millis() % 1000), readings, rtcTempCenti, status);
  return aquaLinkFlush();
#else
  if (WiFi.status() != WL_CONNECTED) {
    Logln("publishToAquaControl: WiFi not connected.");
    return false;
  }

  extern char activeExperimentId[40];
  extern bool experimentActive;

  StaticJsonDocument<768> doc;
  JsonObject root = doc.to<JsonObject>();
  root["experiment_id"] = (experimentActive && strlen(activeExperimentId) > 0) ? activeExperimentId : "idle";

  JsonArray bulkArr = root.createNestedArray("payload");
  JsonObject obj = bulkArr.createNestedObject();
  buildSampleObject(obj, seconds2000, readings, rtcTempCenti, status, (uint16_t)(millis() % 1000));

  String jsonOut;
  serializeJson(doc, jsonOut);
  return postTelemetryJson(jsonOut);
#endif
}

#endif // WIFI_MODULE
