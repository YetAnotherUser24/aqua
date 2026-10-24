/**
 * @file sensor_engine.h
 * @brief Sensor measurement layer for Aqualabo DIGISENS Optical Dissolved Oxygen (OD) logger.
 *
 * Provides functions to collect all sensor readings into TelemetryPayload:
 *   tp.sensorReadings[0] = Water Temperature (centi-°C)
 *   tp.sensorReadings[1] = DO Saturation (deci-%Sat)
 *   tp.sensorReadings[2] = Dissolved Oxygen (milli-mg/L)
 *   tp.sensorReadings[3] = Parameter 3 (centi)
 *   tp.sensorReadings[4] = Parameter 4 (centi)
 *   tp.sensorReadings[5] = Battery Voltage (mV, ADC)
 */

#pragma once
#include "n_logger_config.h"
#include <Arduino.h>

/**
 * @brief Performs a full measurement cycle on the OD sensor and battery monitor.
 *
 * Sequence:
 *   1. Powers on RS-485 bus and OD sensor
 *   2. Writes Modbus measurement trigger (0x001F -> reg 0x0001)
 *   3. Waits sampling delay (from reg 0x00A4 or 250ms default)
 *   4. Reads 10 holding registers starting at 0x0053 (5 IEEE 754 floats)
 *   5. Decodes Temp, DO, Sat, P3, P4 into tp.sensorReadings[0..4]
 *   6. Samples battery (ADC) into tp.battery and tp.sensorReadings[5]
 *   7. Powers down RS-485 and sensor power rails
 *
 * @param tp Telemetry payload to fill with readings.
 */
void sensorReadAll(TelemetryPayload& tp);

/**
 * @brief Takes a single real-time measurement sample from the OD sensor for the live monitor preview.
 * @return Dissolved Oxygen in milli-mg/L (e.g. 8456 for 8.456 mg/L), or negative error code.
 */
int16_t readPrimarySensorRealtime();

/**
 * @brief Powers off and closes serial bus for real-time sensor sampling.
 */
void stopPrimarySensorRealtime();
