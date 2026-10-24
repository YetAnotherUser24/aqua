/**
 * @file aqua_link.cpp
 * @brief Schema-v2 MQTT transport. See aqua_link.h for the design rationale.
 *
 * Zero heap: every buffer is file-static, because this node streams continuously and repeated
 * malloc/free fragments the ESP32 heap (AGENTS.md §6.1).
 */

#include "aqua_link.h"

#if defined(WIFI_MODULE) && AQUA_TRANSPORT_MQTT

#include <PubSubClient.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <sys/time.h>
#include <ArduinoJson.h>
#include <esp_random.h>

#include "aqua_protocol.h"
#include "config.h"
#include "log.h"
#include "n_logger_config.h"
#include "rtc_utils.h"
#include "wifi_http.h"

/** Seconds between 1970-01-01 and 2000-01-01. The node's native timebase is the latter. */
#ifndef UNIX_OFFSET_2000
#define UNIX_OFFSET_2000 946684800ULL
#endif

/**
 * Below this the DS3231 date is nonsense and the clock came from somewhere else.
 *
 * The threshold is the shared `aqua::kPlausibleEpochS`; this node used to declare its own copy
 * of the same value, as did the other three, and one of those copies had drifted.
 */

namespace {

struct Queued {
  uint64_t unixMs;
  int16_t readings[MAX_SENSOR_READINGS];
  int16_t rtcTempCenti;
  uint32_t status;
};

WiFiClient s_net;
PubSubClient s_mqtt(s_net);
IPAddress s_brokerIp;
uint8_t s_resolveFailures = 0;

/**
 * Resolves the MQTT broker dynamically:
 * 1. Literal IPv4 address (e.g. override via -DAQUA_MQTT_HOST="x.x.x.x")
 * 2. mDNS query (ESPmDNS, bounded timeout 800 ms) for "aqua-station.local" or bare hostname
 * 3. Unicast DNS fallback (WiFi.hostByName)
 * 4. Default gateway fallback if AQUA_MQTT_HOST is "gateway"
 */
bool resolveBrokerIp(IPAddress& outIp) {
  if (outIp != INADDR_NONE && outIp != IPAddress(0, 0, 0, 0)) {
    return true;
  }
  if (outIp.fromString(AQUA_MQTT_HOST)) {
    return true;
  }
  char host[32];
  strncpy(host, AQUA_MQTT_HOST, sizeof(host) - 1);
  host[sizeof(host) - 1] = '\0';
  char* dot = strstr(host, ".local");
  if (dot) *dot = '\0';

  MDNS.begin(AQUA_MQTT_CLIENT_ID);
  outIp = MDNS.queryHost(host, 800);
  if (outIp != INADDR_NONE && outIp != IPAddress(0, 0, 0, 0)) {
    Logf("[AQUA] mDNS resolved %s -> %s\n", host, outIp.toString().c_str());
    return true;
  }
  if (WiFi.hostByName(AQUA_MQTT_HOST, outIp) && outIp != INADDR_NONE && outIp != IPAddress(0, 0, 0, 0)) {
    Logf("[AQUA] DNS resolved %s -> %s\n", AQUA_MQTT_HOST, outIp.toString().c_str());
    return true;
  }
  if (strcmp(AQUA_MQTT_HOST, "gateway") == 0 || true) {
    outIp = WiFi.gatewayIP();
    if (outIp != INADDR_NONE && outIp != IPAddress(0, 0, 0, 0)) {
      Logf("[AQUA] fallback to gateway -> %s\n", outIp.toString().c_str());
      return true;
    }
  }
  return false;
}

char s_buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];           // one envelope, reused
aqua::EnvelopeBuilder s_builder(aqua::Role::OdSensor, 0, s_buffer, sizeof(s_buffer));

Queued s_batch[aqua::AQUA_ENVELOPE_BATCH_LIMIT];
size_t s_queued = 0;

