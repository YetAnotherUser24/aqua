/**
 * @file virtual_node.cpp
 * @brief Host-side virtual bench: four nodes and a plant, running the REAL firmware path.
 *
 * This is the host counterpart to the `*-virtual` PlatformIO environments. Those run the real
 * firmware on an ESP32 with a simulated peripheral; this runs the real *protocol* module and the
 * real *device models* on a workstation, so the whole chain can be exercised without hardware,
 * without flashing, and without a broker on the same machine.
 *
 * What is real here, and what is not:
 *
 *   real   `aqua_protocol.{h,cpp}` — the exact serialiser the nodes ship
 *   real   `motor_sim.cpp`, `sensor_sim.cpp`, `mixer_sim.cpp`, `pump_sim.cpp` — taken unmodified
 *          from the four firmware repositories, include-path only
 *   real   the envelope contract: this output is parsed by the production gateway
 *   fake   the plant. Coupling RPM to dissolved oxygen is a plausibility model, NOT an
 *          identified one. Its coefficients are labelled UNIDENTIFIED and are never evidence
 *          about the thesis bench.
 *
 * Following `VIRTUAL_NODES.md` principle 5, time is injected: the sims are driven by an explicit
 * millisecond tick rather than reading a clock internally, so a run is reproducible and a fault
 * schedule fires at the same simulated instant every time.
 *
 * ## Process interface
 *
 * stdout, one record per line, TAB separated:
 *   `TEL<TAB><topic><TAB><payload>`        telemetry envelope
 *   `ACK<TAB><topic><TAB><payload>`        command acknowledgement
 *   `PRESENCE<TAB><topic><TAB><payload>`   retained presence (also fed to the MQTT Last Will)
 *
 * stdin, one record per line:
 *   `CMD<TAB><role><TAB><json>`
 *
 * The transport split is deliberate. The node speaks the protocol; the bridge
 * (`tools/virtual-nodes.py`) owns MQTT. That keeps this binary dependency-free and lets the same
 * node be driven from a file for deterministic tests.
 *
 * Build: `./build.sh` in this directory.
 */

#include "aqua_protocol.h"

#include "mixer_sim.h"
#include "motor_sim.h"
#include "pump_sim.h"
#include "sensor_sim.h"

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <mutex>
#include <queue>
#include <string>
#include <thread>

