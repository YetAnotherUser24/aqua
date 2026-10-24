/**
 * @file measurement_storage.cpp
 * @brief Measurement capture, EEPROM buffering, SD batching and telemetry upload.
 *
 * This module centralizes:
 *  - Sensor sampling and temperature/battery acquisition.
 *  - Compact EEPROM page storage.
 *  - Dumping buffered pages to SD.
 *  - Converting CSV records to JSON for ThingsBoard.
 *  - Uploading pending data from SD and EEPROM.
 *
 * Feature guards:
 *  - #ifdef SD_MODULE: SD card operations (dump, write, free space)
 *  - #ifdef GPRS_MODULE: MQTT/network operations (publish, upload)
 *
 * All functionality is preserved exactly as in original implementation.
 */

#include <Arduino.h>
#include <HardwareSerial.h>

#include "board.h"
#include "measurement_storage.h"
#include "n_logger_config.h"
#include "wifi_http.h"
#include "esp_now_sender.h"
#include "rtc_utils.h"
#include "sensor_engine.h"
#include "wdt_feed.h"

#include "config.h"
#include "log.h"

#include <ArduinoJson.h>
#include <EEPROM.h>
#include <Wire.h>

#ifdef SD_MODULE
#include <SdFat.h>
#endif

/* --------------------------------------------------------------------------
 *  External objects and configuration (declared in main.cpp)
 * -------------------------------------------------------------------------- */

#ifndef SerialMon
#define SerialMon Serial
#endif

extern ModuleConfig moduleCfg;
extern TelemetryPayload telemetryPayload;


#ifdef SD_MODULE
extern SdFat SD;
extern SdFile sdFile;
extern SPIClass sdSPI;
#endif
extern char csvFilename[24];
extern bool fileOpen;

extern RtcDS3231<TwoWire> rtc;
extern RtcDateTime rtcNow;
extern char dateStr[20];

extern bool wifiAvailable;

/* --------------------------------------------------------------------------
 *  SD / EEPROM Helpers (unchanged logic, documented)
 * -------------------------------------------------------------------------- */

String markLineSent(const String& line) {
  String s = line;

  while (s.length() && s[s.length() - 1] == ' ')
    s.remove(s.length() - 1);

  int lastComma = s.lastIndexOf(',');
  if (lastComma <= 0)
    return line;

  return s.substring(0, lastComma) + ", 1";
}


/**
 * @brief Returns available SD capacity (MB).
 */
uint32_t getSDFreeMB() {
#ifdef SD_MODULE
  if (!SD.begin(SdSpiConfig(SD_PIN_CS, SHARED_SPI, SD_SCK_MHZ(20), &sdSPI))) {
    return 0;
  }

  FsVolume* vol = SD.vol();
  if (!vol)
    return 0;

  uint64_t totalBytes = (uint64_t)vol->clusterCount() * (uint64_t)vol->sectorsPerCluster() * 512ULL;

  SdFile dir, entry;
  uint64_t usedBytes = 0;

  if (dir.open("/")) {
    while (entry.openNext(&dir, O_READ)) {
      if (!entry.isDir())
        usedBytes += entry.fileSize();
      entry.close();
    }
    dir.close();
  }

  uint64_t freeBytes = (totalBytes > usedBytes) ? (totalBytes - usedBytes) : 0;
  return freeBytes / (1024ULL * 1024ULL);
#else
  return 0;
#endif
}

/**
 * @brief Ensures valid timing values in module configuration.
 */
void normalizeConfig() {
  if (moduleCfg.measure_time <= 0)
    moduleCfg.measure_time = 1;

  if (moduleCfg.GPRS_time < 0)
    moduleCfg.GPRS_time = 0;
}

/* --------------------------------------------------------------------------
 *  Measurement Capture and EEPROM Packing (takeMeasurement)
 * -------------------------------------------------------------------------- */

