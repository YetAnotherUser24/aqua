/**
 * @file test_protocol.cpp
 * @brief Host conformance test for the schema v2 envelope builder.
 *
 * Compiles the **production** serialiser (`aqua_protocol.cpp`, unmodified) with plain g++ and
 * no Arduino, no ArduinoJson, and no PlatformIO — the same approach `test/host/` already uses
 * for `control_engine.cpp`.
 *
 * Two layers of checking, because they catch different things:
 *
 *   1. **Here** — properties the C++ side owns: overflow behaviour, sequence allocation,
 *      NaN handling, topic construction, and the byte budget. A JSON parser cannot tell you
 *      that a too-small buffer silently truncated.
 *   2. **`verify_against_gateway.py`** — feeds everything this emits to the real
 *      `aqua_gateway.mqtt.envelope.parse_envelope` and cross-checks field names against
 *      `roles.yaml`. A hand-written "valid JSON" check would only prove the JSON is
 *      well-formed; what actually matters is that the *gateway* accepts it.
 *
 * Build:
 *   g++ -std=c++17 -I firmware/aqua_protocol \
 *       firmware/aqua_protocol/test/test_protocol.cpp \
 *       firmware/aqua_protocol/aqua_protocol.cpp -o /tmp/test_protocol
 *   /tmp/test_protocol /tmp/envelopes.jsonl
 */

#include "aqua_protocol.h"

#include <stdio.h>
#include <string.h>

#include <cmath>
#include <cstdint>
#include <string>

static int g_failures = 0;
static int g_checks = 0;

#define CHECK(condition)                                                     \
  do {                                                                       \
    ++g_checks;                                                              \
    if (!(condition)) {                                                      \
      ++g_failures;                                                          \
      std::printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #condition);     \
    }                                                                        \
  } while (0)

#define CHECK_STR_EQ(actual, expected)                                                  \
  do {                                                                                  \
    ++g_checks;                                                                         \
    const std::string a_((actual) ? (actual) : "<null>");                              \
    const std::string e_((expected) ? (expected) : "<null>");                          \
    if (a_ != e_) {                                                                      \
      ++g_failures;                                                                      \
      std::printf("  FAIL %s:%d  got \"%s\", want \"%s\"\n", __FILE__, __LINE__,        \
                  a_.c_str(), e_.c_str());                                               \
    }                                                                                    \
  } while (0)

// ---------------------------------------------------------------------------
// Sample tables — the field set each node publishes, mirroring roles.yaml
// ---------------------------------------------------------------------------

using aqua::FieldValue;
namespace f = aqua::aerator_fields;
namespace odf = aqua::od_sensor_fields;
namespace mf = aqua::mixer_fields;
namespace pf = aqua::pump_fields;

static const FieldValue* aeratorSample(bool running, double rpm) {
  static FieldValue fields[14];
  // Field 0 is the per-sample instant, exactly as `buildFields()` on the node emits it. It is
  // an int64 with an 8-char name, so it costs ~20 bytes per sample — omitting it here made the
  // byte budget read ~6500 B for a 25-sample batch when the wire saw ~7100, and the difference
  // is what put the real payload over the ESP32 send buffer while the test stayed green.
  fields[0] = FieldValue::integer64(aqua::AQUA_SAMPLE_TIME_FIELD, 1791547200000LL);
  fields[1] = FieldValue::num(f::kRpm, rpm, 2);
  fields[2] = FieldValue::num(f::kRpmTarget, running ? 600.0 : 0.0, 2);
  fields[3] = FieldValue::num(f::kVbusV, 23.84, 2);
  fields[4] = FieldValue::num(f::kIbusA, running ? 3.42 : 0.15, 2);
  fields[5] = FieldValue::num(f::kPowerW, running ? 81.4 : 3.6, 2);
  fields[6] = FieldValue::num(f::kTorqueNm, running ? 0.213 : 0.0, 3);
  fields[7] = FieldValue::num(f::kIqA, running ? 2.94 : 0.02, 2);
  fields[8] = FieldValue::flag(f::kRunning, running);
  fields[9] = FieldValue::integer(f::kStatus, 0);
  fields[10] = FieldValue::text(f::kCtlMode, f::kModePid);
  fields[11] = FieldValue::flag(f::kCtlFailsafe, false);
  fields[12] = FieldValue::num(f::kPeerDoMgL, 7.874, 3);
  fields[13] = FieldValue::num(f::kPeerTempC, 23.05, 2);
  return fields;
}

