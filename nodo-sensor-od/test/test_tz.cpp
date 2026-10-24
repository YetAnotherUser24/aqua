#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>

#define UNIX_OFFSET_2000 946684800UL
#define LOCAL_TZ_OFFSET_SEC (5 * 3600) // UTC-5

int main() {
  // 1. UTC timestamp corresponding to 2026-09-12 00:29:19 GMT
  std::tm utc_tm = {};
  utc_tm.tm_year = 2026 - 1900;
  utc_tm.tm_mon = 8; // September (0-indexed)
  utc_tm.tm_mday = 12;
  utc_tm.tm_hour = 0;
  utc_tm.tm_min = 29;
  utc_tm.tm_sec = 19;

  std::time_t utcEpoch = 1789172959; // 2026-09-12 00:29:19 UTC

  // 2. Convert to local seconds since 2000
  uint32_t localSec2000 = static_cast<uint32_t>(utcEpoch - LOCAL_TZ_OFFSET_SEC - UNIX_OFFSET_2000);

  // 3. Local unix timestamp
  std::time_t localUnix = static_cast<std::time_t>(localSec2000 + UNIX_OFFSET_2000);
  std::tm* local_tm = std::gmtime(&localUnix);

  assert(local_tm->tm_year + 1900 == 2026);
  assert(local_tm->tm_mon + 1 == 9);
  assert(local_tm->tm_mday == 11);
  assert(local_tm->tm_hour == 19);
  assert(local_tm->tm_min == 29);
  assert(local_tm->tm_sec == 19);

  // 4. Invert: localSec2000 + UNIX_OFFSET_2000 + LOCAL_TZ_OFFSET_SEC must yield exact original UTC
  std::time_t invertedUtc = localSec2000 + UNIX_OFFSET_2000 + LOCAL_TZ_OFFSET_SEC;
  assert(invertedUtc == utcEpoch);

  // 5. ISO-8601 formatting with -05:00
  char isoBuf[30];
  std::snprintf(isoBuf, sizeof(isoBuf), "%04d-%02d-%02dT%02d:%02d:%02d-05:00",
                local_tm->tm_year + 1900, local_tm->tm_mon + 1, local_tm->tm_mday,
                local_tm->tm_hour, local_tm->tm_min, local_tm->tm_sec);

  std::printf("ISO 8601 string: %s\n", isoBuf);
  assert(std::string(isoBuf) == "2026-09-11T19:29:19-05:00");

  std::printf("All UTC-5 timezone offset tests passed successfully.\n");
  return 0;
}