void takeMeasurement() {
  // La medición puede superar el timeout del Task WDT (5 s) si el sensor OD no responde.
  wdtFeed();
  /**
   * Reset telemetry structure.
   */
  telemetryPayload.freeStorage = 0;
  telemetryPayload.temperature = 0;
  for (int i = 0; i < MAX_SENSOR_READINGS; i++)
    telemetryPayload.sensorReadings[i] = 0;
#if defined(SENSOR_BATTERY_ADC)
  telemetryPayload.battery = 0;
#endif
  telemetryPayload.status = 0x00000000;

  rtcNow = getRtcDateTime();
  if (isRtcAvailable()) {
    for (int retry = 0; retry < 3 && rtcNow.Year() < 2020; retry++) {
      delay(20);
      rtcNow = rtc.GetDateTime();
    }
  }
  uint32_t secondsSince2000 = rtcNow.TotalSeconds();

  /* Temperature from RTC (always included if RTC present) */
  int16_t tempCenti = 0;
  if (isRtcAvailable()) {
    tempCenti = rtc.GetTemperature().AsCentiDegC();
    if (tempCenti == 0 && rtcNow.Year() >= 2020) {
      delay(10);
      tempCenti = rtc.GetTemperature().AsCentiDegC();
    }
    if (tempCenti < -4000 || tempCenti > 8500)
      telemetryPayload.status = changeBit(telemetryPayload.status, STATUS_TEMPERATURE_BIT, true);
  } else {
    tempCenti = 2500; // 25.0 C ambient fallback for bare ESP32 / virtual mode
  }
  telemetryPayload.temperature = tempCenti;

  /* Sensor sampling via sensor engine (handles all enabled sensors) */
  sensorReadAll(telemetryPayload);

#if !defined(DISABLE_ESPNOW)
  /* Broadcast real-time DO packet via ESP-NOW to ODrive Motor Controller */
  static uint32_t espNowSeqCounter = 0;
  OdTelemetryPacket espNowPkt;
  espNowPkt.magic = AQUACONTROL_PACKET_MAGIC;
  espNowPkt.sequence = ++espNowSeqCounter;
  // ADR-3: el Reloj Maestro publica epoch Unix UTC en ms (misma base que los nodos NTP),
  // para que el ODrive y los actuadores alineen sus series sin desfase horario.
  espNowPkt.t_dev_ms =
      ((uint64_t)secondsSince2000 + UNIX_OFFSET_2000 + LOCAL_TZ_OFFSET_SEC) * 1000ULL
      + (uint64_t)(millis() % 1000);
  espNowPkt.water_temperature = telemetryPayload.sensorReadings[0] / 100.0f;  // centi-C to C
  espNowPkt.oxygen_saturation = telemetryPayload.sensorReadings[1] / 10.0f;   // deci-% to %
  espNowPkt.dissolved_oxygen = telemetryPayload.sensorReadings[2] / 1000.0f; // milli-mg/L to mg/L
  espNowPkt.battery_voltage = telemetryPayload.battery / 1000.0f;             // mV to V
  espNowPkt.status_flags = telemetryPayload.status;
  espNowBroadcastDO(espNowPkt);
#endif


  /* Low-battery emergency config update (battery populated by sensor engine) */
#if defined(SENSOR_BATTERY_ADC)
  if (telemetryPayload.battery < 3400) {
    moduleCfg.measure_time = 60;
    moduleCfg.GPRS_time = 1440;

    EEPROM.begin(sizeof(ModuleConfig));
    EEPROM.put(0, moduleCfg);
    EEPROM.commit();
    EEPROM.end();
  }
#endif

#ifdef DEBUG
  formatDateTime(rtcNow);
  Logf("========== MEASUREMENT TAKEN ==========\n");
  Logf("   Timestamp: %lu (%s %s)\n", (unsigned long)secondsSince2000, dateStr, LOCAL_TZ_NAME);
#if defined(SENSOR_BATTERY_ADC)
  Logf("   Battery: %u mV\n", telemetryPayload.battery);
#endif
  Logf("   Temperature: %.2f C\n", tempCenti / 100.0f);
  Logf("   Sensor Readings: [%d, %d, %d, %d, %d]\n",
       telemetryPayload.sensorReadings[0],
       telemetryPayload.sensorReadings[1],
       telemetryPayload.sensorReadings[2],
       telemetryPayload.sensorReadings[3],
       telemetryPayload.sensorReadings[4]);
  Logf("   Status: 0x%08X\n", (unsigned int)telemetryPayload.status);
  Logf("==========================================\n");
#endif

#ifdef SD_MODULE
  wdtFeed();
  logDailyRow(secondsSince2000, telemetryPayload.sensorReadings, tempCenti, telemetryPayload.status, FLAG_NOT_SENT);
  wdtFeed();
#endif

  error(1, LED_PIN);
}

