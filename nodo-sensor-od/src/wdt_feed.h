/**
 * @file wdt_feed.h
 * @brief Alimentación segura del Task Watchdog (TWDT) en rutinas de muestreo largas.
 *
 * Contexto: el lazo principal del OD-Logger está suscrito al Task WDT del Core 1 (5 s por
 * defecto). Una medición puede tardar ~6.7 s cuando el sensor OD no responde
 * (warmup 1500 ms + timeout de muestreo 5000 ms), lo que provocaba un
 * `TG1WDT_SYS_RESET` y reiniciaba el nodo en cada intento de muestreo.
 *
 * `wdtDelay()` reemplaza a `delay()` en las esperas largas y alimenta el WDT por tramos;
 * `wdtFeed()` debe invocarse dentro de cualquier bucle de reintentos que pueda exceder el
 * timeout del watchdog.
 */

#pragma once

#include <Arduino.h>
#include <esp_task_wdt.h>

/** Alimenta el Task Watchdog. Es inocuo si el WDT está deshabilitado o la tarea no está suscrita. */
static inline void wdtFeed() {
#if defined(ARDUINO_ARCH_ESP32) || defined(ESP32)
  esp_task_wdt_reset();
#endif
}

/** `delay()` apto para esperas largas: alimenta el WDT cada 100 ms. */
static inline void wdtDelay(uint32_t ms) {
  const uint32_t SLICE_MS = 100;
  uint32_t remaining = ms;
  while (remaining > SLICE_MS) {
    delay(SLICE_MS);
    remaining -= SLICE_MS;
    wdtFeed();
  }
  if (remaining > 0) {
    delay(remaining);
  }
}