uint32_t s_bootId = 0;
uint32_t s_seq = 0;
uint32_t s_dropped = 0;
uint32_t s_nextAttemptMs = 0;
bool s_presencePublished = false;
/** Set while a session is up, so a drop is reported once rather than every loop. */
bool s_everConnected = false;
uint32_t s_connectedAtMs = 0;
bool s_configured = false;

/**
 * Latest DS3231 time seen by `aquaLinkQueue`, epoch ms, and when `aqua/time` last went out.
 *
 * The sensor owns the deployment's persistent clock (`docs/TIME-SYNC.md`), so it is the tier-3
 * source for the station: the Pi has no RTC, and after a power cut it learns the time from
 * here. The transport is already handed the authoritative time on every queue, so it keeps the
 * newest rather than reaching for the RTC itself.
 */
uint64_t s_lastRtcMs = 0;
uint32_t s_timePublishedAtMs = 0;

/** How often the retained time broadcast is refreshed. */
constexpr uint32_t kTimeBroadcastIntervalMs = 30000UL;

/** Stated uncertainty of the DS3231 reading, ms. It is a real clock, not a guess. */
constexpr unsigned kTimeUncertaintyMs = 50;

/**
 * Experiment the samples in flight belong to, or empty for none.
 *
 * The gateway distinguishes a monitor run from an experiment by this field alone -- the two
 * sample identically -- so without it every row looks like idle monitoring and no completed
 * experiment can be queried afterwards. Held as a copy because the caller's buffer
 * (``activeExperimentId`` in main.cpp) can be rewritten between queueing and flushing.
 */
char s_experimentId[40] = {0};

/**
 * Provenance of the device clock.
 *
 * Deliberately conservative: a plausible date means the DS3231 supplied it, and anything else
 * is reported as `monotonic` — ordered but not anchored. Claiming `ntp` here would be a lie the
 * export could not detect, which is the whole reason `t_src` exists.
 */
aqua::TimeSource timeSourceFor(uint64_t unixSeconds) {
  return unixSeconds >= aqua::kPlausibleEpochS ? aqua::TimeSource::Rtc : aqua::TimeSource::Monotonic;
}

void presenceTopic(char* out, size_t capacity) {
  snprintf(out, capacity, "aqua/presence/%s", aqua::nodeId(aqua::Role::OdSensor));
}

void publishPresence(bool online, const char* reason) {
  if (!s_mqtt.connected()) return;
  char topic[64];
  presenceTopic(topic, sizeof(topic));
  char payload[128];
  snprintf(payload, sizeof(payload), "{\"online\":%s,\"role\":\"od_sensor\",\"reason\":\"%s\"}",
           online ? "true" : "false", reason);
  // Retained: a gateway that restarts learns current state immediately instead of waiting for
  // the next heartbeat.
  s_mqtt.publish(topic, payload, true);
}

/**
 * Inbound command.
 *
 * The payload is reshaped into the *legacy* response envelope and handed to
 * `wifiHttpHandleMqttCommand`, which forwards it to the existing `processTelemetryResponse`
 * dispatcher. That is deliberate: the command semantics (which actions exist, what they clamp,
 * how the state machine reacts) are already implemented and reviewed. Re-implementing them for
 * MQTT would be two copies of the same logic that would drift.
 */
/**
 * Has this command's deadline already passed?
 *
 * The gateway stamps `deadline_ms` when it issues a command, and nothing on the device ever
 * read it. A command that arrived late -- queued while the session was down, or redelivered --
 * was carried out exactly as if it had just been requested. An operator's intent expires, and
 * acting after it does is not neutral: it moves an actuator for a reason nobody is waiting on.
 *
 * Enforced here rather than in each node's handler, so all four nodes behave the same.
 *
 * Skipped outright when the device clock is not anchored (before 2020). This node gets its
 * time from the DS3231 and falls back to NTP, but an unanchored clock must never be able to
 * reject every command -- that is a worse failure than the one being fixed.
 *
 * @return true when the command is stale and must not be applied
 */