/* --------------------------------------------------------------------------
 *  Upload routines: SD pending + EEPROM pending (via WiFi HTTPS)
 * -------------------------------------------------------------------------- */

void sendSDPending() {
#if defined(WIFI_MODULE) && defined(SD_MODULE)
  if (!wifiAvailable) {
    Logln("WiFi unavailable, skipping SD uploads.");
    return;
  }

  SdFile dir, entry;
  if (!SD.begin(SdSpiConfig(SD_PIN_CS, SHARED_SPI, SD_SCK_MHZ(20), &sdSPI)))
    return;

  if (!dir.open("/"))
    return;

  char name[32];
  uint16_t stillPending = 0;

  while (entry.openNext(&dir, O_READ)) {
    if (!entry.isDir()) {
      entry.getName(name, sizeof(name));
      String fname = name;

      // Process CSV files that match /YYYYMMDD.CSV
      if (fname.endsWith(".CSV") && fname.startsWith("20")) {
        String full = String("/") + fname;
        bool pending = processCsvFile(full.c_str());
        if (pending)
          stillPending++;
      }
    }
    entry.close();
  }

  dir.close();
#else
  (void)0;
#endif
}


/* --------------------------------------------------------------------------
 *  CSV filename: /YYYYMMDD.CSV (no pending _P prefix)
 * -------------------------------------------------------------------------- */

void getFilename(const RtcDateTime& ts) {
  int y = ts.Year();
  int m = ts.Month();
  int d = ts.Day();

  csvFilename[0] = '/';
  csvFilename[1] = '2';
  csvFilename[2] = '0';
  csvFilename[3] = (y - 2000) / 10 + '0';
  csvFilename[4] = y % 10 + '0';
  csvFilename[5] = m / 10 + '0';
  csvFilename[6] = m % 10 + '0';
  csvFilename[7] = d / 10 + '0';
  csvFilename[8] = d % 10 + '0';
  csvFilename[9] = '.';
  csvFilename[10] = 'C';
  csvFilename[11] = 'S';
  csvFilename[12] = 'V';
  csvFilename[13] = '\0';
}

#ifdef SD_MODULE
static void writeCsvHeader(SdFile& file) {
  file.print(F("datetime, seconds_since_2000"));
  file.print(F(", water_temp_centi, do_sat_deci_pct, do_milli_mg_l, param3_centi, param4_centi"));
#if defined(SENSOR_BATTERY_ADC)
  file.print(F(", battery_mv"));
#endif
  file.println(F(", rtc_temp_centi, status, sent"));
  file.flush();
}
#endif

#ifdef SD_MODULE
/**
 * @brief Appends a single measurement row into the daily CSV file (/YYYYMMDD.CSV).
 */
void logDailyRow(uint32_t seconds2000, const int16_t* readings, int16_t temp, uint32_t status, uint8_t sentFlag) {
  if (!SD.begin(SdSpiConfig(SD_PIN_CS, SHARED_SPI, SD_SCK_MHZ(20), &sdSPI))) {
    Logln("[SD] begin failed for daily log");
    return;
  }

  RtcDateTime ts(seconds2000);
  static uint32_t dayCache = 40;

  if (!fileOpen || dayCache != ts.Day()) {
    if (fileOpen) {
      sdFile.close();
      fileOpen = false;
    }
    getFilename(ts);
    fileOpen = sdFile.open(csvFilename, O_CREAT | O_WRITE | O_APPEND);
    if (fileOpen && sdFile.fileSize() == 0) {
      writeCsvHeader(sdFile);
    }
    dayCache = ts.Day();
  }

  if (!fileOpen) {
    Logln("[SD] Failed to open daily CSV file");
    return;
  }

  formatDateTime(ts);
  sdFile.print(dateStr);
  sdFile.print(", ");
  sdFile.print(seconds2000);

  for (uint16_t k = 0; k < MAX_SENSOR_READINGS; k++) {
    sdFile.print(", ");
    sdFile.print(readings[k]);
  }

  sdFile.print(", ");
  sdFile.print(temp);

  sdFile.print(", ");
  sdFile.print(status);

  sdFile.print(", ");
  sdFile.print(sentFlag);

  sdFile.println();
  sdFile.flush();
}
#endif


