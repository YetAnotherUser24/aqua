/**
 * @file aqua_link.cpp
 * @brief Schema-v2 transport para nodo-vibracion sobre MQTT.
 */

#include "aqua_link.h"
#include "config.h"

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <sys/time.h>
#include <esp_random.h>
#include <ArduinoJson.h>

#include "aqua_protocol.h"

namespace {

struct Queued {
  VibrationTelemetry state;
  uint64_t unixMs;
};

WiFiClient s_net;
PubSubClient s_mqtt(s_net);

char s_buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
aqua::EnvelopeBuilder s_builder(aqua::Role::Vibration, 0, s_buffer, sizeof(s_buffer));

Queued s_batch[aqua::AQUA_ENVELOPE_BATCH_LIMIT];
size_t s_queued = 0;

uint32_t s_sequence = 0;
uint32_t s_dropped = 0;
uint32_t s_bootId = 0;
uint32_t s_nextAttemptMs = 0;
bool s_presencePublished = false;

uint64_t s_broadcastMs = 0;
uint32_t s_broadcastAtMs = 0;
bool s_haveBroadcast = false;

char s_experimentId[40] = {0};

aqua::TimeSource timeSourceFor(uint64_t unixSeconds) {
  return unixSeconds >= aqua::kPlausibleEpochS ? aqua::TimeSource::Ntp : aqua::TimeSource::Monotonic;
}

size_t populateFields(const VibrationTelemetry& st, uint64_t unixMs, aqua::FieldValue* out, size_t capacity) {
  if (capacity < 9) return 0;
  size_t n = 0;
  out[n++] = aqua::FieldValue::integer64(aqua::AQUA_SAMPLE_TIME_FIELD, static_cast<long long>(unixMs));
  out[n++] = aqua::FieldValue::num("vib_ax_g", st.ax_g, 3);
  out[n++] = aqua::FieldValue::num("vib_ay_g", st.ay_g, 3);
  out[n++] = aqua::FieldValue::num("vib_az_g", st.az_g, 3);
  out[n++] = aqua::FieldValue::num("vib_rms_g", st.rms_g, 3);
  out[n++] = aqua::FieldValue::num("vib_peak_g", st.peak_g, 3);
  out[n++] = aqua::FieldValue::num("vib_crest_factor", st.crest_factor, 2);
  out[n++] = aqua::FieldValue::num("vib_temp_c", st.temp_c, 2);
  out[n++] = aqua::FieldValue::integer("dev_status", st.dev_status);
  return n;
}

void onMqttMessage(char* topic, uint8_t* payload, unsigned int length) {
  if (strcmp(topic, aqua::AQUA_TIME_TOPIC) == 0) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length) == DeserializationError::Ok) {
      if (doc["t_dev_ms"].is<long long>()) {
        long long ms = doc["t_dev_ms"].as<long long>();
        if (aqua::plausibleEpochMs(ms)) {
          s_broadcastMs = (uint64_t)ms;
          s_broadcastAtMs = millis();
          s_haveBroadcast = true;
        }
      }
    }
  }
}

} // namespace

uint64_t aquaLinkNowMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  const uint64_t local = (uint64_t)tv.tv_sec * 1000ULL + (uint64_t)(tv.tv_usec / 1000);
  if (local >= aqua::kPlausibleEpochS * 1000ULL) return local;
  if (s_haveBroadcast) return aqua::extrapolateEpochMs(s_broadcastMs, s_broadcastAtMs, millis());
  return 0;
}

bool aquaLinkBegin() {
  if (s_bootId == 0) {
    s_bootId = esp_random();
    s_builder.setBootId(s_bootId);
  }

  s_mqtt.setServer(MQTT_BROKER_HOST, MQTT_BROKER_PORT);
  s_mqtt.setCallback(onMqttMessage);
  s_mqtt.setBufferSize(aqua::AQUA_ENVELOPE_MAX_BYTES);
  return true;
}

bool aquaLinkConnected() {
  return s_mqtt.connected();
}

void aquaLinkLoop() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  if (s_mqtt.connected()) {
    s_mqtt.loop();
    return;
  }

  const uint32_t now = millis();
  if (now < s_nextAttemptMs) {
    return;
  }

  char presenceTopicBuf[64];
  if (!aqua::presenceTopic(aqua::Role::Vibration, presenceTopicBuf, sizeof(presenceTopicBuf))) {
    return;
  }

  // LWT payload: {"online":false}
  const char* willPayload = "{\"online\":false}";

  Serial.printf("[MQTT] Conectando a %s:%d como %s...\n", MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_CLIENT_ID);
  bool ok = false;
  if (MQTT_USER != nullptr) {
    ok = s_mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASSWORD, presenceTopicBuf, 1, true, willPayload);
  } else {
    ok = s_mqtt.connect(MQTT_CLIENT_ID, presenceTopicBuf, 1, true, willPayload);
  }

  if (ok) {
    Serial.println("[MQTT] Conectado con exito.");
    s_mqtt.subscribe(aqua::AQUA_TIME_TOPIC, 0);

    // Publicar presencia en linea
    const char* onlinePayload = "{\"online\":true}";
    s_mqtt.publish(presenceTopicBuf, (const uint8_t*)onlinePayload, strlen(onlinePayload), true);
    s_presencePublished = true;
    s_nextAttemptMs = 0;
  } else {
    Serial.printf("[MQTT] Fallo de conexion, rc=%d. Reintentando en 3s\n", s_mqtt.state());
    s_nextAttemptMs = now + 3000;
  }
}

void aquaLinkQueue(const VibrationTelemetry& state, uint64_t unixMs) {
  if (s_queued >= aqua::AQUA_ENVELOPE_BATCH_LIMIT) {
    aquaLinkFlush();
  }

  if (s_queued < aqua::AQUA_ENVELOPE_BATCH_LIMIT) {
    s_batch[s_queued].state = state;
    s_batch[s_queued].unixMs = unixMs;
    s_queued++;
  } else {
    s_dropped++;
  }
}

bool aquaLinkFlush() {
  if (s_queued == 0) return true;
  if (!s_mqtt.connected()) return false;

  const uint32_t seq = ++s_sequence;
  s_builder.begin(seq);

  for (size_t i = 0; i < s_queued; ++i) {
    aqua::FieldValue fields[9];
    const size_t count = populateFields(s_batch[i].state, s_batch[i].unixMs, fields, 9);
    s_builder.addSample(fields, count);
  }

  const uint64_t newestMs = s_batch[s_queued - 1].unixMs;
  const aqua::TimeSource src = timeSourceFor(newestMs / 1000ULL);
  const int rssi = WiFi.RSSI();

  const size_t len = s_builder.finish(newestMs, src, rssi);
  if (len == 0) {
    s_dropped += s_queued;
    s_queued = 0;
    return false;
  }

  char topic[64];
  aqua::telemetryTopic(aqua::Role::Vibration, topic, sizeof(topic));

  const bool published = s_mqtt.publish(topic, (const uint8_t*)s_buffer, len, false);
  if (published) {
    s_queued = 0;
    return true;
  } else {
    s_dropped += s_queued;
    return false;
  }
}

size_t aquaLinkQueued() { return s_queued; }
uint32_t aquaLinkDropped() { return s_dropped; }
