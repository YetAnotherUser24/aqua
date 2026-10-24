#pragma once
#include "esp_now_packet.h"
#ifdef ARDUINO
#include <Arduino.h>
#else
#include <stdint.h>
#endif


#if !defined(DISABLE_ESPNOW)

/**
 * @brief Initializes ESP-NOW protocol on the logger node.
 * Uses broadcast address FF:FF:FF:FF:FF:FF so any listening controller receives it.
 * @return true if initialized and peer registered successfully.
 */
bool espNowSenderInit();

/**
 * @brief Broadcasts optical DO and temperature telemetry packet via ESP-NOW.
 * @param packet Fully formed OdTelemetryPacket struct.
 * @return true if transmission succeeded.
 */
bool espNowBroadcastDO(const OdTelemetryPacket& packet);

#else

// Zero-overhead stubs when ESP-NOW is disabled (e.g. virtual dashboard test build)
static inline bool espNowSenderInit() { return true; }
static inline bool espNowBroadcastDO(const OdTelemetryPacket&) { return true; }

#endif