static bool commandExpired(const char* payload, size_t length) {
  // 768, not 256. The command envelope is ~230 bytes with a nested `target`, and a document
  // too small to hold it does not truncate -- it fails to parse. That failure used to return
  // false here, which silently disabled the whole check while every layer still looked right.
  // A probe that cannot read the deadline must say so rather than pretend there isn't one.
  StaticJsonDocument<768> probe;
  const DeserializationError err = deserializeJson(probe, payload, length);
  if (err) {
    Logf("[AQUA] deadline probe could not parse (%s); applying\n", err.c_str());
    return false;
  }
  if (!probe["deadline_ms"].is<long long>()) return false;

  const long long deadlineMs = probe["deadline_ms"].as<long long>();

  // This node's clock is the DS3231 rather than the MQTT broadcast, so the reading is taken
  // here and only the judgement is shared. An unanchored clock answers "cannot judge" instead of
  // rejecting every command it receives, which is the direction that keeps the node obeying.
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  const uint64_t nowMs = (uint64_t)tv.tv_sec * 1000ULL + (uint64_t)(tv.tv_usec / 1000);
  if (!aqua::deadlinePassed(deadlineMs, nowMs)) return false;
  const long long ageMs = (long long)nowMs - deadlineMs;

  const char* cmdId = probe["cmd_id"].is<const char*>() ? probe["cmd_id"].as<const char*>() : "";
  Logf("[AQUA] command %s expired %lld ms ago; rejected\n", cmdId, ageMs);
  if (cmdId[0] != '\0') aquaLinkAck(cmdId, false);
  return true;
}

void onMessage(char* topic, uint8_t* payload, unsigned int length) {
  // The topic is logged rather than discarded. An earlier version dropped it, which made a
  // message arriving on an unexpected topic indistinguishable from one with a malformed
  // payload -- and those two have completely different fixes.
  Logf("[AQUA] rx %s (%u bytes)\n", topic ? topic : "(null)", length);

  if (topic && strcmp(topic, "aqua/time") == 0) {
    StaticJsonDocument<256> tDoc;
    if (deserializeJson(tDoc, payload, length) == DeserializationError::Ok) {
      uint64_t tMs = 0;
      if (tDoc.containsKey("t_dev_ms")) {
        tMs = tDoc["t_dev_ms"].as<uint64_t>();
      } else if (tDoc.containsKey("epoch_s")) {
        tMs = tDoc["epoch_s"].as<uint64_t>() * 1000ULL;
      }
      if (tMs >= 1577836800000ULL) { // >= 2020-01-01
        time_t localNow = (time_t)(tMs / 1000ULL) - LOCAL_TZ_OFFSET_SEC;
        uint32_t sec2000 = (uint32_t)(localNow - UNIX_OFFSET_2000);
        RtcDateTime syncDt(sec2000);
        setRtcDateTime(syncDt);
        Logf("[TIME] DS3231 synchronized from aqua/time: %llu ms\n", tMs);
      }
    }
    return;
  }

  if (commandExpired(reinterpret_cast<const char*>(payload), length)) return;
  wifiHttpHandleMqttCommand(reinterpret_cast<const char*>(payload), length);
}

bool connectOnce() {
  if (!resolveBrokerIp(s_brokerIp)) {
    Logf("[AQUA] cannot connect: host %s not resolved\n", AQUA_MQTT_HOST);
    return false;
  }
  s_mqtt.setServer(s_brokerIp, AQUA_MQTT_PORT);

  const char* user = AQUA_MQTT_USER[0] ? AQUA_MQTT_USER : nullptr;
  const char* pass = AQUA_MQTT_PASSWORD[0] ? AQUA_MQTT_PASSWORD : nullptr;

  char willTopic[64];
  presenceTopic(willTopic, sizeof(willTopic));
  const char* willPayload = "{\"online\":false,\"role\":\"od_sensor\",\"reason\":\"will\"}";

  // Last Will: the broker reports the drop the moment the connection dies, which is what
  // replaces the previous 60-second heartbeat heuristic. QoS 1 + retain, so a gateway that
  // starts later still sees the node as offline.
  const bool ok = s_mqtt.connect(AQUA_MQTT_CLIENT_ID, user, pass, willTopic, 1, true, willPayload);
  if (!ok) {
    s_resolveFailures++;
    if (s_resolveFailures >= 3) {
      s_resolveFailures = 0;
      s_brokerIp = INADDR_NONE;
    }
    return false;
  }

  s_resolveFailures = 0;
  Logln("[AQUA] mqtt connected");
  s_presencePublished = false;  // republished by the next loop so a restart is visible
  s_mqtt.subscribe("aqua/time", 1);
  return s_mqtt.subscribe("aqua/cmd/od_sensor", 1);
}

