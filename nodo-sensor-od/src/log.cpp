/**
 * @file log.cpp
 * @brief Serial logging backend for Log()/Logln()/Logf().
 *
 * These used to live in `ble_manager.cpp`, which mirrored output to a BLE notify characteristic
 * when a client was streaming. BLE was removed from this node, so the backend is Serial-only and
 * lives here, next to the header that declares it.
 *
 * The `modbus_probe` build (`-DMODBUS_PROBE_TOOL`) also defines these three functions; that
 * environment replaces this file, so both are never linked together.
 */

#include "log.h"

#include <stdarg.h>

#if !defined(MODBUS_PROBE_TOOL)

void Log(const char* s) {
  Serial.print(s);
}

void Logln(const char* s) {
  Serial.println(s);
}

void Logf(const char* fmt, ...) {
  char buf[192];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  Serial.print(buf);
}

#endif  // !MODBUS_PROBE_TOOL
