/**
 * @file config.h
 * @brief Configuración del nodo Bomba Dosificadora Peristáltica (AquaControl V4).
 */

#pragma once

/* ------------------------------- WiFi ------------------------------- */
// Guarded so a credential can be injected at build time; see the note in
// nodo-aerador/src/config.h. An unguarded #define here silently wins over the flag.
#ifndef AQUA_WIFI_SSID
#define AQUA_WIFI_SSID          "AquaStation"
#endif
#ifndef AQUA_WIFI_PASSWORD
#define AQUA_WIFI_PASSWORD      "AquaPass"
#endif
#define WIFI_SSID               AQUA_WIFI_SSID
#define WIFI_PASSWORD           AQUA_WIFI_PASSWORD
#define WIFI_CONNECT_TIMEOUT_MS 15000

#ifndef AQUA_STATIC_IP
#define AQUA_STATIC_IP          192, 168, 4, 13
#endif
#ifndef AQUA_STATIC_GATEWAY
#define AQUA_STATIC_GATEWAY     0, 0, 0, 0
#endif
#ifndef AQUA_STATIC_SUBNET
#define AQUA_STATIC_SUBNET      255, 255, 255, 0
#endif

/* --------------------------- AquaControl ---------------------------- */
#define API_BASE_URL            "https://tesisutec.vercel.app"
#define API_TELEMETRY_BULK      API_BASE_URL "/api/telemetry/motor_bulk"
#define API_COMMANDS            API_BASE_URL "/api/commands/pending"
#define API_ACK_BASE            API_BASE_URL "/api/commands"
#define API_EVENTS_PUMP         API_BASE_URL "/api/events/pump"
#define DEVICE_KEY              "ESP32_PUMP"
#define TELEMETRY_INTERVAL_MS   3000
#define TELEMETRY_IDLE_INTERVAL_MS 20000
#define COMMAND_POLL_INTERVAL_MS 1000

/* ------------------- AquaControl MQTT (schema v2) ------------------ */
/* Transport selection. Setting this to 0 restores the legacy HTTPS paths below: the
   client setup, the bulk POST, the command poll and the ACK all still compile. This is
   the revert switch, so it must stay a compile-time constant and never become a
   runtime branch. */
#ifndef AQUA_TRANSPORT_MQTT
#define AQUA_TRANSPORT_MQTT       1
#endif
/* Every one of these is overridable with -D at build time, and that is not decoration: a
   bench node has to point at a developer's broker instead of the station's, and the
   alternative is editing this file — a tracked file — before every bench session and
   remembering to revert it afterwards.

   Guarding only `AQUA_TRANSPORT_MQTT` does not achieve that. Skipping that block does not
   skip the rest: the settings below still define themselves, so `-DAQUA_MQTT_HOST=...`
   either loses to the value here or emits a redefinition warning and then loses anyway.
   Each setting needs its own guard. */
/* The Pi's Tailscale name. mDNS would be nicer but the ESP32 resolver for
   `pump-doser.local` is already used for OTA and resolving two names costs more
   than it saves. */
#ifndef AQUA_MQTT_HOST
#define AQUA_MQTT_HOST            "192.168.4.1"  // Pi del banco (Hotspot AquaStation)
#endif
#ifndef AQUA_MQTT_PORT
#define AQUA_MQTT_PORT            1883
#endif
/* Anonymous is correct on the isolated bench network; the broker is bound to the
   Tailscale interface. If it is ever exposed further, these must become real
   credentials AND the TLS port, not just a password over 1883. */
#ifndef AQUA_MQTT_USER
#define AQUA_MQTT_USER            "pump"
#endif
#ifndef AQUA_MQTT_PASSWORD
#define AQUA_MQTT_PASSWORD        "3BVLCc0DB2gOfyByhaXkVfCk"
#endif
#ifndef AQUA_MQTT_KEEPALIVE_S
#define AQUA_MQTT_KEEPALIVE_S     30
#endif
/* Bound for how long the library waits for a broker response. PubSubClient defaults to 15 s
   and busy-waits while it does, so a broker that accepts the connection but never answers
   stalls the reconnect for that long and starves the watchdog. Kept well under the 60 s WDT. */