/** Convert one queued reading into the schema-v2 field set. */
void buildFields(const Queued& entry, aqua::FieldValue* out, size_t& count) {
  namespace f = aqua::od_sensor_fields;
  const uint64_t unixS = entry.unixMs / 1000ULL;

  count = 0;
  // The sample's own time, not the envelope's. A batch spans time, so without this the five
  // samples a 5 Hz node packs into one envelope all share the envelope timestamp and plot as
  // a single point.
  out[count++] =
      aqua::FieldValue::integer64(aqua::AQUA_SAMPLE_TIME_FIELD, static_cast<long long>(entry.unixMs));
  out[count++] = aqua::FieldValue::num(f::kDoMgL, entry.readings[2] / 1000.0, 3);
  out[count++] = aqua::FieldValue::integer(f::kDoRaw, entry.readings[2]);
  out[count++] = aqua::FieldValue::num(f::kSatPct, entry.readings[1] / 10.0, 2);
  out[count++] = aqua::FieldValue::integer(f::kSatRaw, entry.readings[1]);
  out[count++] = aqua::FieldValue::num(f::kTempC, entry.readings[0] / 100.0, 2);
  out[count++] = aqua::FieldValue::integer(f::kTempRaw, entry.readings[0]);
  out[count++] = aqua::FieldValue::integer(f::kAux1Raw, entry.readings[3]);
  out[count++] = aqua::FieldValue::integer(f::kAux2Raw, entry.readings[4]);
#if defined(SENSOR_BATTERY_ADC)
  out[count++] = aqua::FieldValue::num(f::kBatteryV,
                                       entry.readings[MAX_SENSOR_READINGS - 1] / 1000.0, 2);
#else
  out[count++] = aqua::FieldValue::num(f::kBatteryV, 0.0, 2);
#endif
  out[count++] = aqua::FieldValue::num(f::kRtcTempC, entry.rtcTempCenti / 100.0, 2);
  out[count++] = aqua::FieldValue::integer(f::kRtcHealth, unixS >= aqua::kPlausibleEpochS ? 1 : 0);
  // The raw bitmask is passed through untouched. Interpreting it here would duplicate knowledge
  // that belongs to `sensor_engine`, and a wrong interpretation is worse than none.
  out[count++] = aqua::FieldValue::integer(f::kDevStatus, static_cast<long>(entry.status));
}

}  // namespace

// ---------------------------------------------------------------------------