namespace {

using aqua::FieldValue;
using aqua::Role;
using aqua::TimeSource;

namespace aer = aqua::aerator_fields;
namespace mix = aqua::mixer_fields;
namespace odf = aqua::od_sensor_fields;
namespace pmp = aqua::pump_fields;

constexpr int kRoleCount = 4;
constexpr double kPi = 3.14159265358979;

// Mixer command scaling. The real node takes an RPM target and runs a PID to a duty; this
// model has no PID, so the RPM is mapped linearly onto the duty the ESC actually sees.
// `kMixerMaxRpm` is `BLDC_MAX_RPM` from nodo-mezclador/include/uart_protocol.h -- the ESC
// ceiling, not the thruster's free-run speed, because the node clamps to it. The default is
// `MIXER_DEFAULT_RPM` from cloud_worker.cpp, applied to a bare start or a sub-floor target.
constexpr double kMixerMaxRpm = 1000.0;
constexpr double kMixerDefaultRpm = 600.0;

//: Millilitres delivered per revolution of the pump head. Mirrors `ML_PER_REV` in
//: nodo-bomba/src/config.h -- the real node converts encoder revolutions to volume with it, so
//: the simulator must use the same number or its dose timing is a fiction.
constexpr double kPumpMlPerRev = 1.0;

bool roleFromName(const std::string& name, Role& out) {
  for (int i = 0; i < kRoleCount; ++i) {
    const Role candidate = static_cast<Role>(i);
    if (name == aqua::roleName(candidate)) {
      out = candidate;
      return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Plant — plausibility only
// ---------------------------------------------------------------------------

/**
 * Closed-loop pond model.
 *
 * Everything here is **UNIDENTIFIED**. It exists so the aerator has a visible effect on the
 * dissolved-oxygen trace and so the control panel does something observable. It is not the
 * thesis plant, its parameters were not fitted to any data, and no number derived from it may
 * appear in a results chapter — the same warning the JS bench carries in
 * `scripts/virtual-bench/models/process.js`. The coefficients here are independently invented
 * and are not comparable with those.
 */
struct Plant {
  // UNIDENTIFIED: chosen to give a response on the order of a minute, not measured.
  static constexpr double kRespirationMgLPerS = 0.0012;
  static constexpr double kKlaFullScalePerS = 0.0035;   // at MAX_MOTOR_RPM
  static constexpr double kMixerKlaFraction = 0.25;
  static constexpr double kMaxRpm = 600.0;
  static constexpr double kSulfiteMgPerMl = 100.0;      // UNIDENTIFIED stock concentration
  static constexpr double kOxygenPerSulfiteMg = 1.0 / 7.878;  // real stoichiometry
  static constexpr double kVolumeL = 87.5;              // the bench's active volume
  double doMgL = 7.40;
  double tempC = 24.10;
  double sulfiteMgL = 0.0;

  /**
   * Operator-set DO, held until cleared. `NaN` means "not overridden".
   *
   * A recipe's first phase is often a *physical precondition* -- `planta_2_step.p2_precond`
   * gates on `DO < 3.5 mg/L`. Nothing could establish that on the bench: the plant models DO
   * physically, so reaching 3.5 by respiration alone takes ~42 minutes from 7.4, and the fast
   * path (dosing sulfite) needs the pump and mixer running. Any recipe whose entry condition is
   * a precondition was therefore untestable, which is a gap in this simulator rather than a
   * property of the station.
   *
   * Setting a value the operator dictates is what MANUAL_OVERRIDE is for, so the override wins
   * over the physics instead of being immediately pulled back to equilibrium -- an override that
   * drifts is not an override. `set_do` with no value clears it and returns the plant to its own
   * dynamics.
   */
  double doOverrideMgL = std::numeric_limits<double>::quiet_NaN();

  bool hasDoOverride() const { return !std::isnan(doOverrideMgL); }

  void clearDoOverride() { doOverrideMgL = std::numeric_limits<double>::quiet_NaN(); }

  /** Freshwater saturation at 1 atm (APHA). Gives ~8.3 mg/L at 24 degC, matching the bench. */
  static double saturationMgL(double tempC) {
    const double t = tempC + 273.15;
    const double lnCs = -139.34411 + (1.575701e5 / t) - (6.642308e7 / (t * t)) +
                        (1.243800e10 / (t * t * t)) - (8.621949e11 / (t * t * t * t));
    return std::exp(lnCs);
  }

  void tick(double dtSeconds, double aeratorRpm, bool mixerRunning, double dosedMl) {
    if (dosedMl > 0.0) sulfiteMgL += (dosedMl * kSulfiteMgPerMl) / kVolumeL;

    const double kla = kKlaFullScalePerS * (aeratorRpm / kMaxRpm) +
                       (mixerRunning ? kMixerKlaFraction * kKlaFullScalePerS : 0.0);
    const double cs = saturationMgL(tempC);
    double dDo = kla * (cs - doMgL) - kRespirationMgLPerS;

    // Sulfite consumes oxygen stoichiometrically until exhausted.
    //
    // The cap exists so a large dose cannot drive DO negative in a single step, but it has to
    // be a *rate* to be dt-independent: it was written as `doMgL * 0.02 + 0.0005` and then
    // added to `dDo`, which is multiplied by `dtSeconds` below -- while the sulfite ledger was
    // drained by the same un-scaled number. The two sides therefore disagreed by 1/dt, and the
    // ledger won: at 5 Hz the whole 12.5 mL dose was consumed in ~13 ticks (2.6 s), removing
    // 0.34 mg/L of the 1.81 mg/L its stoichiometry calls for. `planta_1_deox.p1_deox` waits for
    // `DO < 3.0` and never got near it, so the deox protocol looked like the sulfite did
    // nothing. Scaling both the cap and the drain by `dtSeconds` keeps them consistent and
    // makes the result independent of the tick rate.
    if (sulfiteMgL > 0.0 && dtSeconds > 0.0) {
      const double demandPerS = sulfiteMgL * kOxygenPerSulfiteMg;  // mg/L per second
      const double capPerS = doMgL * 0.02 + 0.0005;                // mg/L per second
      const double consumedPerS = std::fmin(demandPerS, capPerS);
      dDo -= consumedPerS;
      sulfiteMgL = std::fmax(0.0, sulfiteMgL - (consumedPerS * dtSeconds) / kOxygenPerSulfiteMg);
    }

    doMgL += dDo * dtSeconds;
    if (doMgL < 0.2) doMgL = 0.2;
    if (doMgL > cs) doMgL = cs;

    // Applied last, so the override is what a reader sees regardless of what the physics did
    // this tick. Still clamped to the sensor's physical range.
    if (hasDoOverride()) {
      double v = doOverrideMgL;
      if (v < 0.0) v = 0.0;
      if (v > cs) v = cs;
      doMgL = v;
    }

    tempC += std::sin(dtSeconds * 0.05) * 0.0004;
  }
};

// ---------------------------------------------------------------------------
// Command queue
// ---------------------------------------------------------------------------

struct Command {
  std::string role;
  std::string action;
  std::string body;  // raw JSON; the node parses only the scalars it needs
  std::string cmdId;
};

std::mutex g_queueMutex;
std::queue<Command> g_queue;

/**
 * Pull a scalar out of a small JSON object without a parser dependency.
 *
 * Deliberately crude. The node needs one or two scalars out of a command, and adding a JSON
 * library here would reintroduce exactly the version coupling `aqua_protocol.h` exists to avoid.
 * Returns the default when the key is absent, which is the right behaviour for a missing target.
 */
double jsonNumber(const std::string& json, const char* key, double defaultValue) {
  const std::string needle = std::string("\"") + key + "\"";
  const size_t at = json.find(needle);
  if (at == std::string::npos) return defaultValue;
  const size_t colon = json.find(':', at + needle.size());
  if (colon == std::string::npos) return defaultValue;
  return std::strtod(json.c_str() + colon + 1, nullptr);
}

std::string jsonString(const std::string& json, const char* key, const char* defaultValue) {
  const std::string needle = std::string("\"") + key + "\"";
  const size_t at = json.find(needle);
  if (at == std::string::npos) return defaultValue;
  const size_t colon = json.find(':', at + needle.size());
  if (colon == std::string::npos) return defaultValue;
  const size_t open = json.find('"', colon);
  if (open == std::string::npos) return defaultValue;
  const size_t close = json.find('"', open + 1);
  if (close == std::string::npos) return defaultValue;
  return json.substr(open + 1, close - open - 1);
}

void stdinReader() {
  char line[4096];
  while (std::fgets(line, sizeof(line), stdin) != nullptr) {
    std::string text(line);
    while (!text.empty() && (text.back() == '\n' || text.back() == '\r')) text.pop_back();
    if (text.rfind("CMD\t", 0) != 0) continue;

    const size_t firstTab = 3;
    const size_t secondTab = text.find('\t', firstTab + 1);
    if (secondTab == std::string::npos) continue;

    Command cmd;
    cmd.role = text.substr(firstTab + 1, secondTab - firstTab - 1);
    cmd.body = text.substr(secondTab + 1);
    cmd.action = jsonString(cmd.body, "action", "");
    cmd.cmdId = jsonString(cmd.body, "cmd_id", "");

    std::lock_guard<std::mutex> lock(g_queueMutex);
    g_queue.push(cmd);
  }
}

void emitLine(const char* kind, const char* topic, const char* payload, size_t length) {
  std::fputs(kind, stdout);
  std::fputc('\t', stdout);
  std::fputs(topic, stdout);
  std::fputc('\t', stdout);
  std::fwrite(payload, 1, length, stdout);
  std::fputc('\n', stdout);
  std::fflush(stdout);
}

uint64_t epochMs() {
  using namespace std::chrono;
  return static_cast<uint64_t>(
      duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count());
}

// ---------------------------------------------------------------------------
// One node's publishing state
// ---------------------------------------------------------------------------

/**
 * Sequence counter, batch buffer and drop counter for one role.
 *
 * `seq` feeds the gateway's `(node_id, boot_id, seq)` idempotency key and its gap detection, so
 * it must advance once per envelope and must not restart within a boot. A dropped envelope still
 * consumes a number: burning one is correct, because the gateway then reports a sequence gap,
 * which is the only signal that firmware is throwing payloads away.
 */
class Publisher {
 public:
  static constexpr size_t kSamplesPerBatch = 5;

  /**
   * Headroom for a role's own fields plus the per-sample timestamp prepended in \`add\`.
   *
   * The widest role declares 13, so 16 leaves room to add a measurement without touching this.
   * \`add\` checks the bound and drops the sample rather than overrunning the array, because a
   * stack smash here would be reported as a decode failure somewhere else entirely.
   */
  static constexpr size_t kMaxSampleFields = 16;

  Publisher(Role role, uint32_t bootId)
      : role_(role), bootId_(bootId), builder_(role, bootId, buffer_, sizeof(buffer_)) {}

  void setExperiment(const std::string& id) { experimentId_ = id; }

  /** Queue one sample, publishing when the batch fills. */
  void add(const FieldValue* fields, size_t count, uint64_t tDevMs, TimeSource source, int rssi) {
    if (queued_ == 0) {
      builder_.setExperiment(experimentId_.empty() ? nullptr : experimentId_.c_str());
      // One number per envelope, never reused: the gateway deduplicates on
      // (node_id, boot_id, seq), so a constant here would make every message after the first
      // look like a redelivery and silently discard it.
      builder_.begin(++sequence_);
    }

    // Stamp the sample with its own instant, here rather than in each role's field array.
    //
    // A batch spans time: at 5 Hz these five samples cover a second, but only the envelope
    // carried a timestamp, so the gateway filed all five under one millisecond. A chart drawn
    // from `/api/samples` then showed a fifth of the points it had been handed, which makes the
    // dashboard look broken while the firmware is behaving. Doing it centrally is the point:
    // the firmware needs one edit per role to achieve this, and the role that gets forgotten is
    // always the one nobody is looking at.
    if (count + 1 > kMaxSampleFields) {
      ++dropped_;
      reset();
      return;
    }
    FieldValue stamped[kMaxSampleFields];
    stamped[0] =
        FieldValue::integer64(aqua::AQUA_SAMPLE_TIME_FIELD, static_cast<long long>(tDevMs));
    for (size_t i = 0; i < count; ++i) stamped[i + 1] = fields[i];

    if (!builder_.addSample(stamped, count + 1)) {
      // Discard the whole envelope rather than publish a truncated one: invalid JSON makes the
      // gateway reject the batch anyway, but with no way to say which node produced it.
      ++dropped_;
      reset();
      return;
    }

    if (++queued_ >= kSamplesPerBatch) flush(tDevMs, source, rssi);
  }

  /**
   * Queue one sample and push it immediately, skipping the batch.
   *
   * For roles whose firmware sends one envelope per interval rather than accumulating: the
   * mixer and pump both push a single reading. Going through `add` instead made the two
   * intervals *multiply* -- a 20 s idle gate fed a 5-sample batch, so an envelope appeared only
   * every 100 s and a healthy idle node looked like a stalled one.
   */
  void addNow(const FieldValue* fields, size_t count, uint64_t tDevMs, TimeSource source,
              int rssi) {
    add(fields, count, tDevMs, source, rssi);
    flush(tDevMs, source, rssi);
  }

  void flush(uint64_t tDevMs, TimeSource source, int rssi) {
    if (queued_ == 0) return;

    // Emit **before** resetting. `reset()` constructs a fresh builder, whose constructor writes
    // `buffer_[0] = '\0'` — which would clobber the first byte of the envelope we just finished.
    // The result is a payload whose length is correct and whose opening brace is a NUL, so it
    // JSON-decodes as UTF-16 and fails with an error that points nowhere near this line.
    const size_t length = builder_.finish(tDevMs, source, rssi);
    if (length == 0) {
      ++dropped_;
    } else {
      char topic[48];
      if (aqua::telemetryTopic(role_, topic, sizeof(topic))) {
        emitLine("TEL", topic, buffer_, length);
      }
    }
    reset();
  }

  uint32_t sequence() const { return sequence_; }
  uint32_t dropped() const { return dropped_; }

 private:
  /** Start a fresh envelope in the same buffer. */
  void reset() {
    queued_ = 0;
    builder_ = aqua::EnvelopeBuilder(role_, bootId_, buffer_, sizeof(buffer_));
  }

  Role role_;
  uint32_t bootId_;
  std::string experimentId_;
  char buffer_[aqua::AQUA_ENVELOPE_MAX_BYTES];  // static storage: 8 KB is not stack material
  aqua::EnvelopeBuilder builder_;
  size_t queued_ = 0;
  uint32_t dropped_ = 0;
  uint32_t sequence_ = 0;
};

}  // namespace

// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
  double hz = 5.0;
  double durationS = 0.0;  // 0 = run until killed
  bool startMonitoring = true;

  const uint32_t bootId = static_cast<uint32_t>(epochMs() / 1000) & 0xFFFFu;

  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--hz") == 0 && i + 1 < argc) hz = std::atof(argv[++i]);
    else if (std::strcmp(argv[i], "--seconds") == 0 && i + 1 < argc) durationS = std::atof(argv[++i]);
    else if (std::strcmp(argv[i], "--idle") == 0) startMonitoring = false;
  }
  if (hz < 0.1) hz = 0.1;

  // ---- device models, unmodified from the firmware repositories -----------
  MotorSimSpec motorSpec;
  motorSpec.rpmMax = 600.0f;  // MAX_MOTOR_RPM, AGENTS.md rule 2
  MotorSim aerator(motorSpec);

  OdSimSpec odSpec;
  OdSim odSensor(odSpec);
  odSensor.powerOn();

  MixerSim mixer((MixerSimSpec()));
  PumpSim pump((PumpSimSpec()));

  Plant plant;

  // Command-driven state. The plant is the only thing that closes a loop; each node publishes
  // what its own peripheral reports, exactly as the firmware does.
  double aeratorTargetRpm = 0.0;
  bool aeratorRunning = false;
  bool aeratorEstop = false;
  // Closed-loop state. `aeratorMode` starts as "manual" so an untouched node behaves exactly
  // as it did before these existed -- the throttle path -- and only a `set_mode pid` switches
  // to regulating DO. The setpoint default mirrors `control_engine.h` (7.5 mg/L) so a node
  // that is put in PID mode without a setpoint behaves like the real one.
  std::string aeratorMode = "manual";
  double aeratorSetpointDo = 7.5;
  double aeratorKp = 0.10;
  double mixerTargetDuty = 0.0;
  double pumpTargetMl = 0.0;
  double pumpDosedMl = 0.0;
  // The pump model's revolution count at the previous tick. The dose is integrated from the
  // *delta*, so the head's own spin-up is included rather than assumed.
  double pumpRevsLastSeen = 0.0;
  std::string pumpMode = pmp::kModeIdle;
  bool monitoring = startMonitoring;
  // The sensor's sampling period, in seconds. 5.0 -- not 1.0 -- because that is what the real
  // node does: `nodo-sensor-od/src/main.cpp` defaults `experimentIntervalMs` to 5000 and
  // `roles.yaml` declares `expected_rate_hz: {experiment: 0.2}` for the role. Both say five
  // seconds, so a one-second default here made the simulator the only place in the system
  // claiming 1 Hz, and the gateway -- correctly measuring the node's own stamps -- reported a
  // rate no real node produces.
  double sensorIntervalS = 5.0;
  double sensorAccumulator = 5.0;
  // Publishing cadence for the two nodes that push one envelope per interval rather than
  // batching at a fixed sample rate. Real values, from each node's `config.h`:
  //   nodo-mezclador  TELEMETRY_INTERVAL_MS       = 3000 running, 20000 idle
  //   nodo-bomba      TELEMETRY_INTERVAL_MS       = 3000 running, 20000 idle
  // Both match `roles.yaml` (`experiment: 0.3333, idle: 0.05`). Publishing them at the loop
  // rate instead made the simulator the only thing in the system reporting 5 Hz for two roles
  // that declare a third of a hertz -- and the rates the dashboard showed were the simulator's,
  // not the station's.
  double actuatorIntervalS = 3.0;
  double actuatorIdleIntervalS = 20.0;
  double mixerAccumulator = 3.0;
  double pumpAccumulator = 3.0;
  // The node's deep-sleep measurement period in minutes (`moduleCfg.measure_time`). Stored so
  // the verb is accepted and visible, though the host model never sleeps.
  double sleepCycleMin = 15.0;

  Publisher odPub(Role::OdSensor, bootId);
  Publisher aeratorPub(Role::Aerator, bootId);
  Publisher mixerPub(Role::Mixer, bootId);
  Publisher pumpPub(Role::Pump, bootId);

  // A run that starts mid-experiment is the case the dashboard is built for.
  if (startMonitoring) odPub.setExperiment("exp-host-virtual");

  const Role allRoles[kRoleCount] = {Role::OdSensor, Role::Aerator, Role::Mixer, Role::Pump};
  for (Role role : allRoles) {
    char topic[64];
    if (!aqua::presenceTopic(role, topic, sizeof(topic))) continue;
    char payload[160];
    std::snprintf(payload, sizeof(payload), "{\"online\":true,\"role\":\"%s\",\"reason\":\"boot\"}",
                  aqua::roleName(role));
    emitLine("PRESENCE", topic, payload, std::strlen(payload));
  }

  std::thread reader(stdinReader);

  const auto start = std::chrono::steady_clock::now();
  const double dt = 1.0 / hz;
  uint32_t simMs = 0;
  bool finished = false;

  // Telemetry is stamped from the simulator's own simulated clock, offset so it lands in real
  // time. This is what a device does: a node stamps a sample with the reading's timestamp, not
  // with whatever the wall clock says when the payload happens to be assembled.
  //
  // Re-reading `epochMs()` per tick instead made every published stamp advance by the *tick
  // period* rather than by the sampling interval. The od_sensor samples every 5 s but reads the
  // clock while standing at a tick boundary, so consecutive stamps differed by ~4 ms and the
  // gateway -- which is right to measure the interval between the node's own stamps -- computed
  // 250 Hz for a 0.2 Hz sensor. Worse, which tick each role happened to reach the clock on was
  // pure luck: the aerator, mixer and pump, with no sensor read in front of them, reported
  // 4.67 Hz against 5. The measurement was honest; the clock it measured was not.
  const uint64_t deviceBaseMs = epochMs();

  std::fprintf(stderr,
               "[virtual_node] 4 roles, %.2f Hz, boot_id=%u, batch=%zu, envelope budget=%zu B\n",
               hz, bootId, Publisher::kSamplesPerBatch, aqua::AQUA_ENVELOPE_MAX_BYTES);

  while (!finished) {
    // ---- drain commands ---------------------------------------------------
    for (;;) {
      Command cmd;
      {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        if (g_queue.empty()) break;
        cmd = g_queue.front();
        g_queue.pop();
      }

      Role role;
      if (!roleFromName(cmd.role, role)) continue;

      std::string verdict = "applied";
      std::string note;
      // Whether this role's branch actually recognised the verb. The real firmware keeps the
      // same flag in `applyCommand` and acks `rejected` when it is false; without it here the
      // simulator acknowledged *every* command, so a verb the role does not implement came
      // back as success and TEST-PLAN §B3 — the honest-ack assertion — could not fail.
      bool handled = false;

      if (cmd.action == "emergency_stop") {
        if (role == Role::Aerator) {
          aeratorEstop = true;
          aeratorRunning = false;
          aeratorTargetRpm = 0.0;
        } else if (role == Role::Mixer) {
          mixerTargetDuty = 0.0;
        } else if (role == Role::Pump) {
          pumpMode = pmp::kModeIdle;
          pumpTargetMl = 0.0;
        }
        handled = true;
        note = "latched";
      } else if (cmd.action == "clear_estop" || cmd.action == "failsafe_clear" ||
                 cmd.action == "resume") {
        // cloud_worker.cpp groups `resume` with `clear_estop`: both re-arm the drive after a
        // latched stop. Treating them as one verb keeps the simulator's behaviour identical
        // to the node's rather than depending on which word the console happened to send.
        aeratorEstop = false;
        handled = true;
      } else if (role == Role::Aerator) {
        if (cmd.action == "set_speed") {
          // `speed_percent`, not `rpm`: cloud_worker.cpp reads `cmd["speed_percent"]` and the
          // orchestrator sends the same (`recipes.py` -> `("aerator","set_speed",
          // {"speed_percent": step_percent})`). Reading `rpm` meant the key was never present,
          // the default (`aeratorTargetRpm`, i.e. 0 on a fresh boot) applied, and the node
          // *acknowledged* a setpoint it had silently ignored -- the motor stayed at 0 RPM
          // while `p2_step` advanced, so the whole step protocol ran against a dead fan.
          // `rpm` is still accepted as an explicit absolute override, since that is a true
          // station capability and the console may send it.
          if (cmd.body.find("\"rpm\"") != std::string::npos) {
            aeratorTargetRpm = jsonNumber(cmd.body, "rpm", aeratorTargetRpm);
          } else {
            const double pct = jsonNumber(cmd.body, "speed_percent", 0.0);
            aeratorTargetRpm = pct * Plant::kMaxRpm / 100.0;
          }
          aeratorRunning = true;
          handled = true;
        } else if (cmd.action == "start") {
          aeratorRunning = true;
          // cloud_worker.cpp: `start` with no throttle falls back to `manual_throttle_pct`
          // or 20%. The simulator assumed 300 RPM, which is 50% of kMaxRpm -- a value the
          // node would never pick, so a bare `start` behaved differently on the bench.
          const double pct = jsonNumber(cmd.body, "speed_percent", 0.0);
          if (pct > 0.0) aeratorTargetRpm = pct * Plant::kMaxRpm / 100.0;
          else if (aeratorTargetRpm <= 0.0) aeratorTargetRpm = 20.0 * Plant::kMaxRpm / 100.0;
          handled = true;
        } else if (cmd.action == "stop") {
          aeratorRunning = false;
          aeratorTargetRpm = 0.0;
          handled = true;
        } else if (cmd.action == "set_mode" || cmd.action == "set_config") {
          // `target_do` is the key the real node reads for the loop's DO setpoint
          // (`cloud_worker.cpp` -> `cfg.target_do_mg_l`, default 7.5 in control_engine.h).
          // The simulator used to only echo the mode into `note`, so `closed_loop` looked
          // configured while nothing held a setpoint at all -- and a recipe sending the
          // console's name (`setpoint_mg_l`) silently set nothing on the real node either.
          //
          // The node runs a PID (`control_engine.cpp`, with anti-windup); the host model
          // closes a proportional loop instead. `ponytail:` P-only is enough to make
          // `closed_loop` meaningful on the bench -- it drives RPM from the DO error and
          // settles near the setpoint -- and adding I/D would only change the approach
          // transient, which no phase here gates on. The setpoint, mode and gains are all
          // stored so the telemetry reports what was actually commanded.
          if (cmd.body.find("\"mode\"") != std::string::npos) {
            aeratorMode = jsonString(cmd.body, "mode", aeratorMode.c_str());
          }
          if (cmd.body.find("\"target_do\"") != std::string::npos) {
            const double sp = jsonNumber(cmd.body, "target_do", aeratorSetpointDo);
            if (sp >= 0.0 && sp <= 20.0) aeratorSetpointDo = sp;
          } else if (cmd.body.find("\"setpoint_mg_l\"") != std::string::npos) {
            const double sp = jsonNumber(cmd.body, "setpoint_mg_l", aeratorSetpointDo);
            if (sp >= 0.0 && sp <= 20.0) aeratorSetpointDo = sp;
          }
          if (cmd.body.find("\"kp\"") != std::string::npos) aeratorKp = jsonNumber(cmd.body, "kp", aeratorKp);
          note = "mode=" + aeratorMode;
          handled = true;
        } else if (cmd.action == "force_on") {
          // Mirrors cloud_worker.cpp: force_on drives a fixed throttle regardless of the
          // control mode, which is the purge path.
          aeratorRunning = true;
          const double pct = jsonNumber(cmd.body, "speed_percent", 0.0);
          if (pct > 0.0) aeratorTargetRpm = pct * Plant::kMaxRpm / 100.0;
          else if (aeratorTargetRpm <= 0.0) aeratorTargetRpm = 20.0 * Plant::kMaxRpm / 100.0;
          note = "forced";
          handled = true;
        } else if (cmd.action == "force_off") {
          // cloud_worker.cpp treats force_off exactly as stop.
          aeratorRunning = false;
          aeratorTargetRpm = 0.0;
          handled = true;
        }
      } else if (role == Role::Mixer) {
        if (cmd.action == "start" || cmd.action == "start_mixer") mixerTargetDuty = 0.62, handled = true;
        else if (cmd.action == "stop" || cmd.action == "stop_mixer") mixerTargetDuty = 0.0, handled = true;
        else if (cmd.action == "set_speed" || cmd.action == "start" || cmd.action == "start_mixer") {
          // The mixer is commanded in *RPM*, not duty: cloud_worker.cpp reads `target_rpm`
          // then `mixer_rpm` (int or float), falling back to `target_rad_s`/`speed_percent`,
          // and floors a bare start at `MIXER_DEFAULT_RPM` = 600. The simulator read a
          // `duty` key nothing sends, so every mixer command applied its default and the
          // impeller never turned -- `planta_1_deox.p1_prep` (mixer_rpm 1000) would have
          // "run" with a stationary mixer.
          //
          // This model drives `mixer.setDuty`, so the RPM is converted against the ESC's
          // ceiling (`BLDC_MAX_RPM` = 1000 in uart_protocol.h). Linear is a deliberate
          // simplification of the node's PID: `ponytail:` the closed loop would only change
          // the spin-up transient, and every phase here gates on a steady-state condition.
          double rpm = 0.0;
          if (cmd.body.find("\"target_rpm\"") != std::string::npos) rpm = jsonNumber(cmd.body, "target_rpm", 0.0);
          else if (cmd.body.find("\"mixer_rpm\"") != std::string::npos) rpm = jsonNumber(cmd.body, "mixer_rpm", 0.0);
          if (rpm < 100.0) rpm = kMixerDefaultRpm;  // bare start / below-floor -> default
          mixerTargetDuty = std::fmin(1.0, rpm / kMixerMaxRpm);
          handled = true;
        }
        else if (cmd.action == "set_recipe") note = "recipe accepted", handled = true;
      } else if (role == Role::Pump) {
        if (cmd.action == "dose") {
          // `target_ml`, not `ml`: the real node reads `target["target_ml"]`
          // (nodo-bomba/src/main.cpp) and schema v2 is volume-oriented throughout. Reading a
          // key nothing sends meant the default applied and every dose came out 25 mL -- a
          // 12.5 mL dose silently became a 25 mL one, which is a dosing error the plant model
          // would happily carry out.
          pumpTargetMl = jsonNumber(cmd.body, "target_ml", 25.0);
          pumpDosedMl = 0.0;
          pumpRevsLastSeen = pump.revolutions();
          pumpMode = pmp::kModeDose;
          handled = true;
        } else if (cmd.action == "stop" || cmd.action == "force_off") {
          // main.cpp runs `force_off` through the same branch as `stop`: it is the purge
          // stop, not a dose.
          pumpMode = pmp::kModeIdle;
          pumpTargetMl = 0.0;
          handled = true;
        } else if (cmd.action == "force_on") {
          // Continuous run for purging a line, per main.cpp.
          pumpMode = pmp::kModeManual;
          handled = true;
        }
      } else if (role == Role::OdSensor) {
        if (cmd.action == "start_monitor" || cmd.action == "start") {
          monitoring = true;
          handled = true;
        } else if (cmd.action == "stop_monitor" || cmd.action == "stop") {
          monitoring = false;
          odPub.setExperiment("");
          handled = true;
        } else if (cmd.action == "set_config" || cmd.action == "set_sampling_rate") {
          // `interval_sec`, matching the real node (nodo-sensor-od/src/main.cpp reads
          // `pDoc["interval_sec"]`). The simulator read `interval_s`, which nothing sends, so
          // the default always applied and a requested sampling rate was silently ignored.
          sensorIntervalS = jsonNumber(cmd.body, "interval_sec", sensorIntervalS);
          if (sensorIntervalS < 0.2) sensorIntervalS = 0.2;
          if (sensorIntervalS > 60.0) sensorIntervalS = 60.0;
          handled = true;
        } else if (cmd.action == "start_experiment") {
          monitoring = true;
          odPub.setExperiment(jsonString(cmd.body, "experiment_id", "exp-host-virtual"));
          handled = true;
        } else if (cmd.action == "stop_experiment") {
          monitoring = false;
          odPub.setExperiment("");
          handled = true;
        } else if (cmd.action == "manual_sample") {
          sensorAccumulator = sensorIntervalS;
          handled = true;
        } else if (cmd.action == "set_do") {
          // Bench-only manual override for the plant's dissolved oxygen, so a recipe whose
          // first phase is a physical precondition can be reached. `planta_2_step.p2_precond`
          // gates on `DO < 3.5 mg/L`; by respiration that is a ~42 minute wait and the fast
          // path needs the pump, so without this the recipe is untestable on a partial bench.
          //
          // This verb has no counterpart in the real firmware -- a physical station gets its DO
          // from the water. It is deliberately named `set_do` rather than reusing a real verb so
          // it cannot be mistaken for station behaviour, and the ack says so in `note`.
          //
          // No value clears the override and returns the plant to its own dynamics.
          if (cmd.body.find("\"do_mg_l\"") == std::string::npos) {
            plant.clearDoOverride();
            note = "override cleared";
          } else {
            plant.doOverrideMgL = jsonNumber(cmd.body, "do_mg_l", plant.doMgL);
            note = "bench override";
          }
          handled = true;
        } else if (cmd.action == "set_sleep_cycle") {
          // main.cpp persists this to EEPROM as the deep-sleep measurement period; the host
          // model has no EEPROM, so it only accepts the verb. Refusing it would make a
          // valid console action look like a role mismatch.
          const double cycleMin = jsonNumber(cmd.body, "measure_time_min", 15.0);
          if (cycleMin >= 1.0 && cycleMin <= 1440.0) sleepCycleMin = cycleMin;
          handled = true;
        }
      }

      // `set_state` and `stop_experiment` are lifecycle verbs every role accepts: the
      // orchestrator sends them on entering IDLE and every node must not treat that as a
      // fault. Handling them centrally keeps each role's branch to its own mechanism.
      if (cmd.action == "set_state" || cmd.action == "stop_experiment") {
        if (role == Role::Aerator) {
          aeratorRunning = false;
          aeratorTargetRpm = 0.0;
        } else if (role == Role::Mixer) {
          mixerTargetDuty = 0.0;
        } else if (role == Role::Pump) {
          pumpMode = pmp::kModeIdle;
          pumpTargetMl = 0.0;
        } else if (role == Role::OdSensor) {
          monitoring = false;
          odPub.setExperiment("");
        }
        handled = true;
      }

      // A verb this role does not implement is refused, not silently accepted: an ack of
      // success for work never done is worse than an honest failure, because the log then
      // claims a safety action happened when it did not.
      if (!handled) {
        verdict = "rejected";
        note = "unsupported_action";
      }

      // An e-stop is latched: a later setpoint is refused, not silently accepted, so the
      // operator sees why the motor did not move.
      if (aeratorEstop && role == Role::Aerator && cmd.action == "set_speed") {
        verdict = "rejected";
        note = "estop latched";
      }

      char topic[64];
      if (aqua::ackTopic(role, topic, sizeof(topic))) {
        char payload[384];
        std::snprintf(payload, sizeof(payload),
                      "{\"cmd_id\":\"%s\",\"result\":\"%s\",\"node_id\":\"%s\",\"t_dev_ms\":%llu%s%s%s}",
                      cmd.cmdId.c_str(), verdict.c_str(), aqua::nodeId(role),
                      static_cast<unsigned long long>(epochMs()),
                      note.empty() ? "" : ",\"note\":\"", note.c_str(), note.empty() ? "" : "\"");
        emitLine("ACK", topic, payload, std::strlen(payload));
      }
    }

    // ---- advance the device models ---------------------------------------
    // Advanced first so `simMs` is the *instant of this tick's reading* rather than the instant
    // before it. Stamping with the previous tick's clock put every sample one tick late, which
    // is invisible in the values and wrong in the timeline.
    simMs += static_cast<uint32_t>(dt * 1000.0);

    const bool aeratorCommanded = aeratorRunning && !aeratorEstop;
    // In a closed-loop mode the commanded RPM comes from the DO error, not from a stored
    // throttle: the real node runs a PID on `plan.doMgL` vs `cfg.target_do_mg_l`
    // (control_engine.cpp). A proportional law is enough here -- see the `set_config` note.
    // Outside those modes the stored throttle is used verbatim, which is what `p2_step` wants.
    double commandedRpm = aeratorCommanded ? aeratorTargetRpm : 0.0;
    if (aeratorCommanded && !aeratorEstop &&
        (aeratorMode == "pid" || aeratorMode == "fuzzy")) {
      const double error = aeratorSetpointDo - plant.doMgL;  // +ve -> need more oxygen
      commandedRpm = std::fmax(0.0, std::fmin(Plant::kMaxRpm, aeratorKp * error * Plant::kMaxRpm));
      aeratorTargetRpm = commandedRpm;
    }
    if (aeratorCommanded && !aerator.isArmed()) {
      aerator.requestState(8);  // CLOSED_LOOP_CONTROL
    } else if (!aeratorCommanded && aerator.isArmed()) {
      aerator.requestState(1);  // IDLE
    }
    aerator.requestVelocityRpm(static_cast<float>(commandedRpm));
    aerator.tick(simMs);

    mixer.setDuty(static_cast<float>(mixerTargetDuty));
    mixer.tick(simMs);

    const bool dosing = pumpMode == pmp::kModeDose && pumpTargetMl > 0.0;
    pump.setDutyPercent(dosing ? 60.0f : 0.0f);
    pump.tick(simMs);

    // ---- plant ------------------------------------------------------------
    // The delivered volume comes from the pump model's own revolutions, exactly as the real
    // node derives it (`st.dosed_ml += revDelta * ML_PER_REV`, nodo-bomba/src/main.cpp), so the
    // dose takes as long as the head physically takes.
    //
    // This used to spread the target over a fixed 12 s (`target * dt / 12.0`) so the dose was
    // "visible on the DO trace". At 12.5 mL that happened to match the model's flow (12.5/12 ~=
    // 1.04 mL/s against the head's ~1.2 mL/s at 60% duty), so it looked right. It was a
    // coincidence: the number was pinned to the one dose then in use, and any other target made
    // the simulator report a flow rate the hardware cannot produce.
    const double revDelta = std::fmax(0.0, pump.revolutions() - pumpRevsLastSeen);
    pumpRevsLastSeen = pump.revolutions();
    const double dosedThisTick = dosing ? (revDelta * kPumpMlPerRev) : 0.0;
    if (dosedThisTick > 0.0) {
      pumpDosedMl += dosedThisTick;
      if (pumpDosedMl >= pumpTargetMl) {
        pumpDosedMl = pumpTargetMl;
        pumpMode = pmp::kModeIdle;
      }
    }
    plant.tick(dt, aerator.actualRpm(), mixer.rpm() > 40.0f, dosedThisTick);

    // ---- read the sensor through the real model --------------------------
    // The plant supplies the physical value; the model's own oscillation is zeroed so it reports
    // that value instead of an independent sine. Everything downstream — integer scaling,
    // register semantics, fault path — is `sensor_sim.cpp`, unmodified.
    OdSimSpec coupled = odSpec;
    coupled.doAmp = 0.0f;
    coupled.tempAmp = 0.0f;
    coupled.satAmp = 0.0f;
    coupled.doMgL = static_cast<float>(plant.doMgL);
    coupled.tempC = static_cast<float>(plant.tempC);
    coupled.satPct = static_cast<float>(plant.doMgL / Plant::saturationMgL(plant.tempC) * 100.0);
    odSensor.setSpec(coupled);
    odSensor.tick(simMs);

    int16_t tempCenti = 0, satDeciPct = 0, doMilliMgL = 0, p3Centi = 0, p4Centi = 0;
    const bool sensorOk = odSensor.read(simMs, tempCenti, satDeciPct, doMilliMgL, p3Centi, p4Centi);

    // ---- publish ----------------------------------------------------------
    const uint64_t nowMs = deviceBaseMs + simMs;
    const int rssi = -52 - static_cast<int>((simMs / 1000) % 18);  // wander, so RSSI is not fixed

    sensorAccumulator += dt;
    if (monitoring && sensorAccumulator >= sensorIntervalS) {
      sensorAccumulator = 0.0;
      FieldValue fields[12];
      fields[0] = FieldValue::num(odf::kDoMgL, doMilliMgL / 1000.0, 3);
      fields[1] = FieldValue::integer(odf::kDoRaw, doMilliMgL);
      fields[2] = FieldValue::num(odf::kSatPct, satDeciPct / 10.0, 2);
      fields[3] = FieldValue::integer(odf::kSatRaw, satDeciPct);
      fields[4] = FieldValue::num(odf::kTempC, tempCenti / 100.0, 2);
      fields[5] = FieldValue::integer(odf::kTempRaw, tempCenti);
      fields[6] = FieldValue::integer(odf::kAux1Raw, p3Centi);
      fields[7] = FieldValue::integer(odf::kAux2Raw, p4Centi);
      fields[8] = FieldValue::num(odf::kBatteryV, odSensor.batteryMilliVolts() / 1000.0, 2);
      fields[9] = FieldValue::num(odf::kRtcTempC, plant.tempC + 2.4, 2);
      fields[10] = FieldValue::integer(odf::kRtcHealth, 1);
      // A failed Modbus read is reported in the status word rather than by publishing nothing:
      // silence is indistinguishable from a dead node, which is the failure the status field
      // exists to rule out.
      fields[11] = FieldValue::integer(odf::kDevStatus, sensorOk ? 0 : 1);
      odPub.add(fields, 12, nowMs, TimeSource::Rtc, rssi);
    }

    {
      FieldValue fields[13];
      fields[0] = FieldValue::num(aer::kRpm, aerator.actualRpm(), 2);
      fields[1] = FieldValue::num(aer::kRpmTarget, commandedRpm, 2);
      fields[2] = FieldValue::num(aer::kVbusV, aerator.vbus(), 2);
      fields[3] = FieldValue::num(aer::kIbusA, aerator.ibus(), 2);
      fields[4] = FieldValue::num(aer::kPowerW, aerator.power(), 2);
      fields[5] = FieldValue::num(aer::kTorqueNm, aerator.torque(), 3);
      fields[6] = FieldValue::num(aer::kIqA, aerator.iq(), 2);
      fields[7] = FieldValue::flag(aer::kRunning, aerator.actualRpm() > 50.0f);
      fields[8] = FieldValue::integer(aer::kStatus, aerator.state());
      fields[9] = FieldValue::text(aer::kCtlMode, aeratorEstop ? aer::kModeManual : aer::kModePid);
      fields[10] = FieldValue::flag(aer::kCtlFailsafe, aeratorEstop);
      // The ESP-NOW echo of the logger's measurement. Present and non-authoritative, which is
      // exactly the case the dashboard must not average into the od_* source.
      fields[11] = FieldValue::num(aer::kPeerDoMgL, plant.doMgL, 3);
      fields[12] = FieldValue::num(aer::kPeerTempC, plant.tempC, 2);
      aeratorPub.add(fields, 13, nowMs, TimeSource::EspNow, rssi);
    }

    {
      const double radS = mixer.rpm() * 2.0 * kPi / 60.0;
      const double targetRpm = mixerTargetDuty > 0.01 ? 780.0 : 0.0;
      FieldValue fields[8];
      fields[0] = FieldValue::num(mix::kRpm, mixer.rpm(), 1);
      fields[1] = FieldValue::num(mix::kRpmTarget, targetRpm, 1);
      fields[2] = FieldValue::num(mix::kRadS, radS, 3);
      fields[3] = FieldValue::num(mix::kRadSTarget, targetRpm * 2.0 * kPi / 60.0, 3);
      fields[4] = FieldValue::num(mix::kDutyPct, mixer.duty() * 100.0, 2);
      fields[5] = FieldValue::flag(mix::kRunning, mixer.rpm() > 40.0f);
      fields[6] = FieldValue::flag(mix::kStall, mixerTargetDuty > 0.3 && mixer.rpm() < 40.0f);
      fields[7] = FieldValue::integer(mix::kStatus, mixer.faultCode());
      mixerAccumulator += dt;
      const double mixerIntervalS =
          mixer.rpm() > 40.0f ? actuatorIntervalS : actuatorIdleIntervalS;
      if (mixerAccumulator >= mixerIntervalS) {
        mixerAccumulator = 0.0;
        // One envelope per gate: the mixer's firmware pushes a single reading, it does not
        // accumulate. Batching here multiplied the two intervals and hid a healthy idle node.
        mixerPub.addNow(fields, 8, nowMs, TimeSource::Ntp, rssi);
      }
    }

    {
      FieldValue fields[7];
      fields[0] = FieldValue::num(pmp::kDosedMl, pumpDosedMl, 2);
      fields[1] = FieldValue::num(pmp::kTargetMl, pumpTargetMl, 2);
      fields[2] = FieldValue::num(pmp::kRpm, pump.rpm(), 2);
      fields[3] = FieldValue::text(pmp::kMode, pumpMode.c_str());
      fields[4] = FieldValue::flag(pmp::kDoseCompleted,
                                   pumpMode == pmp::kModeIdle && pumpDosedMl > 0.0);
      fields[5] = FieldValue::flag(pmp::kDoseTimedOut, false);
      fields[6] = FieldValue::integer(pmp::kStatus,
                                      pump.fault() == PumpSimFault::None ? 0 : 1);
      pumpAccumulator += dt;
      const double pumpIntervalS = dosing ? actuatorIntervalS : actuatorIdleIntervalS;
      if (pumpAccumulator >= pumpIntervalS) {
        pumpAccumulator = 0.0;
        pumpPub.addNow(fields, 7, nowMs, TimeSource::Ntp,
                       aqua::EnvelopeBuilder::AQUA_RSSI_ABSENT);
      }
    }

    const double elapsed =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    if (durationS > 0.0 && elapsed >= durationS) finished = true;

    std::this_thread::sleep_for(std::chrono::duration<double>(dt));
  }

  // Flush any partial batch: losing the tail of a run is exactly the kind of quiet gap the
  // sequence-gap detector would otherwise report as a firmware fault.
  const uint64_t finalMs = epochMs();
  odPub.flush(finalMs, TimeSource::Rtc, -60);
  aeratorPub.flush(finalMs, TimeSource::EspNow, -60);
  mixerPub.flush(finalMs, TimeSource::Ntp, -60);
  pumpPub.flush(finalMs, TimeSource::Ntp, aqua::EnvelopeBuilder::AQUA_RSSI_ABSENT);

  for (Role role : allRoles) {
    char topic[64];
    if (!aqua::presenceTopic(role, topic, sizeof(topic))) continue;
    char payload[160];
    std::snprintf(payload, sizeof(payload),
                  "{\"online\":false,\"role\":\"%s\",\"reason\":\"shutdown\"}",
                  aqua::roleName(role));
    emitLine("PRESENCE", topic, payload, std::strlen(payload));
  }

  std::fprintf(stderr,
               "[virtual_node] stopped: last seq od=%u aer=%u mix=%u pump=%u, dropped=%u\n",
               odPub.sequence(), aeratorPub.sequence(), mixerPub.sequence(), pumpPub.sequence(),
               odPub.dropped() + aeratorPub.dropped() + mixerPub.dropped() + pumpPub.dropped());

  std::fflush(stdout);
  std::fflush(stderr);
  // The stdin reader blocks in fgets on a detached thread. Skipping destructors avoids racing it
  // during static teardown; there is nothing to flush that has not been flushed above.
  std::_Exit(0);
}
