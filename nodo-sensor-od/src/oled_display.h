/**
 * @file oled_display.h
 * @brief I2C OLED (1.3" SH1106 / 0.96" SSD1306) visualization module for n-logger platform.
 */

#pragma once

#include "n_logger_config.h"
#include <Arduino.h>
#include <RtcDS3231.h>

/**
 * @brief Initializes the OLED display (SH1106 or SSD1306) if present on the I2C bus.
 * @return True if display was successfully detected and initialized, false otherwise.
 */
bool oledInit();

/**
 * @brief Checks if the OLED display is initialized and available.
 * @return True if display is available.
 */
bool isOledAvailable();

/**
 * @brief Displays the continuous monitoring start screen.
 * @param intervalSec Sampling interval in seconds.
 */
void oledShowMonitorStart(uint32_t intervalSec);

/**
 * @brief Updates the OLED screen with live telemetry during continuous monitoring.
 * @param tp Reference to TelemetryPayload containing the latest sensor readings.
 * @param dt RTC date and time.
 * @param intervalSec Sampling interval in seconds.
 */
void oledUpdateMonitor(const TelemetryPayload& tp, const RtcDateTime& dt, uint32_t intervalSec);

/**
 * @brief Displays the monitoring stopped notification.
 */
void oledShowMonitorStop();

/**
 * @brief Clears and powers off / sleeps the OLED display for deep sleep.
 */
void oledPowerOff();
