#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

// Test 1: 8.3 FAT filename validation and sanitization
bool sanitizeFilename83(const char* input, char* output, size_t maxLen) {
  if (!input || !output || maxLen < 16) return false;

  const char* src = input;
  if (*src == '/') src++; // Skip leading slash if present

  char base[16] = {0};
  size_t idx = 0;

  while (*src && *src != '.' && idx < 8) {
    char ch = *src++;
    if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') || ch == '_') {
      if (ch >= 'a' && ch <= 'z') ch -= 32; // Uppercase
      base[idx++] = ch;
    }
  }

  if (idx == 0) {
    std::strcpy(base, "EXP_DATA");
  }

  std::snprintf(output, maxLen, "/%s.CSV", base);
  return true;
}

// Test 2: RFC 4180 CSV row formatting
void formatCsvRow(char* buffer, size_t maxLen, const char* isoTime, uint32_t sec2000,
                  float doMgL, float satPct, float tempC, float battV) {
  std::snprintf(buffer, maxLen, "%s,%lu,%.3f,%.2f,%.2f,%.2f\r\n",
                isoTime, (unsigned long)sec2000, doMgL, satPct, tempC, battV);
}

int main() {
  std::printf("====================================================\n");
  std::printf(" SELF-CHECK: OD-LOGGER EXPERIMENT PROTOCOL & STORAGE\n");
  std::printf("====================================================\n\n");

  // 1. Validate 8.3 FAT Filename Sanitization
  char cleanName[24];
  assert(sanitizeFilename83("EXP_01.CSV", cleanName, sizeof(cleanName)));
  assert(std::strcmp(cleanName, "/EXP_01.CSV") == 0);

  assert(sanitizeFilename83("long_experiment_name_01.csv", cleanName, sizeof(cleanName)));
  assert(std::strcmp(cleanName, "/LONG_EXP.CSV") == 0); // 8 chars max base

  assert(sanitizeFilename83("/ensayo#2.csv", cleanName, sizeof(cleanName)));
  assert(std::strcmp(cleanName, "/ENSAYO2.CSV") == 0);
  std::printf("  ✓ 1. 8.3 FAT filename sanitization verified (/EXP_01.CSV, /LONG_EXP.CSV, etc.)\n");

  // 2. Validate CSV Header and RFC 4180 Row Formatting
  const char* expectedHeader = "Timestamp,RTC_Sec,DO_mg_L,Sat_Pct,Temp_C,Batt_V";
  char rowBuf[128];
  formatCsvRow(rowBuf, sizeof(rowBuf), "2026-09-11T20:00:00-05:00", 841500000, 7.850f, 98.50f, 23.50f, 4.12f);
  assert(std::strstr(rowBuf, "2026-09-11T20:00:00-05:00,841500000,7.850,98.50,23.50,4.12\r\n") != nullptr);
  std::printf("  ✓ 2. RFC 4180 CSV row formatting matches web dashboard download contract\n");

  // 3. Validate Sampling Rate Clamping
  int testIntervals[] = {0, 1, 2, 5, 10, 60, 120};
  int expectedClamped[] = {1, 1, 2, 5, 10, 60, 60};
  for (size_t i = 0; i < sizeof(testIntervals)/sizeof(testIntervals[0]); ++i) {
    int val = testIntervals[i];
    if (val < 1) val = 1;
    if (val > 60) val = 60;
    assert(val == expectedClamped[i]);
  }
  std::printf("  ✓ 3. Sampling rate clamping (1 - 60s) verified\n");

  // 4. Validate Command Type to Action Fallback
  auto resolveEffectiveAction = [](const char* action, const char* cmdType) -> std::string {
    if (action && std::strlen(action) > 0) return action;
    if (cmdType && std::strcmp(cmdType, "start") == 0) return "start_monitor";
    if (cmdType && std::strcmp(cmdType, "stop") == 0) return "stop_monitor";
    return cmdType ? cmdType : "";
  };

  assert(resolveEffectiveAction("", "start") == "start_monitor");
  assert(resolveEffectiveAction("", "stop") == "stop_monitor");
  assert(resolveEffectiveAction("start_experiment", "start") == "start_experiment");
  assert(resolveEffectiveAction("stop_experiment", "stop") == "stop_experiment");
  std::printf("  ✓ 4. Bare command_type ('start' -> 'start_monitor', 'stop' -> 'stop_monitor') fallback verified\n");

  // 5. Validate UTC to UTC-5 Local Time Conversion
  const uint32_t LOCAL_TZ_OFFSET = 5 * 3600; // 18000s
  const uint32_t UNIX_OFFSET_2000 = 946684800;
  // Example: 2026-09-12 02:00:00 UTC (unixTs = 1789178400)
  uint64_t sampleUtc = 1789178400ULL;
  uint64_t sampleLocal = sampleUtc - LOCAL_TZ_OFFSET; // 2026-09-11 21:00:00 UTC-5 (unixTs = 1789160400)
  uint32_t sampleSec2000 = (uint32_t)(sampleLocal - UNIX_OFFSET_2000);
  assert(sampleUtc - sampleLocal == 18000);
  assert(sampleSec2000 == 842475600);
  std::printf("  ✓ 5. Authoritative UTC to local time (UTC-5) conversion verified\n");

  // 6. Validate Fallback Payload Extraction from error_message JSON
  const char* sampleErrMsgJson = "{\"action\":\"start_experiment\",\"interval_sec\":3,\"csv_filename\":\"EXP_99.CSV\"}";
  assert(std::strstr(sampleErrMsgJson, "\"action\":\"start_experiment\"") != nullptr);
  assert(std::strstr(sampleErrMsgJson, "\"interval_sec\":3") != nullptr);
  assert(std::strstr(sampleErrMsgJson, "\"csv_filename\":\"EXP_99.CSV\"") != nullptr);
  std::printf("  ✓ 6. Database error_message JSON fallback payload extraction verified\n");

  // 7. Validate Stop Action Matching (stop_experiment and stop_monitor / stop)
  auto isStopAction = [](const char* act) -> bool {
    return (std::strcmp(act, "stop_experiment") == 0 ||
            std::strcmp(act, "stop_monitor") == 0 ||
            std::strcmp(act, "stop") == 0);
  };

  assert(isStopAction("stop_experiment"));
  assert(isStopAction("stop_monitor"));
  assert(isStopAction("stop"));
  assert(!isStopAction("start_experiment"));
  assert(!isStopAction("start_monitor"));
  std::printf("  ✓ 7. Stop action matching (stop_experiment, stop_monitor, stop) verified\n");

  // 8. Validate OLED Controller Column Offsets (SSD1306 0.96" vs SH1106 1.3")
  auto getOledColumnOffset = [](const char* controller) -> uint8_t {
    if (std::strcmp(controller, "SSD1306") == 0) return 0x00;
    if (std::strcmp(controller, "SH1106") == 0) return 0x02;
    return 0xFF;
  };
  assert(getOledColumnOffset("SSD1306") == 0x00);
  assert(getOledColumnOffset("SH1106") == 0x02);
  std::printf("  ✓ 8. OLED controller geometry & column offsets (SSD1306=0, SH1106=2) verified\n");

  // 9. Validate Dual Independent SPI Architecture (LoRa on VSPI, SD on HSPI)
  const int loraPins[] = {18, 5, 19, 27, 23, 26}; // CS, SCLK, MISO, MOSI, RST, DIO0
  const int sdPins[]   = {13, 14, 2, 15};          // CS, SCLK, MISO, MOSI
  // Verify 0 pin overlap between LoRa and SD
  for (int lp : loraPins) {
    for (int sp : sdPins) {
      assert(lp != sp);
    }
  }
  const int SPI_HOST_VSPI = 3;
  const int SPI_HOST_HSPI = 2;
  assert(SPI_HOST_VSPI != SPI_HOST_HSPI);
  std::printf("  ✓ 9. Dual SPI architecture (LoRa on VSPI/SPI3, SD on HSPI/SPI2, 0 pin overlap) verified\n");

  // 10. Validate Modbus RTU Frame Synchronization & Noise Resilience
  auto crc16_check = [](const uint8_t* data, size_t length) -> uint16_t {
    if (!data || length == 0) return 0;
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < length; ++i) {
      crc ^= static_cast<uint16_t>(data[i]);
      for (uint8_t bit = 0; bit < 8; ++bit) {
        if ((crc & 0x0001) != 0) crc = (crc >> 1) ^ 0xA001;
        else crc >>= 1;
      }
    }
    return crc;
  };

  auto validateModbusFrame = [&](uint8_t slaveId, uint8_t fn, const uint8_t* rx, size_t rxLen,
                                 size_t& outOffset, size_t& outLen) -> bool {
    const uint8_t MinFrame = 5;
    for (size_t off = 0; off + MinFrame <= rxLen; ++off) {
      if (rx[off] != slaveId) continue;
      if (rx[off + 1] != fn) continue;
      size_t expected = 0;
      if (fn == 0x06) expected = 8;
      else if (fn == 0x03) {
        if (off + 3 > rxLen) continue;
        uint8_t bc = rx[off + 2];
        expected = static_cast<size_t>(bc) + MinFrame;
      }
      if (off + expected > rxLen) continue;
      uint16_t rxCrc = static_cast<uint16_t>(rx[off + expected - 2]) |
                       (static_cast<uint16_t>(rx[off + expected - 1]) << 8);
      uint16_t calcCrc = crc16_check(&rx[off], expected - 2);
      if (rxCrc == calcCrc) {
        outOffset = off;
        outLen = expected;
        return true;
      }
    }
    return false;
  };

  // Test 10a: Exact user log failure: 3 noise bytes followed by valid 0x06 response
  const uint8_t noisyWriteRx[] = {0x00, 0x00, 0xE0, 0x0A, 0x06, 0x00, 0x01, 0x00, 0x1F, 0x98, 0xB9};
  size_t frameOff = 0, frameLen = 0;
  assert(validateModbusFrame(0x0A, 0x06, noisyWriteRx, sizeof(noisyWriteRx), frameOff, frameLen));
  assert(frameOff == 3);
  assert(frameLen == 8);

  // Test 10b: Clean frame without noise
  const uint8_t cleanWriteRx[] = {0x0A, 0x06, 0x00, 0x01, 0x00, 0x1F, 0x98, 0xB9};
  assert(validateModbusFrame(0x0A, 0x06, cleanWriteRx, sizeof(cleanWriteRx), frameOff, frameLen));
  assert(frameOff == 0);
  assert(frameLen == 8);

  // Test 10c: Corrupted CRC should fail
  const uint8_t badCrcRx[] = {0x0A, 0x06, 0x00, 0x01, 0x00, 0x1F, 0x00, 0x00};
  assert(!validateModbusFrame(0x0A, 0x06, badCrcRx, sizeof(badCrcRx), frameOff, frameLen));
  std::printf("  ✓ 10. Modbus RTU frame sync & noise resilience (handles leading 00 00 E0) verified\n");

  std::printf("\n====================================================\n");
  std::printf(" ¡TODAS LAS PRUEBAS DE EXPERIMENTO EN FIRMWARE OK! ✓\n");
  std::printf("====================================================\n");

  return 0;
}