bool aquaLinkBegin() {
  s_bootId = esp_random() & 0xFFFFu;
  // The builder was constructed with a placeholder (the RNG is not up at static-init),
  // so push the real per-boot id in now. Without this the published boot_id stays 0 and
  // the (node_id, boot_id, seq) key cannot separate two boots: after a reboot `seq`
  // restarts at 1 while the gateway still holds the previous boot's high-water mark, so a
  // clean restart is logged as an `out_of_order_batch`. The pump does the equivalent
  // inline (it rebuilds the builder here).
  s_builder.setBootId(s_bootId);
  s_seq = 0;
  s_queued = 0;
  s_dropped = 0;
  s_presencePublished = false;
  s_nextAttemptMs = 0;

  if (resolveBrokerIp(s_brokerIp)) {
    s_mqtt.setServer(s_brokerIp, AQUA_MQTT_PORT);
  } else {
    s_mqtt.setServer(AQUA_MQTT_HOST, AQUA_MQTT_PORT);
  }
  s_mqtt.setCallback(onMessage);
  s_mqtt.setKeepAlive(AQUA_MQTT_KEEPALIVE_S);
  // Bounded explicitly. The library default is 15 s of busy-waiting for a CONNACK, which is
  // long enough to trip a watchdog when the broker accepts the TCP connection and then says
  // nothing. Recovery from a dead broker is the case this path exists for, so it must not be
  // the case that resets the node.
  s_mqtt.setSocketTimeout(AQUA_MQTT_SOCKET_TIMEOUT_S);

  // PubSubClient defaults to 256 bytes, which would silently truncate every real envelope:
  // the payload *is* the envelope. Rejecting an undersized buffer at boot is the difference
  // between a build error and a node that publishes nothing and reports nothing.
  if (AQUA_MQTT_BUFFER_BYTES < aqua::AQUA_ENVELOPE_MAX_BYTES) {
    Logln("[AQUA] FATAL: AQUA_MQTT_BUFFER_BYTES < AQUA_ENVELOPE_MAX_BYTES");
    return false;
  }
  s_mqtt.setBufferSize(AQUA_MQTT_BUFFER_BYTES);
  s_configured = true;
  Logf("[AQUA] mqtt %s:%d as %s\n",
       (s_brokerIp != INADDR_NONE && s_brokerIp != IPAddress(0, 0, 0, 0)) ? s_brokerIp.toString().c_str() : AQUA_MQTT_HOST,
       AQUA_MQTT_PORT, AQUA_MQTT_CLIENT_ID);
  return true;
}

/**
 * Broadcast the DS3231 time, retained, so the station can find the clock without NTP.
 *
 * Tier 3 of `docs/TIME-SYNC.md`. The gateway publishes this same topic from its own clock, and
 * the two are deliberately the same shape: the payload names the clock that produced it, so a
 * consumer can tell the Pi's post-reboot guess from a battery-backed reading. The Pi's clock is
 * the one that is worthless exactly when this matters.
 *
 * Withheld until the reading is plausible. A retained message from an unanchored clock would
 * outlive the node and hand a wrong time to every subscriber that arrives afterwards.
 *
 * Published with an explicit length: `publish(topic, const uint8_t*, length, retained)` is the
 * four-argument overload, and the argument count is what forces it.
 */
void publishTimeIfDue(uint32_t nowMs) {
  if (s_lastRtcMs < aqua::kPlausibleEpochS * 1000ULL) return;
  if (static_cast<int32_t>(nowMs - s_timePublishedAtMs) < 0) return;
  if ((uint32_t)(nowMs - s_timePublishedAtMs) < kTimeBroadcastIntervalMs) return;
  s_timePublishedAtMs = nowMs;

  char payload[96];
  const int written = snprintf(payload, sizeof(payload),
                               "{\"t_dev_ms\":%llu,\"t_src\":\"rtc\",\"uncertainty_ms\":%u}",
                               (unsigned long long)s_lastRtcMs, kTimeUncertaintyMs);
  if (written <= 0 || (size_t)written >= sizeof(payload)) return;

  s_mqtt.publish(aqua::AQUA_TIME_TOPIC, (const uint8_t*)payload, (size_t)written,
                 /*retained=*/true);
}

