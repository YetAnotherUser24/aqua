#if !defined(DISABLE_ESPNOW)

#include "esp_now_sender.h"
#include "config.h"
#include "log.h"
#include <esp_now.h>
#include <WiFi.h>

static const uint8_t BROADCAST_MAC[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static const uint8_t TARGET_PEER_MAC[] = AERATOR_PEER_MAC;
static bool espNowReady = false;
static volatile bool s_lastSendAcked = true;

static inline bool isUnicast(const uint8_t* mac) {
  if (memcmp(mac, BROADCAST_MAC, 6) == 0) return false;
  static const uint8_t zeroMac[6] = {0};
  if (memcmp(mac, zeroMac, 6) == 0) return false;
  // Bit 0 of the first byte: 0 = Unicast, 1 = Multicast/Broadcast (IEEE 802.3/802.11)
  return ((mac[0] & 0x01) == 0);
}

static void onDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  s_lastSendAcked = (status == ESP_NOW_SEND_SUCCESS);
}

bool espNowSenderInit() {
  const bool useUnicast = isUnicast(TARGET_PEER_MAC);

  if (espNowReady) {
    bool peersOk = esp_now_is_peer_exist(BROADCAST_MAC);
    if (useUnicast) {
      peersOk = peersOk && esp_now_is_peer_exist(TARGET_PEER_MAC);
    }
    if (peersOk) return true;
  }

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

  esp_now_register_send_cb(onDataSent);

  // 1. Register Unicast target peer if configured
  if (useUnicast) {
    esp_now_peer_info_t unicastPeer = {};
    memcpy(unicastPeer.peer_addr, TARGET_PEER_MAC, 6);
    unicastPeer.channel = 0; // Auto-follow active Wi-Fi channel
    unicastPeer.ifidx = WIFI_IF_STA;
    unicastPeer.encrypt = false;

    if (!esp_now_is_peer_exist(TARGET_PEER_MAC)) {
      esp_err_t err = esp_now_add_peer(&unicastPeer);
      if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
        Logf("[ESP-NOW] Warning: Failed to add unicast peer! err=%d\n", err);
      }
    }
  }

  // 2. Always register Broadcast peer (used as fallback or primary if placeholder)
  esp_now_peer_info_t bcastPeer = {};
  memcpy(bcastPeer.peer_addr, BROADCAST_MAC, 6);
  bcastPeer.channel = 0; // Auto-follow active Wi-Fi channel
  bcastPeer.ifidx = WIFI_IF_STA;
  bcastPeer.encrypt = false;

  if (!esp_now_is_peer_exist(BROADCAST_MAC)) {
    if (esp_now_add_peer(&bcastPeer) != ESP_OK) {
      Logln("[ESP-NOW] Failed to add broadcast peer!");
      return false;
    }
  }

  espNowReady = true;
  if (useUnicast) {
    Logf("[ESP-NOW] Sender ready in UNICAST mode (Peer %02X:%02X:%02X:%02X:%02X:%02X, ch=%u, ACK+Retries ON).\n",
         TARGET_PEER_MAC[0], TARGET_PEER_MAC[1], TARGET_PEER_MAC[2],
         TARGET_PEER_MAC[3], TARGET_PEER_MAC[4], TARGET_PEER_MAC[5],
         (unsigned)WiFi.channel());
  } else {
    Logf("[ESP-NOW] Sender ready in BROADCAST mode (Placeholder MAC, ch=%u, Dual-Burst ON).\n",
         (unsigned)WiFi.channel());
  }
  return true;
}

bool espNowBroadcastDO(const OdTelemetryPacket& packet) {
  const bool useUnicast = isUnicast(TARGET_PEER_MAC);

  if (!espNowReady || WiFi.getMode() == WIFI_OFF || !esp_now_is_peer_exist(BROADCAST_MAC) ||
      (useUnicast && !esp_now_is_peer_exist(TARGET_PEER_MAC))) {
    espNowReady = false;
    if (!espNowSenderInit()) return false;
  }

  if (useUnicast) {
    // Modo UNICAST: El hardware 802.11 gestiona ACKs automaticos y hasta 7 reintentos en microsegundos
    esp_err_t result = esp_now_send(TARGET_PEER_MAC, (const uint8_t*)&packet, sizeof(packet));
    if (result == ESP_ERR_ESPNOW_NOT_INIT || result == ESP_ERR_ESPNOW_IF) {
      espNowReady = false;
      if (espNowSenderInit()) {
        result = esp_now_send(TARGET_PEER_MAC, (const uint8_t*)&packet, sizeof(packet));
      }
    }

    if (result == ESP_OK) {
      Logf("[ESP-NOW] UNICAST TX Seq #%u (ch=%u): DO=%.3f mg/L | Temp=%.2f C | ACK=%s\n",
           packet.sequence, (unsigned)WiFi.channel(), packet.dissolved_oxygen,
           packet.water_temperature, s_lastSendAcked ? "YES" : "NO");
      return true;
    } else {
      Logf("[ESP-NOW] Unicast TX failed (err=%d), falling back to Broadcast...\n", result);
    }
  }

  // Modo BROADCAST (Placeholder o Fallback de contingencia):
  esp_err_t result = esp_now_send(BROADCAST_MAC, (const uint8_t*)&packet, sizeof(packet));
  if (result == ESP_ERR_ESPNOW_NOT_INIT || result == ESP_ERR_ESPNOW_IF) {
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

    Logf("[ESP-NOW] BROADCAST TX Seq #%u (ch=%u): DO=%.3f mg/L | Temp=%.2f C | Sat=%.1f %%\n",
         packet.sequence, (unsigned)WiFi.channel(), packet.dissolved_oxygen,
         packet.water_temperature, packet.oxygen_saturation);
    return true;
  } else {
    Logf("[ESP-NOW] Broadcast TX failed, err=%d\n", result);
    return false;
  }
}

#endif // !DISABLE_ESPNOW

