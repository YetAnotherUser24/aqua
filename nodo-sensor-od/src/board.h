/**
 * @file board.h
 * @brief Board pin definitions and peripheral isolation for n-logger platform.
 *
 * Board: LilyGO T3 LoRa32 v1.6 + custom instrumentation HAT (thesis bench).
 * Selected with -DBOARD_LILYGO_LORA32 (the only supported board; it is also the
 * fallback when no flag is given).
 */

#pragma once

#include <Arduino.h>
#include <SPI.h>
#include <driver/gpio.h>

// Only the LilyGO T3 LoRa32 is supported; it is also the default.
#ifndef BOARD_LILYGO_LORA32
#define BOARD_LILYGO_LORA32
#endif

/* ==========================================================================
 *  Pin Power & Polarity Helper Functions
 * ========================================================================== */

/**
 * @brief Sets an enable/power control pin to its ACTIVE (ON) state.
 * @param pin GPIO pin number (-1 if pin is not used/present on board).
 * @param activeHigh True if pin is active-HIGH, false if active-LOW.
 */
inline void setPinActive(int pin, bool activeHigh) {
  if (pin != -1) {
    if (GPIO_IS_VALID_OUTPUT_GPIO(pin)) {
      gpio_hold_dis((gpio_num_t)pin);
    }
    pinMode(pin, OUTPUT);
    digitalWrite(pin, activeHigh ? HIGH : LOW);
  }
}

/**
 * @brief Sets a GPIO pin to INPUT mode safely.
 * @param pin GPIO pin number (-1 if pin is not used/present on board).
 */
inline void setPinInput(int pin) {
  if (pin != -1) {
    pinMode(pin, INPUT);
  }
}

/**
 * @brief Sets an enable/power control pin to its INACTIVE (OFF) state.
 * @param pin GPIO pin number (-1 if pin is not used/present on board).
 * @param activeHigh True if pin is active-HIGH, false if active-LOW.
 * @param holdInSleep If true, applies gpio_hold_en to retain pin state during deep sleep.
 */
inline void setPinInactive(int pin, bool activeHigh, bool holdInSleep = false) {
  if (pin != -1) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, activeHigh ? LOW : HIGH);
    if (holdInSleep && GPIO_IS_VALID_OUTPUT_GPIO(pin)) {
      gpio_hold_en((gpio_num_t)pin);
    }
  }
}

/* ==========================================================================


/* ==========================================================================
 *  BOARD: LilyGO T3 LoRa32 + Custom Instrumentation HAT
 * ========================================================================== */
#if defined(BOARD_LILYGO_LORA32)

#define BOARD_NAME "LilyGO T3 LoRa32 + HAT"

#ifndef LED_PIN
#define LED_PIN -1
#endif

#ifndef WAKEUP_PIN
#define WAKEUP_PIN GPIO_NUM_34
#endif

#ifndef WAKEUP_PIN_BITMASK
#define WAKEUP_PIN_BITMASK (1ULL << WAKEUP_PIN)
#endif

/* Power Enable Control Pins */
#ifndef EN_5V_PIN
#define EN_5V_PIN -1
#endif
#ifndef EN_5V_ACTIVE_HIGH
#define EN_5V_ACTIVE_HIGH true
#endif

/* LoRa SX1278 Dedicated SPI Controller (VSPI / SPI3) */
#ifndef LORA_SPI_HOST
#define LORA_SPI_HOST VSPI
#endif

#ifndef LORA_CS_PIN
#define LORA_CS_PIN 18
#endif

#ifndef LORA_PIN_SCLK
#define LORA_PIN_SCLK 5
#endif

#ifndef LORA_PIN_MISO
#define LORA_PIN_MISO 19
#endif

#ifndef LORA_PIN_MOSI
#define LORA_PIN_MOSI 27
#endif

#ifndef LORA_RST_PIN
#define LORA_RST_PIN 23
#endif

#ifndef LORA_DIO0_PIN
#define LORA_DIO0_PIN 26
#endif


/* Main I2C Bus Pins (DS3231MZ + OLED SSD1306) */
#ifndef I2C_SDA_PIN
#define I2C_SDA_PIN 21
#endif
#ifndef I2C_SCL_PIN
#define I2C_SCL_PIN 22
#endif
#ifndef I2C_EN_PIN
#define I2C_EN_PIN -1
#endif
#ifndef I2C_EN_ACTIVE_HIGH
#define I2C_EN_ACTIVE_HIGH true
#endif

/* OLED Display Configuration (0.96" SSD1306 onboard) */
#ifndef OLED_CONTROLLER_SSD1306
#define OLED_CONTROLLER_SSD1306
#endif
#ifndef OLED_COLUMN_OFFSET
#define OLED_COLUMN_OFFSET 0x00
#endif
#ifndef OLED_TYPE_NAME
#define OLED_TYPE_NAME "0.96\" SSD1306"
#endif
#ifndef OLED_RST_PIN
#define OLED_RST_PIN -1
#endif

/* RS-485 Modbus RTU Pins (SP3485EN on HAT) */
#ifndef OD_RX_PIN
#define OD_RX_PIN 39
#endif
#ifndef OD_TX_PIN
#define OD_TX_PIN 4
#endif
#ifndef OD_DE_RE_PIN
#define OD_DE_RE_PIN 0
#endif
#ifndef OD_PWR_PIN
#define OD_PWR_PIN 25
#endif
#ifndef OD_PWR_ACTIVE_HIGH
#define OD_PWR_ACTIVE_HIGH true
#endif