#ifndef AQUA_MQTT_SOCKET_TIMEOUT_S
#define AQUA_MQTT_SOCKET_TIMEOUT_S 5
#endif
/* Unique per node: two clients sharing a client-id fight over one session and the
   broker disconnects them in a loop. The old 60 s heartbeat heuristic could not tell
   this apart from a healthy node. */
#ifndef AQUA_MQTT_CLIENT_ID
#define AQUA_MQTT_CLIENT_ID       "aqua-pump"
#endif
/* Schema v2 header + one 8-field pump sample is well under 1 KB, but the envelope
   limit is 8 KB and PubSubClient refuses a publish larger than its buffer. Sized to
   match rather than to the current payload, so a batch of samples still fits. */
#ifndef AQUA_MQTT_BUFFER_BYTES
#define AQUA_MQTT_BUFFER_BYTES    8192
#endif
#ifndef AQUA_MQTT_RETRY_MS
#define AQUA_MQTT_RETRY_MS        5000UL
#endif

/* --------------------------- Hardware (Puente H TB6612FNG) ----------- */
// Driver Puente H TB6612FNG: Pines asignados 7, 6, 5, 4 (ESP32-S3)
#ifndef TB6612_STBY_PIN
#define TB6612_STBY_PIN         7       // STANDBY (HIGH = habilitado, LOW = standby)
#endif
#ifndef TB6612_AIN2_PIN
#define TB6612_AIN2_PIN         5       // AIN2 (Dirección)
#endif
#ifndef TB6612_AIN1_PIN
#define TB6612_AIN1_PIN         6       // AIN1 (Dirección)
#endif
#ifndef TB6612_PWMA_PIN
#define TB6612_PWMA_PIN         4       // PWMA (Señal PWM de velocidad)
#endif

// Aliases para compatibilidad
#ifndef PUMP_PWM_PIN_A
#define PUMP_PWM_PIN_A          TB6612_PWMA_PIN
#endif
#ifndef PUMP_PWM_PIN_B
#define PUMP_PWM_PIN_B          TB6612_AIN1_PIN
#endif
#ifndef I2C_SDA_PIN
#define I2C_SDA_PIN             9       // SDA para AS5600 (ESP32-S3 default)
#endif
#ifndef I2C_SCL_PIN
#define I2C_SCL_PIN             8       // SCL para AS5600 (ESP32-S3 default)
#endif
#ifndef PUMP_ENCODER_INVERT
#define PUMP_ENCODER_INVERT     1       // Invertir conteo del encoder (1: velocidad positiva al dosificar hacia afuera)
#endif
#ifndef PUMP_SERIAL_DEBUG
#define PUMP_SERIAL_DEBUG       0       // 0: Silencioso (no inunda el monitor con telemetria periodica); 1: Activo por defecto
#endif

#ifndef PUMP_PWM_PIN
#define PUMP_PWM_PIN            TB6612_PWMA_PIN
#endif

#ifndef PUMP_PWM_CHANNEL
#define PUMP_PWM_CHANNEL        0
#endif
#ifndef PUMP_PWM_CHANNEL_A
#define PUMP_PWM_CHANNEL_A      PUMP_PWM_CHANNEL
#endif
#ifndef PUMP_PWM_CHANNEL_B
#define PUMP_PWM_CHANNEL_B      PUMP_PWM_CHANNEL
#endif

#ifndef PUMP_PWM_FREQ_HZ
#define PUMP_PWM_FREQ_HZ        20000   // 20 kHz: fuera del rango audible (operacion ultrasonica silenciosa)
#endif

#ifndef PUMP_PWM_RES_BITS
#define PUMP_PWM_RES_BITS       10      // 0..1023
#endif

