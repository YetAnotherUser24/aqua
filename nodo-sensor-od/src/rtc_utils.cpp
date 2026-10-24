/**
 * @file rtc_utils.cpp
 * @brief RTC formatting and DS3231 alarm configuration functions.
 *
 * Only formatting and documentation were improved; all functional logic
 * remains identical to the original implementation.
 */

#include "rtc_utils.h"

#include "board.h"
#include "log.h"
#include "n_logger_config.h"
#include <RtcDS3231.h>
#include <Wire.h>
#include <sys/time.h>
#include <time.h>

/* --------------------------------------------------------------------------
 *  External variables defined in main program
 * -------------------------------------------------------------------------- */

extern RtcDS3231<TwoWire> rtc;
extern RtcDateTime rtcNow;
extern char dateStr[20];

static int s_rtcAvailable = -1; // -1: unprobed, 0: absent, 1: present

#ifndef countof
#define countof(a) (sizeof(a) / sizeof(a[0]))
#endif

/* --------------------------------------------------------------------------
 *  RTC Probing & Initialization
 * -------------------------------------------------------------------------- */

bool isRtcAvailable() {
  if (s_rtcAvailable == -1) {
    Wire.beginTransmission(0x68);
    s_rtcAvailable = (Wire.endTransmission() == 0) ? 1 : 0;
  }
  return s_rtcAvailable == 1;
}

bool rtcInit() {
  Wire.beginTransmission(0x68);
  s_rtcAvailable = (Wire.endTransmission() == 0) ? 1 : 0;

  if (s_rtcAvailable == 1) {
    rtc.Begin();
    rtc.Enable32kHzPin(false);
    rtc.SetSquareWavePin(DS3231SquareWavePin_ModeAlarmBoth);
    rtcNow = rtc.GetDateTime();
    Logln("[RTC] DS3231 hardware RTC detected and initialized.");
    if (rtcNow.Year() >= 2020) {
      struct timeval tv = { (time_t)(rtcNow.TotalSeconds() + UNIX_OFFSET_2000 + LOCAL_TZ_OFFSET_SEC), 0 };
      settimeofday(&tv, nullptr);
    }
    return true;
  }

  Logln("[RTC] DS3231 not found. Using ESP32 internal RTC / NTP time.");
  rtcNow = getRtcDateTime();
  return false;
}

void setRtcDateTime(const RtcDateTime& dt) {
  rtcNow = dt;
  if (isRtcAvailable()) {
    rtc.SetDateTime(dt);
  }
  uint32_t sec2000 = dt.TotalSeconds();
  struct timeval tv = { (time_t)(sec2000 + UNIX_OFFSET_2000 + LOCAL_TZ_OFFSET_SEC), 0 };
  settimeofday(&tv, nullptr);
}

RtcDateTime getRtcDateTime() {
  if (isRtcAvailable()) {
    return rtc.GetDateTime();
  }

  // Fallback: ESP32 internal POSIX clock (retained through deep sleep)
  time_t now = time(nullptr);
  if (now >= 1577836800ULL) { // >= 2020-01-01T00:00:00Z
    uint32_t sec2000 = (uint32_t)(now - LOCAL_TZ_OFFSET_SEC - UNIX_OFFSET_2000);
    return RtcDateTime(sec2000);
  }

  return RtcDateTime(0); // Epoch fallback (2000-01-01)
}

/* --------------------------------------------------------------------------
 *  Timestamp Formatting
 * -------------------------------------------------------------------------- */

/**
 * @brief Converts an RtcDateTime into a human-readable string.
 *
 * Format example: "2025/12/05 21:37:10"
 */
void formatDateTime(const RtcDateTime& dt) {
  snprintf_P(dateStr, countof(dateStr), PSTR("%04u/%02u/%02u %02u:%02u:%02u"), dt.Year(),
             dt.Month(), dt.Day(), dt.Hour(), dt.Minute(), dt.Second());
}

/* --------------------------------------------------------------------------
 *  DS3231 Alarm Configuration
 * -------------------------------------------------------------------------- */

/**
 * @brief Sets DS3231 Alarm2 so the ESP32 will wake after a given number of minutes.
 *
 * Steps:
 *  - Reads current RTC time.
 *  - Computes new alarm timestamp.
 *  - Aligns alarm to the closest `minutes` interval.
 *  - Writes Alarm2 to DS3231.
 *
 * No-op if DS3231 is absent (ESP32 timer wakeup handles sleep interval).
 */
void configureAlarm(int minutes) {
  if (!isRtcAvailable()) {
    return;
  }

  rtcNow = rtc.GetDateTime();

  RtcDateTime alarmTime = rtcNow + (minutes * 60);

  // Align the minute field to the configured interval
  int adjust = alarmTime.Minute() % minutes;

  DS3231AlarmTwo alarm2(0, 0, alarmTime.Minute() - adjust, DS3231AlarmTwoControl_MinutesMatch);

  rtc.SetAlarmTwo(alarm2);

  // Clear any previous alarm flags
  rtc.LatchAlarmsTriggeredFlags();
}
