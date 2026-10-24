/**
 * @file power_sleep.cpp
 * @brief Implementation of low-power helpers for SD bus, radio shutdown,
 *        and deep-sleep transition.
 *
 * Feature guards:
 *  - #ifdef SD_MODULE: SD bus suspend/resume
 *  - #ifdef GPRS_MODULE: modem sleep call
 *  - WiFi/BT radio shutdown always included (needed for deep sleep)
 */

#include "power_sleep.h"

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <driver/gpio.h>
#include <esp_sleep.h>

#include "board.h"
#include "log.h"
#include "n_logger_config.h"
#include "oled_display.h"
#include "rtc_utils.h"
#include "sensor_engine.h"

#ifdef SD_MODULE
#include <SdFat.h>
#endif

/* ------------------------ External Objects from main.cpp ------------------- */

#ifdef SD_MODULE
extern SdFat SD;
extern SdFile sdFile;
extern SPIClass sdSPI;
#endif

extern ModuleConfig moduleCfg;

/* ------------------------------ SD Power Helpers ------------------------------ */

/**
 * @brief Suspends the SD SPI bus to prevent leakage during deep sleep.
 */
void sdBusSuspend() {
#ifdef SD_MODULE
  sdSPI.end();

  pinMode(SD_PIN_CS, OUTPUT);
  digitalWrite(SD_PIN_CS, HIGH);

  pinMode(SD_PIN_SCLK, INPUT);
  pinMode(SD_PIN_MOSI, INPUT);
  pinMode(SD_PIN_MISO, INPUT);

#if SD_EN_PIN != -1
  setPinInactive(SD_EN_PIN, SD_EN_ACTIVE_HIGH, true);
#endif
#else
  // No SD module — no-op
#endif
}

/**
 * @brief Resumes the SD bus after waking from deep sleep.
 */
void sdBusResume() {
#ifdef SD_MODULE
#if SD_EN_PIN != -1
  setPinActive(SD_EN_PIN, SD_EN_ACTIVE_HIGH);
  delay(5);
#endif
  sdSPI.begin(SD_PIN_SCLK, SD_PIN_MISO, SD_PIN_MOSI, SD_PIN_CS);
#else
  // No SD module — no-op
#endif
}

/* --------------------------- Radio Power Management --------------------------- */

/**
 * @brief Brings the WiFi radio down before deep sleep.
 *
 * Bluetooth is not a factor on this node — BLE was removed from the firmware entirely, so no BT
 * controller is initialised and there is nothing to stop here. Earlier this also called
 * `btStop()`/`esp_bt_controller_disable()`, which pulled the whole Bluetooth stack into the
 * image for a feature the node does not have.
 */
void boardRadiosOff() {
  WiFi.mode(WIFI_OFF);
}

/* ------------------------------- Deep Sleep Prep ------------------------------ */

/**
 * @brief Prepares all peripherals and enters ESP32 deep sleep.
 *
 * Operations performed:
 *  - Validate interval (fallback to 1 min)
 *  - Suspend SD
 *  - Disable sensor and UART
 *  - Disable LEDs
 *  - Configure DS3231 alarm
 *  - Enable EXT1 wakeup (same mask as user sketch)
 *  - Call `esp_deep_sleep_start()`
 */
void prepareForDeepSleep(int minutes) {
  int interval = (minutes <= 0) ? 1 : minutes;

  oledPowerOff();

  sdBusSuspend();

  digitalWrite(LED_PIN, LOW);

  // Lock sensor power/control pins inactive through deep sleep with GPIO hold enabled
#ifdef RS485_EN_PIN
  setPinInactive(RS485_EN_PIN, RS485_EN_ACTIVE_HIGH, true);
#endif
  setPinInactive(OD_PWR_PIN, OD_PWR_ACTIVE_HIGH, true);
  setPinInactive(OD_DE_RE_PIN, true, true);
  setPinInput(OD_RX_PIN);
  setPinInput(OD_TX_PIN);

#ifdef DEBUG
  Logf("\nWAKE UP IN %d MIN\n", moduleCfg.measure_time);
#endif

  // Ensure wakeup pin is input (RTC EXT1 handles it during deep sleep,
  // but explicitly setting INPUT prevents leakage from floating state)
  setPinInput(WAKEUP_PIN);

  configureAlarm(interval);

  esp_sleep_enable_ext1_wakeup(WAKEUP_PIN_BITMASK, ESP_EXT1_WAKEUP_ALL_LOW);
  // ponytail: timer wakeup ensures bench devkits without DS3231 INT wiring still wake up reliably
  esp_sleep_enable_timer_wakeup((uint64_t)interval * 60 * 1000000ULL);


  // Double blink LED to visually signal transition to deep sleep
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(150);
  digitalWrite(LED_PIN, LOW);
  delay(150);
  digitalWrite(LED_PIN, HIGH);
  delay(150);
  digitalWrite(LED_PIN, LOW);
  delay(150);

  Serial.flush();

  esp_deep_sleep_start();
}