/* Onboard MicroSD Dedicated SPI Controller (HSPI / SPI2) */
#ifndef SD_SPI_HOST
#define SD_SPI_HOST HSPI
#endif

#ifndef SD_PIN_CS
#define SD_PIN_CS 13
#endif

#ifndef SD_PIN_SCLK
#define SD_PIN_SCLK 14
#endif

#ifndef SD_PIN_MISO
#define SD_PIN_MISO 2
#endif

#ifndef SD_PIN_MOSI
#define SD_PIN_MOSI 15
#endif

#ifndef SD_EN_PIN
#define SD_EN_PIN -1
#endif
#ifndef SD_EN_ACTIVE_HIGH
#define SD_EN_ACTIVE_HIGH true
#endif

/* RTC DS3231MZ Interrupt Pin */
#ifndef RTC_INT_PIN
#define RTC_INT_PIN 34
#endif

/* Battery ADC Pin */
#ifndef BOARD_BAT_ADC_PIN
#define BOARD_BAT_ADC_PIN 36
#endif

#endif

/* ==========================================================================
 *  Fallback Defaults for Display Configuration
 * ========================================================================== */
#ifndef OLED_COLUMN_OFFSET
#if defined(OLED_CONTROLLER_SSD1306)
#define OLED_COLUMN_OFFSET 0x00
#else
#define OLED_COLUMN_OFFSET 0x02
#endif
#endif

#ifndef OLED_TYPE_NAME
#if defined(OLED_CONTROLLER_SSD1306)
#define OLED_TYPE_NAME "0.96\" SSD1306"
#else
#define OLED_TYPE_NAME "1.3\" SH1106"
#endif
#endif

#ifndef OLED_RST_PIN
#define OLED_RST_PIN -1
#endif

#ifndef SD_SPI_HOST
#define SD_SPI_HOST HSPI
#endif

#ifndef LORA_SPI_HOST
#define LORA_SPI_HOST VSPI
#endif

/* ==========================================================================
 *  Board Hardware Initialization & Power Control
 * ========================================================================== */

/**
 * @brief Displays a repeated blink pattern to indicate error codes.
 */
inline void error(int errorNum, int errPin = LED_PIN) {
  if (errPin < 0) return;
  for (int i = 0; i < errorNum; i++) {
    digitalWrite(errPin, HIGH);
    delay(150);
    digitalWrite(errPin, LOW);
    delay(150);
  }
}

/**
 * @brief Initializes board status LEDs and puts all board power rails into low-power inactive
 * state.
 *
 * Safe for any board revision (if a pin is set to -1 or missing, it is safely ignored).
 * Active HIGH / LOW polarities are handled automatically per pin.
 * Call at the top of setup().
 */
inline void boardInit() {
  if (LED_PIN >= 0) {
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
  }

#ifdef BOARD_POWERON_PIN
  setPinInactive(BOARD_POWERON_PIN, true);
#endif
#ifdef BOARD_PWRKEY_PIN
  setPinInactive(BOARD_PWRKEY_PIN, true);
#endif
#ifdef MODEM_RESET_PIN
  setPinInactive(MODEM_RESET_PIN, MODEM_RESET_LEVEL == HIGH);
#endif

#ifdef SD_MODULE
  setPinActive(SD_EN_PIN, SD_EN_ACTIVE_HIGH);
#else
  setPinInactive(SD_EN_PIN, SD_EN_ACTIVE_HIGH);
#endif

  // Ensure both independent SPI Chip Select lines are deselected (HIGH) at boot
#ifdef SD_PIN_CS
  if (SD_PIN_CS != -1) {
    pinMode(SD_PIN_CS, OUTPUT);
    digitalWrite(SD_PIN_CS, HIGH);
  }
#endif
#ifdef LORA_CS_PIN
  if (LORA_CS_PIN != -1) {
    pinMode(LORA_CS_PIN, OUTPUT);
    digitalWrite(LORA_CS_PIN, HIGH);
  }
#endif

  // Put all defined board power rails into default inactive OFF state at boot
#ifdef RS485_EN_PIN
  setPinInactive(RS485_EN_PIN, RS485_EN_ACTIVE_HIGH);
#endif
  setPinInactive(EN_5V_PIN, EN_5V_ACTIVE_HIGH);
#ifdef OD_PWR_PIN
  setPinInactive(OD_PWR_PIN, OD_PWR_ACTIVE_HIGH);
#endif
  setPinInactive(I2C_EN_PIN, I2C_EN_ACTIVE_HIGH);
}

/**
 * @brief Initializes the dedicated LoRa SX1278 SPI bus on VSPI (SPI3).
 */
inline void loraSpiInit() {
#if defined(LORA_CS_PIN) && (LORA_CS_PIN != -1)
  pinMode(LORA_CS_PIN, OUTPUT);
  digitalWrite(LORA_CS_PIN, HIGH);
#endif
#if defined(LORA_RST_PIN) && (LORA_RST_PIN != -1)
  pinMode(LORA_RST_PIN, OUTPUT);
  digitalWrite(LORA_RST_PIN, HIGH);
#endif
#if defined(LORA_PIN_SCLK) && (LORA_PIN_SCLK != -1)
  SPI.begin(LORA_PIN_SCLK, LORA_PIN_MISO, LORA_PIN_MOSI, LORA_CS_PIN);
#endif
}

/**
 * @brief LED pattern signaling entry into IDLE state (3 quick pulses).
 */
inline void ledSignalIdle() {
#if defined(LED_PIN) && (LED_PIN != -1)
  pinMode(LED_PIN, OUTPUT);
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(100);
    digitalWrite(LED_PIN, LOW);
    if (i < 2) delay(100);
  }
#endif
}
