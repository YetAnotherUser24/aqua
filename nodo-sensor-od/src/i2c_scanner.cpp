/**
 * @file i2c_scanner.cpp
 * @brief Standalone I2C bus scanner diagnostic utility for the LoRa32 board.
 *
 * Automatically handles board power rail gating, scans 7-bit I2C addresses
 * (0x01 - 0x7E) on the primary Wire bus, and identifies known peripherals
 * (DS3231 RTC, AT24Cxx EEPROM, OLED display).
 *
 * Build via:
 *   pio run -e i2c-scan -t upload -t monitor
 */

#if defined(I2C_SCANNER)

#include <Arduino.h>
#include <Wire.h>
#include "board.h"

static const char* identifyDevice(uint8_t address) {
  switch (address) {
    case 0x3C:
    case 0x3D:
      return "OLED Display (SH1106 / SSD1306)";
    case 0x45:
      return "INA226 Voltage/Current Monitor (optional)";
    case 0x50:
    case 0x51:
    case 0x52:
    case 0x53:
    case 0x54:
    case 0x55:
    case 0x56:
    case 0x57:
      return "AT24Cxx I2C EEPROM";
    case 0x68:
      return "DS3231 RTC";
    default:
      return "Unknown I2C device";
  }
}

static void scanBus(TwoWire& bus, const char* busName, int sda, int scl) {
  Serial.printf("\n--- Scanning %s (SDA=%d, SCL=%d) ---\n", busName, sda, scl);
  int nDevices = 0;

  for (uint8_t address = 1; address < 127; address++) {
    bus.beginTransmission(address);
    uint8_t error = bus.endTransmission();

    if (error == 0) {
      Serial.printf("  [+] Found device at 0x%02X (%3d) -> %s\n",
                    address, address, identifyDevice(address));
      nDevices++;
    } else if (error == 4) {
      Serial.printf("  [!] Bus error at address 0x%02X\n", address);
    }
  }

  if (nDevices == 0) {
    Serial.printf("No I2C devices found on %s.\n", busName);
  } else {
    Serial.printf("%s scan complete: %d device(s) found.\n", busName, nDevices);
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println(F("\n============================================"));
  Serial.println(F("       I2C Bus Diagnostic Scanner     "));
  Serial.println(F("============================================"));

  boardInit();

  // Energize board I2C power rail
  Serial.printf("Powering I2C rail (GPIO %d, active %s)...\n",
                I2C_EN_PIN, I2C_EN_ACTIVE_HIGH ? "HIGH" : "LOW");
  setPinActive(I2C_EN_PIN, I2C_EN_ACTIVE_HIGH);

#ifdef EN_5V_PIN
  if (EN_5V_PIN != -1) {
    setPinActive(EN_5V_PIN, EN_5V_ACTIVE_HIGH);
  }
#endif

  // Allow power rail and peripheral internal POR to settle
  delay(100);

  Serial.printf("Board: %s\n", BOARD_NAME);
  Serial.printf("Primary Wire Bus: SDA=GPIO %d, SCL=GPIO %d\n", I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

#if defined(I2C_EEPROM_SDA_PIN) && (I2C_EEPROM_SDA_PIN != -1) && defined(I2C_EEPROM_SCL_PIN) && (I2C_EEPROM_SCL_PIN != -1)
  Serial.printf("Secondary Wire1 Bus: SDA=GPIO %d, SCL=GPIO %d\n",
                I2C_EEPROM_SDA_PIN, I2C_EEPROM_SCL_PIN);
  Wire1.begin(I2C_EEPROM_SDA_PIN, I2C_EEPROM_SCL_PIN);
#endif

  Serial.println(F("Initialization complete. Starting periodic scan...\n"));
}

void loop() {
  // Blink LED to indicate active scan
  if (LED_PIN != -1) {
    digitalWrite(LED_PIN, HIGH);
  }

  scanBus(Wire, "Wire (Primary)", I2C_SDA_PIN, I2C_SCL_PIN);

#if defined(I2C_EEPROM_SDA_PIN) && (I2C_EEPROM_SDA_PIN != -1) && defined(I2C_EEPROM_SCL_PIN) && (I2C_EEPROM_SCL_PIN != -1)
  scanBus(Wire1, "Wire1 (EEPROM/Secondary)", I2C_EEPROM_SDA_PIN, I2C_EEPROM_SCL_PIN);
#endif

  if (LED_PIN != -1) {
    digitalWrite(LED_PIN, LOW);
  }

  Serial.println(F("\nNext scan in 5 seconds..."));
  delay(5000);
}

#endif // I2C_SCANNER