/* ------------------- Retracción Anti-Goteo (Suck-Back) -------------- */
// Vueltas en reversa al terminar una inyección para retraer el menisco 15-20 mm dentro de la boquilla
// Nota: El encoder está en el eje del motor (0.03488 mL/rev). 4.5 vueltas = ~0.15 mL (~15-20 mm en manguera de 3 mm)
// absorbiendo también el juego mecánico (backlash) de la caja reductora.
#ifndef SUCKBACK_REVS
#define SUCKBACK_REVS           4.5f    // 4.5 vueltas en eje motor (~0.15 mL de retracción efectiva)
#endif
#ifndef SUCKBACK_RAD_S
#define SUCKBACK_RAD_S          20.94f  // Velocidad suave de retracción en rad/s (200 RPM en eje motor)
#endif
#ifndef SUCKBACK_RPM
#define SUCKBACK_RPM            200.0f  // Alias en RPM
#endif

/* ------------------- Control de Velocidad (PID en rad/s) ------------ */
// Sistema canónico en unidades SI (rad/s), coherente con el nodo mezclador
#ifndef PUMP_MAX_RAD_S
#define PUMP_MAX_RAD_S          261.8f  // Velocidad angular máxima a 12V (2500 RPM ~ 261.8 rad/s)
#endif
#ifndef PUMP_MAX_RPM
#define PUMP_MAX_RPM            2500.0f // Alias en RPM (calibrado en banco de pruebas)
#endif
#ifndef PUMP_MIN_DUTY_PERCENT
#define PUMP_MIN_DUTY_PERCENT   10.0f   // Umbral de stiction para empezar a girar
#endif
#ifndef DOSE_TARGET_RAD_S
#define DOSE_TARGET_RAD_S       104.72f // Velocidad objetivo dosificación (~1000 RPM en eje motor)
#endif
#ifndef DOSE_TARGET_RPM
#define DOSE_TARGET_RPM         1000.0f // Alias en RPM (punto de operación lineal y estable)
#endif

// Ganancias canónicas del lazo de velocidad PI en unidades SI: duty/(rad/s) y duty/(rad/s * s)
#ifndef PUMP_PID_KP
#define PUMP_PID_KP             0.003780f // duty / (rad/s)  (Sintonizado analíticamente SIMC en eje motor)
#endif
#ifndef PUMP_PID_KI
#define PUMP_PID_KI             0.026888f // duty / (rad/s * s)
#endif
#ifndef PUMP_PID_KD
#define PUMP_PID_KD             0.000000f // Pure PI: KD forbidden for discrete ticks
#endif
#ifndef PUMP_PID_MAX_SLEW
#define PUMP_PID_MAX_SLEW       3.0f    // Slew rate max (duty fraction/segundo)
#endif

/* ------------------- Calibración de dosificación -------------------- */
// Volumen entregado por revolución del cabezal peristáltico (mL/rev). Calibrar en banco.
#ifndef ML_PER_REV
#define ML_PER_REV              0.03488f // Calibrado gravimétricamente a 1000 RPM (0.03488 mL/rev, error < 0.1%)
#endif
// Duty PWM de respaldo en caso de emergencia/modo abierto (%)
#ifndef DOSE_DUTY_PERCENT
#define DOSE_DUTY_PERCENT       50.0f
#endif
// Tiempo máximo de seguridad por dosis (ms) para evitar sobredosificación por fallo de encoder.
#ifndef DOSE_TIMEOUT_MS
#define DOSE_TIMEOUT_MS         120000UL
#endif

/* ------------------------------- OTA -------------------------------- */
#ifndef OTA_ENABLED
#define OTA_ENABLED             1
#endif
#ifndef OTA_HOSTNAME
#define OTA_HOSTNAME            "pump-doser"
#endif
#ifndef OTA_PASSWORD
#define OTA_PASSWORD            ""
#endif

