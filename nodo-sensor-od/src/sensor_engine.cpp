/**
 * @file sensor_engine.cpp
 * @brief Sensor measurement layer for the OD-Logger.
 *
 * All hardware access goes through the injected device bus (`sensor_bus.h`), so this file is
 * identical in real and virtual builds: it sequences the OD samples, maps them into the
 * TelemetryPayload, samples the battery, and maintains the status bits. The device itself
 * (OPTOD over RS-485 vs OdSim, ADC vs virtual) is chosen at link time by the factory.
 */

#include "sensor_engine.h"
#include "sensor_bus.h"
#include "log.h"
#include "n_logger_config.h"
#include "wdt_feed.h"

#include <Arduino.h>

extern bool webMonitorActive;
extern bool experimentActive;

/* ==================================================================
 *  Full measurement cycle
 * ================================================================== */

void sensorReadAll(TelemetryPayload& tp) {
  uint8_t readingIndex = 0;
  const bool isContinuous = webMonitorActive || experimentActive;

  IOdSensor& sensor = odSensor();

  // 1. Ensure the sensor rails are energized.
  sensor.powerOn();

  // 2. Collect OD_SAMPLE_COUNT samples (retry until timeout).
  uint8_t nread = 0;
  const unsigned long sampleStart = millis();
  const unsigned long kSampleTimeoutMs = 5000;

  while (nread < OD_SAMPLE_COUNT && (millis() - sampleStart) < kSampleTimeoutMs) {
    // A full cycle can exceed the Task WDT timeout when the sensor is unresponsive
    // (warmup + sampling timeout): keep feeding it each pass.
    wdtFeed();

    OdReading r;
    if (sensor.read(r, millis()) && r.valid) {
      tp.sensorReadings[readingIndex++] = r.tempCenti;
      tp.sensorReadings[readingIndex++] = r.satDeciPct;
      tp.sensorReadings[readingIndex++] = r.doMilliMgL;
      tp.sensorReadings[readingIndex++] = r.p3Centi;
      tp.sensorReadings[readingIndex++] = r.p4Centi;
      nread++;
      if (nread < OD_SAMPLE_COUNT) wdtDelay(200);
    } else {
      wdtDelay(100);
    }
  }

  if (nread == 0) {
    Logf("[OD] Incomplete: %u/%u samples obtained\n", (unsigned)nread, (unsigned)OD_SAMPLE_COUNT);
    tp.status = changeBit(tp.status, STATUS_OD_SENSOR_BIT, true);
    for (uint8_t i = 0; i < OD_NUM_READINGS; i++) {
      tp.sensorReadings[readingIndex++] = 0;
    }
  }

  // 3. Power down when not continuously monitoring, to save battery.
  if (!isContinuous) {
    sensor.powerOff();
  }

  // 4. Battery (ADC-only platform).
  IBatteryMonitor& battery = batteryMonitor();
  tp.battery = battery.readMilliVolts();
  tp.sensorReadings[readingIndex++] = (int16_t)(tp.battery & 0xFFFF);
  Logf("[BAT] %u mV\n", (unsigned)tp.battery);

  // 5. Pad any remaining slots.
  while (readingIndex < MAX_SENSOR_READINGS) {
    tp.sensorReadings[readingIndex++] = 0;
  }
}

/* ==================================================================
 *  Real-time DO preview (monitor)
 * ================================================================== */

int16_t readPrimarySensorRealtime() {
  IOdSensor& sensor = odSensor();
  sensor.powerOn();

  OdReading r;
  if (sensor.read(r, millis()) && r.valid) {
    return r.doMilliMgL;  // milli-mg/L (e.g. 8456 == 8.456 mg/L)
  }
  return -1;
}

void stopPrimarySensorRealtime() {
  odSensor().powerOff();
}