/* --------------------------------------------------------------------------
 *  csvLineParse() — CSV field extraction
 *
 *  CSV format: dateStr, seconds2000, reading[0..N-1], temperature, status, sent
 *  Extracts raw fields for publishToAquaControl().
 * -------------------------------------------------------------------------- */

bool csvLineParse(const String& line, uint32_t& outSeconds2000,
                  int16_t* outReadings, int16_t& outTemp, uint32_t& outStatus) {
  if (line.length() < 10)
    return false;

  // Skip CSV header line (starts with non-digit character)
  if (!isdigit(line.charAt(0)))
    return false;

  int idx = 0, start = 0, field = 0;
  int values[MAX_SENSOR_READINGS + 2];
  uint32_t status = 0;
  int sent = 0;
  uint32_t seconds2000 = 0;

  while (true) {
    idx = line.indexOf(',', start);
    String token = (idx == -1) ? line.substring(start) : line.substring(start, idx);
    token.trim();

    if (field == 1)
      seconds2000 = token.toInt();
    else if (field >= 2 && field <= (1 + MAX_SENSOR_READINGS))
      values[field - 2] = token.toInt();
    else if (field == (2 + MAX_SENSOR_READINGS))
      values[MAX_SENSOR_READINGS] = token.toInt();
    else if (field == (3 + MAX_SENSOR_READINGS))
      status = token.toInt();
    else if (field == (4 + MAX_SENSOR_READINGS))
      sent = token.toInt();

    field++;
    if (idx == -1)
      break;
    start = idx + 1;
  }

  if (field < (5 + MAX_SENSOR_READINGS))
    return false;

  outSeconds2000 = seconds2000;
  for (int i = 0; i < MAX_SENSOR_READINGS; i++)
    outReadings[i] = (int16_t)values[i];
  outTemp = (int16_t)values[MAX_SENSOR_READINGS];
  outStatus = status;

  return (sent == 0);
}

/* --------------------------------------------------------------------------
 *  processCsvFile() — Reads CSV file, publishes unsent lines, rewrites in place
 * -------------------------------------------------------------------------- */

bool processCsvFile(const char* filename) {
#ifdef SD_MODULE
  SdFile inFile, outFile;

  char tmpName[28];
  snprintf(tmpName, sizeof(tmpName), "%s.tmp", filename);

  if (!inFile.open(filename, O_READ))
    return true;

  if (!outFile.open(tmpName, O_CREAT | O_TRUNC | O_WRITE)) {
    inFile.close();
    return true;
  }

  bool pendingLeft = false;
  String line;

  while (inFile.available()) {
    int c = inFile.read();

    if (c == '\r')
      continue;
    if (c == '\n') {
      uint32_t sec2000 = 0;
      int16_t csvReadings[MAX_SENSOR_READINGS];
      int16_t csvTemp = 0;
      uint32_t csvStatus = 0;

      String newLine = line;
      bool wasPending = csvLineParse(line, sec2000, csvReadings, csvTemp, csvStatus);

      if (wasPending) {
        if (publishToAquaControl(sec2000, csvReadings, csvTemp, csvStatus)) {
          newLine = markLineSent(line);
        } else {
          pendingLeft = true;
        }
      }

      outFile.print(newLine);
      outFile.print("\n");
      line = "";
    } else {
      line += (char)c;
    }
  }

  if (line.length() > 0) {
    uint32_t sec2000 = 0;
    int16_t csvReadings[MAX_SENSOR_READINGS];
    int16_t csvTemp = 0;
    uint32_t csvStatus = 0;
    String newLine = line;

    bool wasPending = csvLineParse(line, sec2000, csvReadings, csvTemp, csvStatus);

    if (wasPending) {
      if (publishToAquaControl(sec2000, csvReadings, csvTemp, csvStatus)) {
        newLine = markLineSent(line);
      } else {
        pendingLeft = true;
      }
    }

    outFile.print(newLine);
    outFile.print("\n");
  }

  outFile.flush();
  inFile.close();
  outFile.close();

  // Replace original with updated file
  SD.remove(filename);
  SD.rename(tmpName, filename);

  return pendingLeft;
#else
  return true;
#endif
}

