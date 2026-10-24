/**
 * @file rtc_utils.h
 * @brief RTC formatting and DS3231 alarm configuration helpers.
 *
 * Provides:
 *  - Human-readable timestamp formatting into the global `dateStr` buffer.
 *  - DS3231 Alarm2 setup for waking the ESP32 after a minutes-based interval.
 */

#pragma once
#include <Arduino.h>
#include <RtcDS3231.h>

/**
 * @brief Formats an RTC timestamp into the global `dateStr` buffer.
 *
 * Format: "YYYY/MM/DD HH:MM:SS"
 *
 * @param dt RtcDateTime structure to format.
 */
void formatDateTime(const RtcDateTime& dt);

/**
 * @brief Probes and initializes RTC (DS3231 if available, otherwise ESP32 internal RTC).
 * @return true if an external DS3231 was found and initialized.
 */
bool rtcInit();

/**
 * @brief Checks if external DS3231 RTC is present on the I2C bus.
 */
bool isRtcAvailable();

/**
 * @brief Gets current date/time from DS3231 if present, otherwise from ESP32 internal RTC.
 */
RtcDateTime getRtcDateTime();

/**
 * @brief Sets the system time and hardware DS3231 RTC (if available) to the given RtcDateTime.
 */
void setRtcDateTime(const RtcDateTime& dt);

/**
 * @brief Configures DS3231 Alarm2 to wake the system in a given number of minutes.
 *
 * Alarm2 uses minute-match mode, waking on the next aligned interval.
 * No-op if external DS3231 is not present.
 *
 * @param minutes Number of minutes until next wake. Must be >0.
 */
void configureAlarm(int minutes);