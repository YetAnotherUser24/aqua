#pragma once
/**
 * @file aqua_link.h
 * @brief Schema-v2 MQTT transport for the OD logger.
 *
 * Replaces the HTTPS upload path in `wifi_http.cpp` with a v2 envelope published to
 * `aqua/tlm/od_sensor`. Everything else about the node is unchanged: the Modbus cycle, the
 * plausibility checks, the integer scaling, the SD experiment file and the OLED all keep
 * running exactly as before. This is a transport swap, not a control change.
 *
 * ## Why a separate module rather than a rewrite of `wifi_http.cpp`
 *
 * The legacy POST path is ~450 lines that also parses piggybacked commands. Deleting it would
 * make the migration a large, hard-to-review diff and would remove the ability to revert a
 * node by reflashing. Instead `wifi_http.cpp` gains three one-line shims at the top of its
 * queue/flush functions and keeps compiling either way, selected by `AQUA_TRANSPORT_MQTT` in
 * `config.h`.
 *
 * The envelope itself is built by the vendored `aqua_protocol` module — the same code the
 * gateway's conformance test validates, so the wire format cannot drift from the contract.
 *
 * ## Threading
 *
 * `aquaLinkLoop()` must be called from the same task that calls `aquaLinkQueue()` and
 * `aquaLinkFlush()`. On this node that is the single `WEB_MASTER_MODE` loop.
 */

#pragma once
#include <Arduino.h>

// Pulled in here rather than left to the includer: without it `AQUA_TRANSPORT_MQTT` would still
// be undefined at this point in a translation unit that includes this header first, this whole
// header would silently compile to nothing, and the resulting error (`'aquaLinkLoop' was not
// declared in this scope`) points at the caller rather than at the cause.
#include "config.h"

#ifndef AQUA_TRANSPORT_MQTT
#define AQUA_TRANSPORT_MQTT 0
#endif

#if AQUA_TRANSPORT_MQTT

/**
 * @brief Configure the MQTT client and set the Last Will.
 *
 * Does **not** block on a connection. A node that cannot reach the broker must still log to SD
 * and still run its sensor loop; telemetry accumulates in RAM until `aquaLinkFlush()` succeeds.
 *
 * @return true if configuration succeeded. A false here means a programming error (buffer too
 *         small), not a network problem.
 */
bool aquaLinkBegin();

/**
 * @brief Maintain the connection and dispatch inbound commands. Call every loop iteration.
 *
 * Non-blocking between retries: a failed connect schedules the next attempt
 * `AQUA_MQTT_RETRY_MS` later rather than spinning, so a broker that is down does not starve the
 * sensor loop.
 */
void aquaLinkLoop();

/** @return true when the broker connection is up and subscriptions are live. */
bool aquaLinkConnected();

/**
 * @brief Queue one reading for the next envelope.
 *
 * Readings arrive in the node's own units (`readings[0]` = centi-°C, `[2]` = milli-mg/L) and are
 * converted to schema v2 units here, using the divisors recorded in the gateway's `roles.yaml`.
 * Doing the conversion here rather than at the call site keeps the legacy path untouched.
 *
 * @param seconds2000  device clock, seconds since 2000-01-01 (the node's native timebase)
 * @param msRemainder  sub-second part, 0-999. The RTC only resolves whole seconds, so at
 *                     the experiment rate of 5 Hz every sample in a second would otherwise
 *                     carry an identical ``t_dev_ms`` and the plot would collapse five
 *                     points onto one instant. The legacy path took this from
 *                     ``millis() % 1000`` and v2 needs it just as much -- ``t_dev_ms`` is
 *                     epoch *milliseconds*, and a millisecond field that is always zero is
 *                     worse than absent, because it looks precise.
 * @param readings     `MAX_SENSOR_READINGS` int16 values, as produced by `sensor_engine`
 * @param rtcTempCenti DS3231 die temperature, centi-°C
 * @param status       the node's 32-bit status bitmask, passed through as `dev_status`
 */
void aquaLinkQueue(uint32_t seconds2000, uint16_t msRemainder, int16_t* readings,
                   int16_t rtcTempCenti, uint32_t status);

/**
 * @brief Record which experiment subsequent samples belong to. Pass null or "" to clear.
 *
 * An experiment and a monitor run collect the same fields at the same rate; the experiment
 * id is the only thing that separates them afterwards. The schema models "no experiment" as
 * JSON null, so this stores a copy and ``aquaLinkFlush`` sends null when it is empty -- the
 * legacy ``"idle"`` sentinel would instead create a fake experiment to group rows under.
 *
 * @param experimentId id from `start_experiment`, or null/"" for a monitor or idle run
 */
void aquaLinkSetExperiment(const char* experimentId);

/**
 * @brief Publish everything queued as one envelope.
 *
 * On failure the batch is **retained**, not discarded: the caller's RAM buffer is only cleared
 * when this returns true, so a broker outage costs latency rather than data. The batch is
 * capped at `AQUA_ENVELOPE_BATCH_LIMIT`; anything beyond that is left for the next call, which
 * is why the return value is a count rather than a bool.
 *
 * @return true if the envelope was published (the queue may still hold a partial batch).
 */
bool aquaLinkFlush();

/** @return samples currently held for the next envelope. */
size_t aquaLinkQueued();

/** @return envelopes discarded because they could not be built. Non-zero is a firmware bug. */
uint32_t aquaLinkDropped();

/** @return true once the retained presence has been published for this connection. */
bool aquaLinkPresencePublished();

/**
 * @brief Publish a command acknowledgement to `aqua/ack/od_sensor`.
 *
 * The gateway settles the outbox row on this, which is what stops a command being re-served.
 * A node that acts but never acks looks, from the operator's side, exactly like a node that
 * ignored the command — so this must be sent even when the action failed.
 *
 * @param cmdId   the `cmd_id` from the command payload; ignored if empty
 * @param success true if the action was applied
 */
bool aquaLinkAck(const char* cmdId, bool success);

#endif  // AQUA_TRANSPORT_MQTT
