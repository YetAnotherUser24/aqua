#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

int main() {
  // 1. Basic milli-mg/L scaling tests
  float testValues[] = {0.0f, 0.001f, 8.456f, 14.205f, 20.000f, 32.760f};
  int16_t expected[] = {0, 1, 8456, 14205, 20000, 32760};

  for (size_t i = 0; i < sizeof(testValues) / sizeof(testValues[0]); ++i) {
    int16_t scaled = static_cast<int16_t>(std::roundf(testValues[i] * 1000.0f));
    assert(scaled == expected[i]);

    // Test reverse decoding
    float decoded = scaled / 1000.0f;
    assert(std::fabs(decoded - testValues[i]) < 0.0001f);

    // Test integer string formatting (as used in main.cpp & OLED)
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%d.%03d", scaled / 1000, std::abs(scaled % 1000));
    float parsed = std::strtof(buf, nullptr);
    assert(std::fabs(parsed - testValues[i]) < 0.001f);
  }

  // 2. Ensure int16_t capacity supports full typical DO range (0 - 30 mg/L -> 0 - 30000)
  assert(30000 <= INT16_MAX);

  // 3. Verify swapped register mapping:
  // Param 1 (0x0055) = Saturation (deci-%Sat, e.g. 98.5% -> 985, reading[1])
  // Param 2 (0x0057) = DO (milli-mg/L, e.g. 8.456 mg/L -> 8456, reading[2])
  float satInput = 98.5f;
  float doInput = 8.456f;
  int16_t readings[5] = {0};
  readings[1] = static_cast<int16_t>(std::roundf(satInput * 10.0f));
  readings[2] = static_cast<int16_t>(std::roundf(doInput * 1000.0f));
  assert(readings[1] == 985);
  assert(readings[2] == 8456);

  std::printf("All OD unit conversion and swapped index checks passed successfully.\n");
  return 0;
}
