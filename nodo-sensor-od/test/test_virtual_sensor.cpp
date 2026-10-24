#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>

#define DISABLE_ESPNOW

#include "../src/esp_now_packet.h"
#include "../src/esp_now_sender.h"
#include "../src/sensor_sim.h"

int main() {
  std::printf("Running Virtual Sensor and ESP-NOW Stub tests...\n");
  assert(espNowSenderInit() == true);
  OdTelemetryPacket testPkt = {};
  testPkt.magic = AQUACONTROL_PACKET_MAGIC;
  testPkt.dissolved_oxygen = 7.85f;
  testPkt.water_temperature = 24.5f;
  testPkt.oxygen_saturation = 95.2f;
  testPkt.battery_voltage = 4.12f;
  assert(espNowBroadcastDO(testPkt) == true);
  std::printf("  [PASS] ESP-NOW stubs return true when DISABLE_ESPNOW is defined.\n");

  OdSimSpec spec;
  OdSim sim(spec);
  for (uint32_t ms = 0; ms <= 60000; ms += 250) {
    sim.tick(ms);
    int16_t tempCenti = 0, satDeci = 0, doMilli = 0, p3 = 0, p4 = 0;
    assert(sim.read(ms, tempCenti, satDeci, doMilli, p3, p4));
    assert(tempCenti >= -4000 && tempCenti <= 12500);
    assert(doMilli >= 0 && doMilli <= 500000);
    assert(satDeci >= 0 && satDeci <= 2000);
    assert(sim.batteryMilliVolts() >= 3400);
    float decodedDo = doMilli / 1000.0f;
    assert(std::fabs(decodedDo - sim.currentDoMgL()) < 0.001f);
    float decodedTemp = tempCenti / 100.0f;
    assert(std::fabs(decodedTemp - sim.currentTempC()) < 0.01f);
  }
  std::printf("  [PASS] Simulated telemetry stays within physical bounds over a full period.\n");
  std::printf("All virtual sensor tests passed successfully!\n");
  return 0;
}
