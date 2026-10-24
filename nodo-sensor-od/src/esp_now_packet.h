#pragma once
#include <stdint.h>

#define AQUACONTROL_PACKET_MAGIC 0x41515541 // ASCII "AQUA"

#pragma pack(push, 1)
struct OdTelemetryPacket {
  uint32_t magic;           // AQUACONTROL_PACKET_MAGIC
  uint32_t sequence;        // Incremental sequence counter
  // Unix epoch milliseconds, UTC -- schema v2 `t_dev_ms`, and the same base the NTP nodes use.
  //
  // This field was previously documented as "milliseconds since 2000", which was never what the
  // sender wrote. The mismatch was harmless only because the receiver happened to leave the
  // value alone: a comment describing the wrong units is an invitation for someone to "fix"
  // the reader into converting a value that is already correct. `docs/TIME-SYNC.md` §3 retires
  // this ambiguity by name, which is why the field is not called `rtc_timestamp_ms` any more.
  uint64_t t_dev_ms;
  float dissolved_oxygen;   // mg/L (e.g. 7.850)
  float water_temperature;  // °C (e.g. 23.45)
  float oxygen_saturation;  // % Sat (e.g. 98.20)
  float battery_voltage;    // V (e.g. 4.18)
  uint32_t status_flags;    // Status bitfield
};
#pragma pack(pop)