void aquaLinkLoop() {
  // Lazy init: the node reaches the network through several paths (blocking connect, async
  // connect, the 30 s stack-reset in wifiMaintain, and auto-reconnect after sleep). Rather than
  // instrument every one, this repairs itself the first time it is called with WiFi up.
  if (!s_configured) {
    if (WiFi.status() != WL_CONNECTED) return;
    if (!aquaLinkBegin()) return;
  }

  if (!s_mqtt.connected()) {
    if (s_everConnected) {
      // Distinguishing "the broker dropped us" from "we never came up" matters: the first is a
      // network or keepalive problem, the second is a configuration one, and the log looked
      // identical for both. The uptime says whether this was a timeout or an immediate close.
      Logf("[AQUA] mqtt lost (state %d) after %lums\n", s_mqtt.state(),
           (unsigned long)(millis() - s_connectedAtMs));
      s_everConnected = false;
      s_resolveFailures = 0;
    }
    const uint32_t now = millis();
    // `(int32_t)(a - b) >= 0` rather than `now >= s_nextAttemptMs`, so the comparison survives
    // the millis() rollover at ~49 days. A node runs for months.
    if (static_cast<int32_t>(now - s_nextAttemptMs) >= 0) {
      if (connectOnce()) {
        s_nextAttemptMs = now;
        s_everConnected = true;
        s_connectedAtMs = now;
        s_resolveFailures = 0;
      } else {
        s_nextAttemptMs = now + AQUA_MQTT_RETRY_MS;
        Logf("[AQUA] mqtt connect failed (state %d), retry in %lums\n", s_mqtt.state(),
             (unsigned long)AQUA_MQTT_RETRY_MS);
        if (++s_resolveFailures >= 3) {
          s_brokerIp = INADDR_NONE;
          s_resolveFailures = 0;
        }
      }
    }
    return;
  }

  s_mqtt.loop();

  // Tier 3 of the time chain: refresh the retained DS3231 broadcast for the station.
  publishTimeIfDue(millis());

  if (!s_presencePublished) {
    // Published after the first successful loop() so the retained time broadcast is already in
    // flight — the node wants the clock before it wants to be seen.
    publishPresence(true, "boot");
    s_presencePublished = true;
  }
}

bool aquaLinkConnected() { return s_configured && s_mqtt.connected(); }

void aquaLinkQueue(uint32_t seconds2000, uint16_t msRemainder, int16_t* readings,
                   int16_t rtcTempCenti, uint32_t status) {
  if (s_queued >= aqua::AQUA_ENVELOPE_BATCH_LIMIT) {
    // The caller's flush cadence should never let this happen. Dropping the *oldest* would
    // silently lose the start of a window; dropping the newest loses one sample and is visible
    // as a sequence gap. So this refuses and the caller retries after a flush.
    return;
  }

  Queued& entry = s_batch[s_queued];
  // `seconds2000` is the node's LOCAL clock: `syncTimeNtp` stores `utcNow - LOCAL_TZ_OFFSET_SEC`
  // into the RTC, which is where this value comes from. Schema v2 defines `t_dev_ms` as a UTC
  // epoch, so the offset has to be added back -- exactly as the legacy `rtc_timestamp_ms` did.
  //
  // Omitting it does not look like a bug. Every value is right, the envelope parses, the
  // gateway stores it and the rollups fill up; the samples are simply filed five hours in the
  // past, so every "last hour" query returns nothing and the dashboard stays empty while the
  // data sits on disk. Five hours is also small enough to look like a timezone display setting
  // rather than a defect, which is what makes it worth a comment.
  entry.unixMs =
      (static_cast<uint64_t>(seconds2000) + UNIX_OFFSET_2000 + LOCAL_TZ_OFFSET_SEC) * 1000ULL +
      (uint64_t)msRemainder;
  for (size_t i = 0; i < MAX_SENSOR_READINGS; ++i) entry.readings[i] = readings[i];
  entry.rtcTempCenti = rtcTempCenti;
  entry.status = status;
  // Remember the newest authoritative reading: this is the tier-3 time source for the whole
  // station, and the transport is already handed it on every queue.
  s_lastRtcMs = entry.unixMs;
  ++s_queued;
}

/**
 * Record which experiment subsequent samples belong to. Pass null or "" to clear.
 *
 * Called by the transport shim on every queue, so it tracks the node's live state rather
 * than being wired to the command handler directly.
 */
void aquaLinkSetExperiment(const char* experimentId) {
  if (experimentId == nullptr || experimentId[0] == '\0') {
    s_experimentId[0] = '\0';
    return;
  }
  // Bounded: the id arrives over the wire, and a truncated id is still a usable grouping
  // key whereas an unbounded copy is a buffer overflow.
  snprintf(s_experimentId, sizeof(s_experimentId), "%s", experimentId);
}

