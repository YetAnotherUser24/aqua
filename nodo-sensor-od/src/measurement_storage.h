/**
 * @file measurement_storage.h
 * @brief Measurement acquisition, SD logging, and upload helpers.
 *
 * This module handles:
 *  - Sampling sensor and temperature.
 *  - Appending daily measurement records directly to SD (/YYYYMMDD.CSV).
 *  - Processing pending CSV files and uploading telemetry to AquaControl via HTTPS.
 *  - Experiment CSV logging (/EXP_XX.CSV).
 */

#pragma once
#include <Arduino.h>
#include <RtcDS3231.h>

/**
 * @brief Marks a CSV line as sent (updates last field to "1").
 *
 * @param line Original CSV line.
 * @return Updated line with sent-flag = 1.
 */
String markLineSent(const String& line);

/**
 * @brief Returns available SD storage in megabytes.
 */
uint32_t getSDFreeMB();

/**
 * @brief Normalizes module configuration (disallows invalid timings).
 */
void normalizeConfig();

/**
 * @brief Performs full measurement cycle and logs result.
 *
 * Includes:
 *  - Temperature reading from RTC
 *  - Sensor sampling via sensor engine
 *  - Status flags
 *  - Direct append to daily SD CSV (/YYYYMMDD.CSV) if SD present
 */
void takeMeasurement();

/**
 * @brief Sends all pending CSV files from SD to AquaControl.
 */
void sendSDPending();

/**
 * @brief Builds a filename of form /YYYYMMDD.CSV.
 */
void getFilename(const RtcDateTime& ts);

#ifdef SD_MODULE
/**
 * @brief Appends a single measurement row into the daily CSV file (/YYYYMMDD.CSV).
 */
void logDailyRow(uint32_t seconds2000, const int16_t* readings, int16_t temp, uint32_t status, uint8_t sentFlag);
#else
static inline void logDailyRow(uint32_t, const int16_t*, int16_t, uint32_t, uint8_t) {}
#endif

/**
 * @brief Parses one CSV line and extracts fields for upload.
 *
 * @param line Input CSV.
 * @param outSeconds2000 Extracted timestamp.
 * @param outReadings    Extracted sensor readings array.
 * @param outTemp        Extracted RTC temperature.
 * @param outStatus      Extracted status bitmask.
 * @return true if line was pending (sent=0), false otherwise.
 */
bool csvLineParse(const String& line, uint32_t& outSeconds2000,
                  int16_t* outReadings, int16_t& outTemp, uint32_t& outStatus);

/**
 * @brief Processes CSV file: sends unsent lines and rewrites the file.
 *
 * @return true if file still has pending lines, false if fully cleared.
 */
bool processCsvFile(const char* filename);

#ifdef SD_MODULE
/**
 * @brief Opens or creates a dedicated experiment CSV file (e.g. /EXP_01.CSV) on SD.
 * Writes RFC 4180 header if file is newly created or empty.
 * @param filename 8.3 FAT filename.
 * @return True if file opened successfully.
 */
bool startExperimentFile(const char* filename);

/**
 * @brief Appends a single measurement row into the active experiment CSV file.
 * Flushes to disk immediately to prevent data loss on power cuts.
 */
void logExperimentRow(uint32_t sec2000, int16_t* readings, int16_t rtcTempCenti, uint16_t battMv);

/**
 * @brief Flushes and closes the active experiment CSV file.
 */
void stopExperimentFile();

/**
 * @brief Checks if an experiment CSV file is currently open.
 */
bool isExperimentFileOpen();
#else
static inline bool startExperimentFile(const char*) { return false; }
static inline void logExperimentRow(uint32_t, int16_t*, int16_t, uint16_t) {}
static inline void stopExperimentFile() {}
static inline bool isExperimentFileOpen() { return false; }
#endif