static const FieldValue* odSample(float doVal) {
  static FieldValue fields[12];
  fields[0] = FieldValue::num(odf::kDoMgL, doVal, 3);
  fields[1] = FieldValue::integer(odf::kDoRaw, (long)std::lround(doVal * 1000.0f));
  fields[2] = FieldValue::num(odf::kSatPct, 90.5, 2);
  fields[3] = FieldValue::integer(odf::kSatRaw, 905);
  fields[4] = FieldValue::num(odf::kTempC, 24.08, 2);
  fields[5] = FieldValue::integer(odf::kTempRaw, 2408);
  fields[6] = FieldValue::integer(odf::kAux1Raw, 120);
  fields[7] = FieldValue::integer(odf::kAux2Raw, 0);
  fields[8] = FieldValue::num(odf::kBatteryV, 3.95, 2);
  fields[9] = FieldValue::num(odf::kRtcTempC, 24.50, 2);
  fields[10] = FieldValue::integer(odf::kRtcHealth, 1);
  fields[11] = FieldValue::integer(odf::kDevStatus, 0);
  return fields;
}

static const FieldValue* mixerSample(bool running) {
  static FieldValue fields[8];
  fields[0] = FieldValue::num(mf::kRpm, running ? 780.0 : 0.0, 1);
  fields[1] = FieldValue::num(mf::kRpmTarget, running ? 780.0 : 0.0, 1);
  fields[2] = FieldValue::num(mf::kRadS, running ? 81.681 : 0.0, 3);
  fields[3] = FieldValue::num(mf::kRadSTarget, running ? 81.681 : 0.0, 3);
  fields[4] = FieldValue::num(mf::kDutyPct, running ? 62.0 : 0.0, 2);
  fields[5] = FieldValue::flag(mf::kRunning, running);
  fields[6] = FieldValue::flag(mf::kStall, false);
  fields[7] = FieldValue::integer(mf::kStatus, 0);
  return fields;
}

static const FieldValue* pumpSample(const char* mode, double dosed) {
  static FieldValue fields[7];
  fields[0] = FieldValue::num(pf::kDosedMl, dosed, 2);
  fields[1] = FieldValue::num(pf::kTargetMl, 25.0, 2);
  fields[2] = FieldValue::num(pf::kRpm, 42.0, 2);
  fields[3] = FieldValue::text(pf::kMode, mode);
  fields[4] = FieldValue::flag(pf::kDoseCompleted, false);
  fields[5] = FieldValue::flag(pf::kDoseTimedOut, false);
  fields[6] = FieldValue::integer(pf::kStatus, 0);
  return fields;
}

// ---------------------------------------------------------------------------
// Helper: build one envelope and hand it back as a std::string
// ---------------------------------------------------------------------------

static std::string buildOne(aqua::Role role, const FieldValue* sample, size_t fieldCount,
                            uint64_t tDevMs, aqua::TimeSource source, int rssi) {
  char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
  aqua::EnvelopeBuilder env(role, 0xA1B2C3D4u, buffer, sizeof(buffer));
  env.begin(1);
  env.addSample(sample, fieldCount);
  const size_t len = env.finish(tDevMs, source, rssi);
  if (len == 0) return std::string();
  return std::string(buffer, len);
}

/**
 * The per-boot id must reach the wire even though the builder is constructed before the
 * RNG is up (every node passes a placeholder, then learns its real id in `aquaLinkBegin`).
 *
 * Regression: the setter did not exist, so the placeholder 0 was what every node published.
 * That defeats the `(node_id, boot_id, seq)` idempotency key — after a reboot `seq`
 * restarts at 1 while the gateway still holds the previous boot's high-water mark, so a
 * clean restart is logged as an `out_of_order_batch`.
 */
static void testBootIdSetter() {
  std::printf("boot id\n");

  char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
  aqua::EnvelopeBuilder env(aqua::Role::OdSensor, 0u, buffer, sizeof(buffer));
  env.setBootId(0xBEEF1234u);
  env.begin(1);
  env.addSample(odSample(7.874f), 12);
  const size_t len = env.finish(1791547200000ULL, aqua::TimeSource::Rtc, -61);
  CHECK(len > 0);
  const std::string text(buffer, len);
  CHECK(text.find("\"boot_id\":3203338804") != std::string::npos);  // 0xBEEF1234
  CHECK(text.find("\"boot_id\":0,") == std::string::npos);
}

