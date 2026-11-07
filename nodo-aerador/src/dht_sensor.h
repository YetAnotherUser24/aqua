#pragma once

#include <Arduino.h>

/**
 * @brief Initializes the optional DHT11 ambient temperature and humidity sensor.
 * In virtual mode, sets up the software model.
 */
void dhtSensorInit();

/**
 * @brief Attempts to read ambient temperature (°C) and relative humidity (%).
 * 
 * Non-blocking for Core 1 (designed to run exclusively from Core 0 background worker).
 * Handles sensor disconnection / read errors gracefully without throwing fatal errors.
 * 
 * @param[out] outTempC Ambient temperature in degrees Celsius
 * @param[out] outHumidityPct Relative humidity percentage (0-100%)
 * @return true if valid reading obtained, false if sensor absent/error
 */
bool dhtSensorRead(float& outTempC, float& outHumidityPct);

/**
 * @brief Returns whether a physical or virtual DHT sensor is detected and responding.
 */
bool dhtSensorIsPresent();

