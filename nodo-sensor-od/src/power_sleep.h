/**
 * @file power_sleep.h
 * @brief Low-power helpers: SD bus suspension, radio shutdown, and deep-sleep entry.
 *
 * These functions prepare the board peripherals (SD bus, UART, sensor power, alarms)
 * before entering ESP32 deep sleep. Logic and naming remain exactly as the original.
 */

#pragma once
#include <Arduino.h>

/**
 * @brief Places the SD SPI bus into a safe low-power state.
 *
 * Turns off SPI, releases SCLK/MISO/MOSI, and de-selects CS to avoid leakage.
 */
void sdBusSuspend();

/**
 * @brief Restores the SD SPI bus after sleep.
 *
 * Reinitializes SPI pins so the SD card can be accessed normally.
 */
void sdBusResume();

/**
 * @brief Turns off WiFi/BT controllers to reduce power consumption.
 */
void boardRadiosOff();

/**
 * @brief Prepares the device to enter deep sleep.
 *
 * Steps performed:
 *  - Compute next wake interval.
 *  - Suspend SD bus.
 *  - Disable sensor power and UART.
 *  - Configure RTC alarm.
 *  - Enable EXT1 wakeup.
 *  - Start ESP deep sleep.
 *
 * @param minutes Sleep interval in minutes. If ≤0, defaults to 1 minute.
 */
void prepareForDeepSleep(int minutes);