/** True when every brace/bracket is matched outside of string literals. */
static bool structurallyBalanced(const std::string& text) {
  int depth = 0;
  bool inString = false;
  bool escaped = false;
  for (char c : text) {
    if (escaped) {
      escaped = false;
      continue;
    }
    if (inString) {
      if (c == '\\') {
        escaped = true;
      } else if (c == '"') {
        inString = false;
      }
      continue;
    }
    if (c == '"') {
      inString = true;
    } else if (c == '{' || c == '[') {
      ++depth;
    } else if (c == '}' || c == ']') {
      --depth;
      if (depth < 0) return false;
    }
  }
  return depth == 0 && !inString;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

static void testRegistryConstants() {
  std::printf("registry constants\n");
  CHECK_STR_EQ(aqua::roleName(aqua::Role::OdSensor), "od_sensor");
  CHECK_STR_EQ(aqua::roleName(aqua::Role::Aerator), "aerator");
  CHECK_STR_EQ(aqua::roleName(aqua::Role::Mixer), "mixer");
  CHECK_STR_EQ(aqua::roleName(aqua::Role::Pump), "pump");
  CHECK_STR_EQ(aqua::nodeId(aqua::Role::OdSensor), "a0000000-0000-0000-0000-000000000001");
  CHECK_STR_EQ(aqua::nodeId(aqua::Role::Aerator), "b0000000-0000-0000-0000-000000000002");
  CHECK_STR_EQ(aqua::nodeId(aqua::Role::Mixer), "c0000000-0000-0000-0000-000000000003");
  CHECK_STR_EQ(aqua::nodeId(aqua::Role::Pump), "d0000000-0000-0000-0000-000000000004");
  CHECK_STR_EQ(aqua::timeSourceName(aqua::TimeSource::Ntp), "ntp");
  CHECK_STR_EQ(aqua::timeSourceName(aqua::TimeSource::EspNow), "espnow");
  CHECK_STR_EQ(aqua::timeSourceName(aqua::TimeSource::Rtc), "rtc");
  CHECK_STR_EQ(aqua::timeSourceName(aqua::TimeSource::Monotonic), "monotonic");
}

static void testTopics() {
  std::printf("topic construction\n");
  char buffer[64];

  CHECK(aqua::telemetryTopic(aqua::Role::Aerator, buffer, sizeof(buffer)));
  CHECK_STR_EQ(buffer, "aqua/tlm/aerator");
  CHECK(aqua::telemetryTopic(aqua::Role::OdSensor, buffer, sizeof(buffer)));
  CHECK_STR_EQ(buffer, "aqua/tlm/od_sensor");

  CHECK(aqua::commandTopic(aqua::Role::Pump, buffer, sizeof(buffer)));
  CHECK_STR_EQ(buffer, "aqua/cmd/pump");
  CHECK(aqua::ackTopic(aqua::Role::Mixer, buffer, sizeof(buffer)));
  CHECK_STR_EQ(buffer, "aqua/ack/mixer");
  CHECK(aqua::eventsTopic(aqua::Role::Aerator, buffer, sizeof(buffer)));
  CHECK_STR_EQ(buffer, "aqua/event/aerator");

  CHECK(aqua::presenceTopic(aqua::Role::Aerator, buffer, sizeof(buffer)));
  CHECK_STR_EQ(buffer, "aqua/presence/b0000000-0000-0000-0000-000000000002");

  // A buffer one byte short of "aqua/tlm/aerator" (16 chars, so 17 with the terminator) must
  // fail, not truncate: silently publishing on "aqua/tlm/aerat" would look like a node that
  // is simply not reporting.
  char tight[16];
  CHECK(!aqua::telemetryTopic(aqua::Role::Aerator, tight, sizeof(tight)));
  CHECK_STR_EQ(tight, "");

  // A buffer exactly large enough must succeed.
  char exact[17];
  CHECK(aqua::telemetryTopic(aqua::Role::Aerator, exact, sizeof(exact)));
  CHECK_STR_EQ(exact, "aqua/tlm/aerator");
}

static void testEnvelopeShape() {
  std::printf("envelope shape\n");

  const std::string od = buildOne(aqua::Role::OdSensor, odSample(7.874f), 12, 1791547200000ULL,
                                  aqua::TimeSource::Rtc, -61);
  CHECK(!od.empty());
  CHECK(structurallyBalanced(od));
  CHECK(od.rfind("{\"v\":2,", 0) == 0);
  CHECK(od.find("\"role\":\"od_sensor\"") != std::string::npos);
  CHECK(od.find("\"node_id\":\"a0000000-0000-0000-0000-000000000001\"") != std::string::npos);
  CHECK(od.find("\"exp\":null") != std::string::npos);
  CHECK(od.find("\"t_dev_ms\":1791547200000") != std::string::npos);
  CHECK(od.find("\"t_src\":\"rtc\"") != std::string::npos);
  CHECK(od.find("\"rssi\":-61") != std::string::npos);
  CHECK(od.find("\"samples\":[") != std::string::npos);

  // Precision must follow the registry: od_do_mg_l is 3 dp, od_temp_c is 2 dp.
  CHECK(od.find("\"od_do_mg_l\":7.874") != std::string::npos);
  CHECK(od.find("\"od_temp_c\":24.08") != std::string::npos);
  // Integer fields carry no decimal point.
  CHECK(od.find("\"od_temp_raw\":2408") != std::string::npos);
  CHECK(od.find("\"od_temp_raw\":2408.") == std::string::npos);

  const std::string aer = buildOne(aqua::Role::Aerator, aeratorSample(true, 600.0), 14,
                                   1791547200000ULL, aqua::TimeSource::EspNow, -62);
  CHECK(!aer.empty());
  CHECK(structurallyBalanced(aer));
  CHECK(aer.find("\"drv_running\":true") != std::string::npos);
  CHECK(aer.find("\"ctl_mode\":\"pid\"") != std::string::npos);
  CHECK(aer.find("\"ctl_failsafe\":false") != std::string::npos);
  CHECK(aer.find("\"t_src\":\"espnow\"") != std::string::npos);

  const std::string pump = buildOne(aqua::Role::Pump, pumpSample(pf::kModeDose, 25.0), 7,
                                    1791547200000ULL, aqua::TimeSource::Ntp,
                                    aqua::EnvelopeBuilder::AQUA_RSSI_ABSENT);
  CHECK(!pump.empty());
  CHECK(pump.find("\"pump_mode\":\"dose\"") != std::string::npos);
  // A node with no radio metric must omit rssi rather than send 0, which would read as a
  // perfect signal.
  CHECK(pump.find("\"rssi\"") == std::string::npos);
}

static void testNonFiniteBecomesNull() {
  std::printf("non-finite handling\n");

  const double nan = std::nan("");
  const double inf = INFINITY;

  FieldValue fields[3];
  fields[0] = FieldValue::num(odf::kDoMgL, nan, 3);
  fields[1] = FieldValue::num(odf::kTempC, inf, 2);
  fields[2] = FieldValue::num(odf::kSatPct, 98.5, 2);

  const std::string env = buildOne(aqua::Role::OdSensor, fields, 3, 1791547200000ULL,
                                   aqua::TimeSource::Rtc, -60);
  CHECK(!env.empty());
  // `nan` / `inf` are not valid JSON and would make the gateway reject the whole batch.
  CHECK(env.find("nan") == std::string::npos);
  CHECK(env.find("inf") == std::string::npos);
  CHECK(env.find("\"od_do_mg_l\":null") != std::string::npos);
  CHECK(env.find("\"od_temp_c\":null") != std::string::npos);
  CHECK(env.find("\"od_sat_pct\":98.50") != std::string::npos);
}

static void testMultisampleAndSequence() {
  std::printf("multi-sample batch and sequence allocation\n");

  char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
  aqua::EnvelopeBuilder env(aqua::Role::Aerator, 0xDEADBEEFu, buffer, sizeof(buffer));
  CHECK(env.begin(42));

  CHECK(env.sequence() == 42);

  for (int i = 0; i < 5; ++i) {
    CHECK(env.addSample(aeratorSample(i % 2 == 0, 300.0 + i), 14));
  }
  CHECK(env.sampleCount() == 5);

  const size_t len = env.finish(1791547200000ULL, aqua::TimeSource::EspNow, -55);
  CHECK(len > 0);
  CHECK(env.length() == len);

  const std::string payload(buffer, len);
  CHECK(structurallyBalanced(payload));

  // Exactly four separators between five sample objects: five '{' after the array opener.
  size_t sampleOpens = 0;
  for (size_t i = 0; i < payload.size(); ++i) {
    if (payload[i] == '{') ++sampleOpens;
  }
  CHECK(sampleOpens == 6);  // 1 envelope + 5 samples

  // The sequence has to reach the wire: it is what the gateway deduplicates on, and a value
  // that never changes makes every message after the first look like a redelivery.
  CHECK(payload.find("\"seq\":42,") != std::string::npos);

  // Successive envelopes from the SAME builder.
  //
  // This is the only usage that matters in production: a node keeps one long-lived builder and
  // starts a new envelope on every flush. An earlier version of this test constructed a fresh
  // builder per case, which is why a bug survived where `begin()` refused the second envelope --
  // and a node that streams therefore published exactly one envelope and then went silent for as
  // long as it ran. Testing the class was not enough; the usage had to be tested.
  CHECK(env.begin(43));
  CHECK(env.sequence() == 43);
  CHECK(env.sampleCount() == 0);  // the previous envelope's samples must not carry over
  CHECK(!env.overflowed());       // nor its overflow state

  CHECK(env.addSample(aeratorSample(true, 111.0), 14));
  const size_t len2 = env.finish(1791547201000ULL, aqua::TimeSource::EspNow, -55);
  CHECK(len2 > 0);
  CHECK(len2 < len);  // one sample, not five

  const std::string payload2(buffer, len2);
  CHECK(structurallyBalanced(payload2));
  CHECK(payload2.find("\"seq\":43,") != std::string::npos);
  CHECK(payload2.find("\"seq\":42,") == std::string::npos);  // no stale header survived

  size_t opens2 = 0;
  for (size_t i = 0; i < payload2.size(); ++i) {
    if (payload2[i] == '{') ++opens2;
  }
  CHECK(opens2 == 2);  // 1 envelope + 1 sample, exactly
}

static void testEmptyBatchRefused() {
  std::printf("empty batch\n");
  char buffer[512];
  aqua::EnvelopeBuilder env(aqua::Role::Mixer, 1u, buffer, sizeof(buffer));
  CHECK(env.begin(1));
  // The gateway raises `empty_samples`; refuse locally so the failure is attributable.
  CHECK(env.finish(1791547200000ULL, aqua::TimeSource::Monotonic, -50) == 0);
}

static void testOverflowIsDetectable() {
  std::printf("overflow\n");

  // 64 bytes cannot hold one aerator sample, let alone a batch.
  char tiny[64];
  aqua::EnvelopeBuilder env(aqua::Role::Aerator, 7u, tiny, sizeof(tiny));
  env.begin(1);
  for (int i = 0; i < 25; ++i) {
    if (!env.addSample(aeratorSample(true, 600.0), 14)) break;
  }
  CHECK(env.overflowed());
  // The whole envelope is discarded: a truncated payload is invalid JSON, and the gateway
  // would reject the batch anyway — but with no way to tell which node produced it.
  CHECK(env.finish(1791547200000ULL, aqua::TimeSource::EspNow, -55) == 0);

  // A zero-capacity buffer must not write anything or crash.
  char none[1];
  aqua::EnvelopeBuilder degenerate(aqua::Role::Pump, 1u, none, 0);
  CHECK(!degenerate.begin(1));
  CHECK(degenerate.overflowed());
}

/** Measure how much room the widest role actually needs, and check the declared budget. */
static void testByteBudget() {
  std::printf("byte budget\n");

  char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
  aqua::EnvelopeBuilder env(aqua::Role::Aerator, 0x11223344u, buffer, sizeof(buffer));
  env.begin(1);

  size_t accepted = 0;
  for (size_t i = 0; i < aqua::AQUA_ENVELOPE_BATCH_LIMIT; ++i) {
    if (!env.addSample(aeratorSample(true, 600.0), 14)) break;
    ++accepted;
  }  const size_t len = env.finish(1791547200000ULL, aqua::TimeSource::EspNow, -60);

  const double perSample = accepted ? static_cast<double>(len) / static_cast<double>(accepted) : 0.0;
  std::printf("  buffer          : %zu bytes\n", aqua::AQUA_ENVELOPE_MAX_BYTES);
  std::printf("  batch limit     : %zu samples\n", aqua::AQUA_ENVELOPE_BATCH_LIMIT);
  std::printf("  accepted        : %zu samples\n", accepted);
  std::printf("  envelope size   : %zu bytes (%.1f bytes/sample)\n", len, perSample);
  std::printf("  headroom        : %zu bytes\n",
              aqua::AQUA_ENVELOPE_MAX_BYTES - len);

  // The declared limit must actually fit inside the declared buffer. When this fails, the
  // node drops every full batch — which on the dashboard is indistinguishable from the node
  // going offline, so it is worth an assertion rather than a comment.
  CHECK(accepted == aqua::AQUA_ENVELOPE_BATCH_LIMIT);
  CHECK(len > 0);
  CHECK(len < aqua::AQUA_ENVELOPE_MAX_BYTES);

  // The binding ceiling is the ESP32 TCP send buffer, not the envelope buffer above. PubSubClient
  // hands the whole topic+payload to `WiFiClient::write()` in one call and ignores a short count,
  // so a payload over `TCP_SND_BUF` is truncated mid-segment and the broker drops the client with
  // `malformed packet`. This is the check that was missing when a 25-sample aerator batch shipped
  // at ~6.9 KB and corrupted every flush: the buffer assertion above passed because 6.9 KB does
  // fit in 8 KB. Sized against the measured send buffer (~5744 B) minus topic and header.
  static constexpr size_t kTcpSendBudgetBytes = 5500;
  std::printf("  tcp send budget : %zu bytes\n", kTcpSendBudgetBytes);
  CHECK(len + 64 /* topic + MQTT header margin */ < kTcpSendBudgetBytes);
}

// ---------------------------------------------------------------------------
// Emission for the Python cross-check
// ---------------------------------------------------------------------------

static FILE* g_sink = nullptr;

static void emit(const char* label, aqua::Role role, const FieldValue* sample, size_t count,
                 uint64_t tDevMs, aqua::TimeSource source, int rssi) {
  if (g_sink == nullptr) return;
  char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
  aqua::EnvelopeBuilder env(role, 0x5EED0001u, buffer, sizeof(buffer));
  env.begin(1);
  env.addSample(sample, count);
  const size_t len = env.finish(tDevMs, source, rssi);
  if (len == 0) return;
  // One line per envelope: `label<TAB>{json}` so the verifier can report which case failed.
  std::fprintf(g_sink, "%s\t%s\n", label, std::string(buffer, len).c_str());
}

static void emitAll() {
  emit("od_sensor_rtc", aqua::Role::OdSensor, odSample(7.874f), 12, 1791547200000ULL,
       aqua::TimeSource::Rtc, -61);
  emit("od_sensor_monotonic_no_rssi", aqua::Role::OdSensor, odSample(3.100f), 12,
       1791547200000ULL, aqua::TimeSource::Monotonic,
       aqua::EnvelopeBuilder::AQUA_RSSI_ABSENT);
  emit("aerator_running", aqua::Role::Aerator, aeratorSample(true, 600.0), 14,
       1791547200000ULL, aqua::TimeSource::EspNow, -62);
  emit("aerator_stopped", aqua::Role::Aerator, aeratorSample(false, 0.0), 14,
       1791547200000ULL, aqua::TimeSource::EspNow, -62);
  emit("mixer_running", aqua::Role::Mixer, mixerSample(true), 8, 1791547200000ULL,
       aqua::TimeSource::Ntp, -48);
  emit("pump_idle", aqua::Role::Pump, pumpSample(pf::kModeIdle, 0.0), 7, 1791547200000ULL,
       aqua::TimeSource::Ntp, aqua::EnvelopeBuilder::AQUA_RSSI_ABSENT);
  emit("pump_dosing", aqua::Role::Pump, pumpSample(pf::kModeDose, 25.0), 7, 1791547200000ULL,
       aqua::TimeSource::Ntp, aqua::EnvelopeBuilder::AQUA_RSSI_ABSENT);

  // A multi-sample batch, to prove the array shape survives the parser.
  if (g_sink != nullptr) {
    char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
    aqua::EnvelopeBuilder env(aqua::Role::OdSensor, 0x5EED0002u, buffer, sizeof(buffer));
    env.begin(1);
    for (int i = 0; i < 8; ++i) {
      env.addSample(odSample(7.0f + 0.1f * i), 12);
    }
    const size_t len = env.finish(1791547200000ULL, aqua::TimeSource::Rtc, -61);
    if (len > 0) {
      std::fprintf(g_sink, "od_sensor_batch8\t%s\n", std::string(buffer, len).c_str());
    }
  }

  // An experiment-tagged envelope, since `exp` is the only escaped string on the wire.
  if (g_sink != nullptr) {
    char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
    aqua::EnvelopeBuilder env(aqua::Role::Aerator, 0x5EED0003u, buffer, sizeof(buffer));
    env.setExperiment("exp-2026-10-09-deox");
    env.begin(1);
    env.addSample(aeratorSample(true, 600.0), 14);
    const size_t len = env.finish(1791547200000ULL, aqua::TimeSource::EspNow, -62);
    if (len > 0) {
      std::fprintf(g_sink, "aerator_with_experiment\t%s\n", std::string(buffer, len).c_str());
    }
  }
}

/**
 * A batch spans time, so every sample carries its own instant.
 *
 * The failure being guarded against is quiet in both directions. With only the envelope
 * timestamp, five samples taken at 5 Hz share one millisecond and a reader plots them as a
 * single point. And because epoch milliseconds (~1.79e12) does not fit the 32-bit ``long``
 * that ``integer`` accepts, routing the value through the wrong factory would not crash or
 * warn on the device -- it would emit a plausible-looking number from 1987.
 *
 * Note the width half of this cannot fail on a 64-bit host, where ``long`` is already wide
 * enough; that check is asserted through an explicit narrowing below so the boundary is at
 * least stated rather than assumed.
 */
static void testPerSampleTimestamp() {
  std::printf("per-sample timestamp\n");

  const long long base = 1791568200423LL;

  // The instant cannot survive a 32-bit round trip. This is the boundary ``integer`` and
  // ``integer64`` sit on either side of, and the reason the field is not emitted with
  // ``integer`` -- on the device that narrowing is silent.
  const std::int32_t narrowed = static_cast<std::int32_t>(static_cast<std::uint32_t>(base));
  CHECK(static_cast<long long>(narrowed) != base);

  char buffer[aqua::AQUA_ENVELOPE_MAX_BYTES];
  aqua::EnvelopeBuilder env(aqua::Role::OdSensor, 0x11223344u, buffer, sizeof(buffer));
  CHECK(env.begin(9));

  static FieldValue fields[2];
  for (int i = 0; i < 5; ++i) {  // five samples: the 5 Hz case that collapsed to one point
    fields[0] = FieldValue::integer64(aqua::AQUA_SAMPLE_TIME_FIELD, base + i * 200);
    fields[1] = FieldValue::num(odf::kDoMgL, 7.5 + i * 0.1, 3);
    CHECK(env.addSample(fields, 2));
  }

  const size_t len = env.finish(base + 800, aqua::TimeSource::Rtc, -61);
  CHECK(len > 0);
  const std::string payload(buffer, len);
  CHECK(structurallyBalanced(payload));

  // Every sample keeps its own full-precision instant, with no decimal point.
  for (int i = 0; i < 5; ++i) {
    const std::string want = "\"t_dev_ms\":" + std::to_string(base + i * 200);
    CHECK(payload.find(want) != std::string::npos);
  }
  CHECK(payload.find("1791568200423.0") == std::string::npos);

  // The five timestamps are all distinct: this is exactly what the collapsed plot lost.
  std::size_t distinct = 0;
  for (int i = 0; i < 5; ++i) {
    const std::string want = "\"t_dev_ms\":" + std::to_string(base + i * 200);
    if (payload.find(want) != std::string::npos) ++distinct;
  }
  CHECK(distinct == 5);
}

// ---------------------------------------------------------------------------
// Clock rules
//
// These are pure functions in the library rather than per-node copies, and they are the rules
// that decide whether a command is refused and whether a timestamp is believed. Both have gone
// wrong before: the deadline check was silently disabled on all four nodes at once by an
// undersized JSON probe, which is exactly the kind of defect a shared, tested rule prevents.
// ---------------------------------------------------------------------------

static void testDeadlinePassed() {
  const long long deadline = 1'800'000'000'000LL;

  // The ordinary case, both sides of the boundary. A deadline that has just been reached has
  // not passed: the gateway issues the command *for* that moment.
  CHECK(!aqua::deadlinePassed(deadline, static_cast<uint64_t>(deadline)));
  CHECK(!aqua::deadlinePassed(deadline, static_cast<uint64_t>(deadline) - 1));
  CHECK(aqua::deadlinePassed(deadline, static_cast<uint64_t>(deadline) + 1));

  // Nothing to enforce. The gateway always stamps one, but a command shaped by hand or by an
  // older client may not, and a missing deadline must not be read as an expired one.
  CHECK(!aqua::deadlinePassed(0, 999'999'999'999'999ULL));
  CHECK(!aqua::deadlinePassed(-1, 999'999'999'999'999ULL));

  // No anchored clock, in the two forms a caller can produce. The important direction is that a
  // node knowing 1970 does not conclude that every command is stale: 1970 is less than every
  // real deadline, so the bare comparison would reject everything the node ever receives and turn
  // a clock fault into a node that ignores its operator.
  CHECK(!aqua::deadlinePassed(deadline, 0));
  CHECK(!aqua::deadlinePassed(deadline, 1));

  // The same guard with a *small* deadline, which is the only shape where it changes the answer:
  // here the unanchored clock reads higher than the deadline, so a rule that only compared the
  // two would report a stale command. No current caller produces this pair -- their deadlines are
  // always anchored -- so this pins the rule rather than a live defect, which is what makes it
  // worth stating: the next caller with a different clock source meets it first.
  CHECK(!aqua::deadlinePassed(1'000'000'000'000LL, 1'200'000'000'000LL));

  // A plausible but stale clock still enforces, which is the point of the check.
  CHECK(aqua::deadlinePassed(deadline - 60'000, static_cast<uint64_t>(deadline)));
}

static void testPlausibleEpochMs() {
  // 2020-01-01T00:00:00Z exactly, and one millisecond either side.
  CHECK(aqua::plausibleEpochMs(static_cast<long long>(aqua::kPlausibleEpochS) * 1000LL));
  CHECK(!aqua::plausibleEpochMs(static_cast<long long>(aqua::kPlausibleEpochS) * 1000LL - 1));

  // An unsynced clock reports 1970, and it must not be believed.
  CHECK(!aqua::plausibleEpochMs(0));
  CHECK(!aqua::plausibleEpochMs(1));

  // 2020-09-13 is a real date, so it is believed. This assertion is the drift itself: the pump
  // declared its own threshold as 1600000000 and so treated every reading before that date as
  // unanchored, while its comment said the pre-2020 case was the one being handled. A node
  // whose clock read 2020-05-01 was reported `monotonic` there and `ntp` everywhere else.
  CHECK(aqua::plausibleEpochMs(1'600'000'000'000LL));
  CHECK(aqua::plausibleEpochMs(1'791'601'806'522LL));
}

static void testExtrapolateEpochMs() {
  const uint64_t anchor = 1'791'601'806'522ULL;

  // A reading taken at the anchor is the anchor.
  CHECK(aqua::extrapolateEpochMs(anchor, 1000, 1000) == anchor);
  // And it advances with the uptime, one millisecond per millisecond.
  CHECK(aqua::extrapolateEpochMs(anchor, 1000, 6000) == anchor + 5000);
  CHECK(aqua::extrapolateEpochMs(anchor, 0, 200) == anchor + 200);

  // The wrap at 2^32. A node up for 49.7 days crosses it, and a signed difference here would
  // produce a negative age -- an extrapolated clock that jumps *backwards* by 49 days. The
  // subtraction has to stay unsigned, and this is the only place that says so.
  const uint32_t just_before = 4'294'967'295u;  // UINT32_MAX
  CHECK(aqua::extrapolateEpochMs(anchor, just_before, 5) == anchor + 6);

  // An anchor of 0 is used by the callers to mean "no broadcast", and they never reach here
  // with it; this pins the arithmetic rather than the policy.
  CHECK(aqua::extrapolateEpochMs(0, 0, 0) == 0);
}

int main(int argc, char** argv) {
  if (argc > 1) {
    g_sink = std::fopen(argv[1], "w");
    if (g_sink == nullptr) {
      std::printf("cannot open %s for writing\n", argv[1]);
      return 2;
    }
  }

  std::printf("aqua_protocol conformance test\n\n");

  testRegistryConstants();
  testTopics();
  testBootIdSetter();
  testEnvelopeShape();
  testNonFiniteBecomesNull();
  testMultisampleAndSequence();
  testPerSampleTimestamp();
  testEmptyBatchRefused();
  testOverflowIsDetectable();
  testByteBudget();
  testDeadlinePassed();
  testPlausibleEpochMs();
  testExtrapolateEpochMs();

  emitAll();
  if (g_sink != nullptr) {
    std::fclose(g_sink);
    std::printf("\nwrote envelopes to %s\n", argv[1]);
  }

  std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
