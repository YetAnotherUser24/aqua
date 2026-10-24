/**
 * @file sensor_bus_real.cpp
 * @brief Real `IOdSensor` (OPTOD over Modbus RS-485) + `IBatteryMonitor` (ADC).
 *
 * Thin hardware adapter: rail gating, Modbus trigger/read, float decoding, rejection of the
 * Digisens sentinels and implausible values, and the ADC battery read. No node-level policy.
 */

#include "sensor_bus.h"
#include "n_logger_config.h"
#include "board.h"
#include "log.h"
#include "modbus_rtu.h"
#include "oled_display.h"
#include "wdt_feed.h"

#include <Arduino.h>
#include <HardwareSerial.h>
#include <esp32-hal-adc.h>
#include <math.h>

namespace {

HardwareSerial& odSerial() {
  static HardwareSerial serial(2);
  return serial;
}

float decodeFloat(uint16_t first, uint16_t second, bool swapWords) {
  union {
    uint32_t u32;
    float f32;
  } v = {};
  if (swapWords) {
    v.u32 = ((uint32_t)second << 16) | (uint32_t)first;
  } else {
    v.u32 = ((uint32_t)first << 16) | (uint32_t)second;
  }
  return v.f32;
}

bool isSentinel(float f) { return f == 9999.0f || f == 9998.0f || f == 9996.0f; }

bool plausible(float temp, float od) {
  return !isnan(temp) && !isnan(od) && !isinf(temp) && !isinf(od) && temp >= -40.0f &&
         temp <= 125.0f && od >= 0.0f && od <= 500.0f;
}

void safePowerDown(int pin, bool activeHigh) {
  if (pin != -1) {
    if (isOledAvailable() && pin == I2C_EN_PIN) return;
    setPinInactive(pin, activeHigh, false);
  }
}

class RealOdSensor : public IOdSensor {
public:
  void powerOn() override {
    if (_powered) return;
#ifdef RS485_EN_PIN
    setPinActive(RS485_EN_PIN, RS485_EN_ACTIVE_HIGH);
#endif
    gpio_hold_dis((gpio_num_t)OD_DE_RE_PIN);
    setPinActive(OD_PWR_PIN, OD_PWR_ACTIVE_HIGH);
    wdtDelay(OD_WARMUP_MS);
    _powered = true;
  }

  void powerOff() override {
    if (!_powered) return;
    odSerial().end();
    safePowerDown(OD_PWR_PIN, OD_PWR_ACTIVE_HIGH);
#ifdef RS485_EN_PIN
    safePowerDown(RS485_EN_PIN, RS485_EN_ACTIVE_HIGH);
#endif
    _powered = false;
  }

  bool read(OdReading& out, uint32_t /*nowMs*/) override {
    int16_t values[OD_NUM_READINGS] = {0};
    if (!readAll(values)) {
      out.valid = false;
      return false;
    }
    out.tempCenti = values[0];
    out.satDeciPct = values[1];
    out.doMilliMgL = values[2];
    out.p3Centi = values[3];
    out.p4Centi = values[4];
    out.valid = true;
    return true;
  }

private:
  bool readAll(int16_t* values) {
    modbus_rtu::ModbusRtu modbus(odSerial());
    modbus_rtu::ModbusRtu::Config cfg;
    cfg.directionPin = OD_DE_RE_PIN;
    cfg.manualDirection = true;
    cfg.txActiveHigh = true;
    cfg.responseTimeoutMs = 500;
    cfg.txEnableGuardUs = 250;
    cfg.txDisableGuardUs = 250;
    cfg.interFrameGapMs = 4;
    cfg.rxTurnaroundUs = 200;
    modbus.configure(cfg);
    modbus.begin(OD_BAUD, OD_RX_PIN, OD_TX_PIN);
    modbus.clearRxBuffer();

    bool triggered = false;
    modbus_rtu::ModbusRtu::WriteResult lastWrite;
    for (int retry = 0; retry < 2 && !triggered; retry++) {
      lastWrite = modbus.writeSingleRegister(OD_SLAVE_ID, OD_MODBUS_REGISTER_ORDER, OD_MODBUS_ORDER_VALUE);
      triggered = lastWrite.success;
      if (!triggered && retry == 0) wdtDelay(100);
    }
    if (!triggered) return false;

    uint32_t delayMs = OD_DEFAULT_SAMPLING_DELAY_MS;
    auto delayRes = modbus.readRegisters(OD_SLAVE_ID, 0x03, OD_SAMPLING_DELAY_REG, 1);
    if (delayRes.success && delayRes.count >= 1 && delayRes.values[0] > 0) {
      delayMs = delayRes.values[0];
    }
    wdtDelay(delayMs);

    auto readRes = modbus.readRegisters(OD_SLAVE_ID, 0x03, OD_MODBUS_START_ADDR, OD_MODBUS_READ_COUNT);
    if (!readRes.success || readRes.count < OD_MODBUS_READ_COUNT) return false;

    const bool swap = (OD_MODBUS_REGISTER_ORDER != 0x0001);
    float temp = decodeFloat(readRes.values[0], readRes.values[1], swap);
    float sat = decodeFloat(readRes.values[2], readRes.values[3], swap);
    float od = decodeFloat(readRes.values[4], readRes.values[5], swap);
    float p3 = decodeFloat(readRes.values[6], readRes.values[7], swap);
    float p4 = decodeFloat(readRes.values[8], readRes.values[9], swap);

    if (isSentinel(temp) || isSentinel(od) || !plausible(temp, od)) return false;

    values[0] = (int16_t)lroundf(temp * 100.0f);
    values[1] = (int16_t)lroundf(sat * 10.0f);
    values[2] = (int16_t)lroundf(od * 1000.0f);
    values[3] = (int16_t)lroundf(p3 * 100.0f);
    values[4] = (int16_t)lroundf(p4 * 100.0f);
    return true;
  }

  bool _powered = false;
};

class AdcBatteryMonitor : public IBatteryMonitor {
public:
  uint32_t readMilliVolts() override {
#if defined(SENSOR_BATTERY_ADC)
    return analogReadMilliVolts(BATTERY_ADC_PIN) * BATTERY_ADC_MULTIPLIER;
#else
    // Battery is ADC-only on this platform. Boards without the ADC divider report 0
    // rather than a bogus value.
    return 0;
#endif
  }
};

RealOdSensor g_sensor;
AdcBatteryMonitor g_battery;

}  // namespace

IOdSensor& odSensor() { return g_sensor; }
IBatteryMonitor& batteryMonitor() { return g_battery; }
