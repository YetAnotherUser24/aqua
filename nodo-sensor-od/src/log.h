/**
 * @file log.h
 * @brief Standalone logging declarations.
 *
 * Provides lightweight logging functions (Log/Logln/Logf) that print to Serial. These used to
 * mirror to a BLE notify characteristic when a client was streaming; that path was removed with
 * the rest of BLE, so output is Serial-only now.
 */

#pragma once
#include <Arduino.h>

/**
 * @brief Prints to Serial.
 */
void Log(const char* s);

/**
 * @brief Same as Log() with a trailing newline.
 */
void Logln(const char* s);

/**
 * @brief printf-style output to Serial.
 */
void Logf(const char* fmt, ...);