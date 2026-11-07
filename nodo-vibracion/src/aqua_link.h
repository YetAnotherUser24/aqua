#pragma once
/**
 * @file aqua_link.h
 * @brief Schema-v2 MQTT transport para el nodo de vibracion (ESP32-C3).
 */

#include <stddef.h>
#include <stdint.h>
#include "vibration_processor.h"

bool aquaLinkBegin();
void aquaLinkLoop();
bool aquaLinkConnected();
void aquaLinkQueue(const VibrationTelemetry& state, uint64_t unixMs);
bool aquaLinkFlush();
size_t aquaLinkQueued();
uint32_t aquaLinkDropped();
uint64_t aquaLinkNowMs();
void aquaLinkAck(const char* cmdId, bool applied, const char* detail);