/* --------------------------------------------------------------------------
 *  Experiment File Lifecycle (startExperimentFile, logExperimentRow, stopExperimentFile)
 * -------------------------------------------------------------------------- */

#ifdef SD_MODULE
static SdFile s_expFile;
static bool s_expFileOpen = false;

bool startExperimentFile(const char* filename) {
  if (s_expFileOpen) {
    s_expFile.flush();
    s_expFile.close();
    s_expFileOpen = false;
  }

#if defined(SD_EN_PIN) && SD_EN_PIN != -1
  setPinActive(SD_EN_PIN, SD_EN_ACTIVE_HIGH);
  delay(10);
#endif

  if (!SD.begin(SdSpiConfig(SD_PIN_CS, SHARED_SPI, SD_SCK_MHZ(20), &sdSPI))) {
    Logln("[EXP] SD.begin failed for experiment file");
    return false;
  }

  char cleanPath[24];
  if (filename[0] == '/') {
    strncpy(cleanPath, filename, sizeof(cleanPath) - 1);
  } else {
    snprintf(cleanPath, sizeof(cleanPath), "/%s", filename);
  }
  cleanPath[sizeof(cleanPath) - 1] = '\0';

  bool exists = SD.exists(cleanPath);
  if (!s_expFile.open(cleanPath, O_RDWR | O_CREAT | O_AT_END)) {
    Logf("[EXP] Failed to open experiment file: %s\n", cleanPath);
    return false;
  }

  s_expFileOpen = true;

  // If new or empty file, write RFC 4180 CSV header
  if (!exists || s_expFile.fileSize() == 0) {
    s_expFile.println(F("RTC_ms,Timestamp,RTC_Sec,DO_mg_L,Sat_Pct,Temp_C,Batt_V"));
    s_expFile.flush();
  }

  Logf("[EXP] Started experiment file: %s (size: %lu bytes)\n", cleanPath, (unsigned long)s_expFile.fileSize());
  return true;
}

void logExperimentRow(uint32_t sec2000, int16_t* readings, int16_t rtcTempCenti, uint16_t battMv) {
  if (!s_expFileOpen) return;

  uint64_t unixTs = (uint64_t)sec2000 + UNIX_OFFSET_2000;
  time_t t = (time_t)unixTs;
  struct tm* tm = gmtime(&t);
  char isoBuf[32];
  snprintf(isoBuf, sizeof(isoBuf), "%04d-%02d-%02dT%02d:%02d:%02d-05:00",
           tm->tm_year + 1900, tm->tm_mon + 1, tm->tm_mday,
           tm->tm_hour, tm->tm_min, tm->tm_sec);

  float doVal = readings[2] / 1000.0f;
  float satVal = readings[1] / 10.0f;
  float tempVal = readings[0] / 100.0f;
  float battV = battMv / 1000.0f;

  // ADR-3: primera columna = epoch Unix UTC en ms (alineable con la telemetría bulk del ODrive).
  uint64_t rtcMs = (unixTs + LOCAL_TZ_OFFSET_SEC) * 1000ULL + (uint64_t)(millis() % 1000);

  s_expFile.printf("%llu,%s,%lu,%.3f,%.2f,%.2f,%.2f\r\n",
                   (unsigned long long)rtcMs, isoBuf, (unsigned long)sec2000,
                   doVal, satVal, tempVal, battV);
  s_expFile.flush(); // Flush each row immediately to prevent data loss
}

void stopExperimentFile() {
  if (s_expFileOpen) {
    s_expFile.flush();
    s_expFile.close();
    s_expFileOpen = false;
    Logln("[EXP] Experiment file closed.");
  }
}

bool isExperimentFileOpen() {
  return s_expFileOpen;
}
#endif