bool aquaLinkFlush() {
  if (s_queued == 0) return true;
  if (!s_mqtt.connected()) return false;  // caller keeps the batch and retries

  ++s_seq;  // one number per envelope, never reused
  // Null rather than an empty string: the format and the gateway both treat null as "no
  // experiment", and an empty string would create a nameless experiment to group by.
  s_builder.setExperiment(s_experimentId[0] != '\0' ? s_experimentId : nullptr);
  if (!s_builder.begin(s_seq)) {
    Logf("[AQUA] flush: begin() refused seq %lu\n", (unsigned long)s_seq);
    ++s_dropped;
    s_queued = 0;
    return false;
  }

  aqua::FieldValue fields[13];
  size_t fieldCount = 0;
  for (size_t i = 0; i < s_queued; ++i) {
    buildFields(s_batch[i], fields, fieldCount);
    if (!s_builder.addSample(fields, fieldCount)) {
      // A truncated envelope is invalid JSON, so the gateway would reject the whole batch with
      // no way to say which node produced it. Discard it and let the sequence gap report it.
      Logf("[AQUA] flush: envelope overflow at sample %u (%u fields)\n", (unsigned)i,
           (unsigned)fieldCount);
      ++s_dropped;
      s_queued = 0;
      return false;
    }
  }

  const uint64_t unixS = s_batch[s_queued - 1].unixMs / 1000ULL;
  const size_t length = s_builder.finish(s_batch[s_queued - 1].unixMs, timeSourceFor(unixS), -70);
  if (length == 0) {
    Logf("[AQUA] flush: finish() produced 0 bytes (%u samples, %u fields each)\n",
         (unsigned)s_queued, (unsigned)fieldCount);
    ++s_dropped;
    s_queued = 0;
    return false;
  }

  char topic[48];
  if (!aqua::telemetryTopic(aqua::Role::OdSensor, topic, sizeof(topic))) {
    Logln("[AQUA] flush: telemetryTopic() failed");
    ++s_dropped;
    s_queued = 0;
    return false;
  }

  if (!s_mqtt.publish(topic, reinterpret_cast<const uint8_t*>(s_buffer), length, false)) {
    // Queue retained: a broker outage costs latency, not data. The sequence number is spent, so
    // the eventual gap is the honest record of what was lost if the batch is later overwritten.
    // Logged because a refused publish is the single most likely cause of an empty dashboard,
    // and PubSubClient fails it for several unrelated reasons.
    Logf("[AQUA] flush: publish refused (%u bytes to %s, state %d)\n", (unsigned)length, topic,
         s_mqtt.state());
    return false;
  }

  Logf("[AQUA] flush ok: %u bytes to %s\n", (unsigned)length, topic);
  s_queued = 0;
  return true;
}

size_t aquaLinkQueued() { return s_queued; }
uint32_t aquaLinkDropped() { return s_dropped; }
bool aquaLinkPresencePublished() { return s_presencePublished; }

bool aquaLinkAck(const char* cmdId, bool success) {
  if (!s_mqtt.connected() || cmdId == nullptr || cmdId[0] == '\0') return false;

  char topic[48];
  if (!aqua::ackTopic(aqua::Role::OdSensor, topic, sizeof(topic))) return false;

  // `result` is the vocabulary the gateway maps to an event type: applied / rejected / failed /
  // expired. Anything else becomes a protocol_error, which is the right outcome for an unknown
  // word rather than silently treating it as success.
  char payload[192];
  snprintf(payload, sizeof(payload),
           "{\"cmd_id\":\"%s\",\"result\":\"%s\",\"node_id\":\"%s\",\"t_dev_ms\":%llu}",
           cmdId, success ? "applied" : "failed", aqua::nodeId(aqua::Role::OdSensor),
           (unsigned long long)((uint64_t)time(nullptr) * 1000ULL));
  return s_mqtt.publish(topic, payload, false);
}

#endif  // WIFI_MODULE && AQUA_TRANSPORT_MQTT
