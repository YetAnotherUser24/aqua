#if !defined(DISABLE_ESPNOW)

#include "esp_now_sender.h"
#include "log.h"
#include <esp_now.h>
#include <WiFi.h>

static const uint8_t BROADCAST_MAC[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static bool espNowReady = false;

bool espNowSenderInit() {
  if (espNowReady && esp_now_is_peer_exist(BROADCAST_MAC)) return true;

  // Ensure WiFi radio is in station mode so MAC layer is active
  if (WiFi.getMode() == WIFI_OFF) {
    WiFi.mode(WIFI_STA);
  }
  WiFi.setSleep(false);

  esp_err_t initErr = esp_now_init();
  if (initErr != ESP_OK && initErr != ESP_ERR_ESPNOW_EXIST) {
    Logf("[ESP-NOW] Init failed! err=%d\n", initErr);
    return false;
  }

  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, BROADCAST_MAC, 6);
  peerInfo.channel = 0; // 0 = auto-follow active Wi-Fi channel
  peerInfo.ifidx = WIFI_IF_STA;
  peerInfo.encrypt = false;

  if (esp_now_is_peer_exist(BROADCAST_MAC)) {
    espNowReady = true;
    return true;
  }

  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Logln("[ESP-NOW] Failed to add broadcast peer!");
    return false;
  }

  espNowReady = true;
  Logf("[ESP-NOW] Sender initialized successfully (peer ch=0 auto-follow, WiFi ch=%u).\n",
       (unsigned)WiFi.channel());
  return true;
}

bool espNowBroadcastDO(const OdTelemetryPacket& packet) {
  if (!espNowReady || WiFi.getMode() == WIFI_OFF || !esp_now_is_peer_exist(BROADCAST_MAC)) {
    espNowReady = false;
    if (!espNowSenderInit()) return false;
  }

  esp_err_t result = esp_now_send(BROADCAST_MAC, (const uint8_t*)&packet, sizeof(packet));
  if (result == ESP_ERR_ESPNOW_NOT_INIT || result == ESP_ERR_ESPNOW_IF) {
    // Attempt automatic re-init recovery
    espNowReady = false;
    if (espNowSenderInit()) {
      result = esp_now_send(BROADCAST_MAC, (const uint8_t*)&packet, sizeof(packet));
    }
  }

  if (result == ESP_OK) {
    // Ráfaga redundante de 2 paquetes con 3ms de separación:
    // Al ser broadcast (FF:FF:..), 802.11 no usa reintentos por ACK.
    // Un segundo paquete consecutivo mitiga colisiones por ruido electromagnético de motores en 2.4 GHz.
    delayMicroseconds(3000);
    esp_now_send(BROADCAST_MAC, (const uint8_t*)&packet, sizeof(packet));

    Logf("[ESP-NOW] TX Seq #%u (ch=%u): DO=%.3f mg/L | Temp=%.2f C | Sat=%.1f %%\n",
         packet.sequence, (unsigned)WiFi.channel(), packet.dissolved_oxygen,
         packet.water_temperature, packet.oxygen_saturation);
    return true;
  } else {
    Logf("[ESP-NOW] TX failed, err=%d\n", result);
    return false;
  }
}

#endif // !DISABLE_ESPNOW

