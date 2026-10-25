"""
MQTT ingest loop.

This is the component that replaces the previous architecture's failure mode. The
old design had nodes POST telemetry every 5 s and poll for commands every 1-1.5 s,
each request costing a TLS handshake, an HTTP parse, a function invocation, a
SHA-256 auth and a database upsert. Here a node holds one connection and publishes.

Idle cost drops to one TCP keepalive per minute.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import aiomqtt

from ..archive.csv_writer import CsvArchiveWriter, rows_from_samples
from ..archive.manifest import Manifest
from ..commands.outbox import CommandOutbox
from ..config import Settings
from ..db.engine import Database
from ..events.derived import DerivedRules
from ..events.model import Event, EventType, Severity, Source
from ..events.stream import EventStream
from ..live.cache import LiveCache
from ..rollup.aggregator import RollupAggregator
from ..schema.registry import RoleSchema, SchemaRegistry
from . import topics
from .envelope import (
    Envelope,
    EnvelopeError,
    SAMPLE_RESERVED_FIELDS,
    detect_sequence_gap,
    parse_envelope,
    split_known_unknown,
)

log = logging.getLogger("aqua.ingest")


class MqttIngest:
    """
    Subscribes to all node topics and fans each batch out to its destinations.

    Per batch, four things happen and only two touch persistent storage:

    1. live cache update  (RAM)          -> SSE to browsers
    2. CSV buffer append  (RAM -> block write on ~64 KB)
    3. rollup accumulation (RAM -> one INSERT per minute)
    4. gap/clock bookkeeping -> events on anomaly only
    """

    def __init__(
        self,
        *,
        settings: Settings,
        registry: SchemaRegistry,
        live: LiveCache,
        archive: CsvArchiveWriter,
        manifest: Manifest,
        db: Database | None = None,
        rollup: RollupAggregator | None = None,
        events: EventStream | None = None,
        derived: DerivedRules | None = None,
        commands: CommandOutbox | None = None,
        orchestrator: Any | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.live = live
        self.archive = archive
        self.manifest = manifest
        # The archive's day boundary is the configured timezone, not UTC: a sample at
        # 21:00 Lima on the 9th belongs in the 9th's file. Every other archive module
        # derives it the same way (`csv_writer`, `rotate`), so ingest must too.
        self._timezone = ZoneInfo(settings.timezone)
        # Every one of these is optional on purpose. Rollups, events and the database
        # are DERIVED data: if they are absent or failing, telemetry still lands in the
        # CSV archive, which is the durable record. Requiring them here would make a
        # database outage stop ingest — the one thing that must not happen.
        self.db = db
        self.rollup = rollup
        self.events = events
        self.derived = derived
        self.commands = commands
        # Typed loosely to avoid an import cycle: the orchestrator imports this module's
        # sibling types, so naming it here would close the loop. It is only ever called
        # through ``observe``.
        self.orchestrator = orchestrator
        self._task: asyncio.Task[None] | None = None
        self._client: aiomqtt.Client | None = None
        #: Dedicated connection for outbound publishes (time broadcast, commands).
        #: An aiomqtt client has one session and one reader loop, so it cannot both publish
        #: and receive. See `_run` for the failure this causes.
        self._publisher: aiomqtt.Client | None = None
        self._stopping = asyncio.Event()

        # Per (node_id, boot_id): last seq seen and a short offset history. Kept in
        # memory only — worst case after a restart we re-learn an offset, which
        # costs a few idle samples, not data.
        self._last_seq: dict[tuple[str, int], int] = {}
        self._offset_samples: dict[tuple[str, int], list[int]] = {}

        # Unknown fields are accumulated and periodically written to the side
        # channel. Losing a new firmware field silently is the same class of bug as
        # the missing-table silence that hid 5 Hz fidelity loss before.
        self._unknown_buffer: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="mqtt-ingest")
        log.info(
            "ingest starting",
            extra={"host": self.settings.mqtt.host, "port": self.settings.mqtt.port},
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._flush_unknown()

    # -- main loop ---------------------------------------------------------

    async def _run(self) -> None:
        """
        Connect, subscribe, dispatch. Reconnect with backoff forever.

        A broker restart must not require a gateway restart; QoS 1 plus a
        persistent session on the node side means nothing is lost across the gap.

        **Two connections, not one.** An `aiomqtt.Client` owns a single session and a single
        reader loop. Calling `publish()` on a client while another coroutine iterates
        `client.messages` interleaves their framing, which corrupts the payload of whatever is
        arriving — and it presents as the gateway failing to JSON-decode valid telemetry, a
        thoroughly misleading symptom for what is really a client-lifetime bug.

        It only shows up once the gateway is publishing *and* receiving, which is why the
        heartbeat time broadcast was enough to trigger it: four nodes streaming at 5 Hz while
        the retained time payload went out on the same socket.
        """
        backoff = self.settings.mqtt.backoff_initial_s
        while not self._stopping.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self.settings.mqtt.host,
                    port=self.settings.mqtt.port,
                    username=self.settings.mqtt.username,
                    password=self.settings.mqtt.password,
                    identifier=f"{self.settings.mqtt.client_id}-pub",
                    keepalive=self.settings.mqtt.keepalive,
                ) as publisher:
                    self._publisher = publisher

                    async with aiomqtt.Client(
                        hostname=self.settings.mqtt.host,
                        port=self.settings.mqtt.port,
                        username=self.settings.mqtt.username,
                        password=self.settings.mqtt.password,
                        identifier=self.settings.mqtt.client_id,
                        keepalive=self.settings.mqtt.keepalive,
                    ) as client:
                        self._client = client
                        backoff = self.settings.mqtt.backoff_initial_s
                        log.info("mqtt connected")

                        for topic in (
                            topics.TELEMETRY_ALL,
                            topics.ACK_ALL,
                            topics.PRESENCE_ALL,
                            topics.EVENTS_ALL,
                        ):
                            await client.subscribe(topic, qos=1)

                        await self._publish_time()

                        async for message in client.messages:
                            await self._dispatch(
                                str(message.topic),
                                message.payload,
                                retained=bool(getattr(message, "retain", False)),
                            )
            except aiomqtt.MqttError as exc:
                if self._stopping.is_set():
                    return
                log.warning("mqtt error, retrying", extra={"error": str(exc), "backoff_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.settings.mqtt.backoff_max_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("unexpected ingest failure")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.settings.mqtt.backoff_max_s)
            finally:
                self._client = None
                self._publisher = None

    async def _dispatch(self, topic: str, payload: bytes, *, retained: bool = False) -> None:
        """Route one message. Never raises — a malformed message must not kill the loop."""
        try:
            if (role := topics.role_from_telemetry_topic(topic)) is not None:
                await self._handle_telemetry(role, payload)
            elif (role := topics.role_from_ack_topic(topic)) is not None:
                await self._handle_ack(role, payload)
            elif (node_id := topics.node_id_from_presence_topic(topic)) is not None:
                await self._handle_presence(node_id, payload, retained=retained)
            elif topic.startswith("aqua/event/"):
                await self._handle_node_event(topic, payload)
            else:
                await self._emit(
                    EventType.PROTOCOL_ERROR,
                    node_id=None,
                    detail={"reason": "unroutable_topic", "topic": topic},
                )
        except Exception:
            log.exception("dispatch failure", extra={"topic": topic})

    # -- telemetry ---------------------------------------------------------

    async def _handle_telemetry(self, topic_role: str, payload: bytes) -> None:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            await self._emit(
                EventType.PROTOCOL_ERROR,
                detail={"reason": "invalid_json", "role": topic_role, "error": str(exc)},
            )
            return

        try:
            envelope = parse_envelope(raw, expect_role=topic_role)
        except EnvelopeError as exc:
            # Validation failures are events, not exceptions. Losing visibility of a
            # firmware bug at the boundary is how the previous system went blind.
            await self._emit(
                EventType.PROTOCOL_ERROR,
                detail={"reason": exc.code, "detail": exc.detail, "role": topic_role},
            )
            return

        schema = self.registry.for_role(envelope.role)
        arrival = time.time()
        await self._check_sequence(envelope)

        # Fallback: if node RTC is unanchored (year < 2020), anchor to gateway arrival time
        if envelope.t_dev_ms < 1577836800000:
            shift_ms = int(arrival * 1000) - envelope.t_dev_ms
            from dataclasses import replace
            envelope = replace(envelope, t_dev_ms=envelope.t_dev_ms + shift_ms)
            for s in envelope.samples:
                if isinstance(s, dict) and "t_dev_ms" in s and isinstance(s["t_dev_ms"], (int, float)):
                    s["t_dev_ms"] = int(s["t_dev_ms"] + shift_ms)

        # -- clock bookkeeping ---------------------------------------------
        # offset = gateway clock - device clock, measured on arrival. Only useful on
        # idle traffic, since a burst inflates transit jitter.
        offset_ms = int(arrival * 1000) - envelope.t_dev_ms
        first_offset = self._record_offset(envelope, offset_ms, arrival)

        if first_offset:
            # Emitted as an event so the EXPORT can find it. Building the aligned dataset
            # needs each node's clock offset, and events are where that lives — no extra
            # table, and the offsets land in the audit trail alongside everything else.
            await self._emit(
                EventType.CLOCK_RESYNCED,
                node_id=envelope.node_id,
                experiment_id=envelope.exp,
                t_dev_ms=envelope.t_dev_ms,
                severity=Severity.INFO,
                detail={
                    "role": envelope.role,
                    "offset_ms": offset_ms,
                    "t_src": envelope.t_src.value,
                    "boot_id": envelope.boot_id,
                },
            )

        flagged = not envelope.is_time_trustworthy or abs(offset_ms) > self.settings.clock_offset_warn_ms

        # -- unknown-field side channel ------------------------------------
        # Reserved format keys are excluded: a node stamping each sample with its own
        # time is correct behaviour, not an unexpected field.
        known = schema.field_names | SAMPLE_RESERVED_FIELDS
        unknown_seen: dict[str, Any] = {}
        for sample in envelope.samples:
            _, unknown = split_known_unknown(sample, known)
            unknown_seen.update(unknown)
        if unknown_seen:
            self._unknown_buffer.append(
                {
                    "t_arr": arrival,
                    "role": envelope.role,
                    "node_id": envelope.node_id,
                    "fields": sorted(unknown_seen),
                    "sample": unknown_seen,
                }
            )
            if len(self._unknown_buffer) >= 64:
                await self._flush_unknown()

        # -- project and fan out -------------------------------------------
        day = datetime.fromtimestamp(envelope.t_dev_ms / 1000, tz=self._timezone).date()
        rows = rows_from_samples(schema, envelope.samples, t_dev_ms=envelope.t_dev_ms)

        await self.archive.append_batch(
            schema,
            rows,
            day=day,
            seq=envelope.seq,
            t_src=envelope.t_src.value,
        )

        for row in rows:
            t_row = int(row.get("t_dev_ms") or envelope.t_dev_ms)

            await self.live.update(
                schema,
                row=row,
                t_dev_ms=t_row,
                t_src=envelope.t_src.value,
                seq=envelope.seq,
                boot_id=envelope.boot_id,
                rssi=envelope.rssi,
                flagged=flagged,
                schema_version=envelope.v,
            )

            # Rollups accumulate purely in memory and return any bucket that completed
            # as a result of this sample. Nothing here touches the database per sample:
            # a whole minute becomes one batched insert.
            if self.rollup is not None:
                completed = self.rollup.add(
                    schema,
                    row,
                    t_dev_ms=t_row,
                    experiment_id=envelope.exp,
                    flagged=flagged,
                )
                if completed:
                    await self._flush_rollups(completed)

            # Domain rules emit on TRANSITION, so a value sitting on a threshold does
            # not write an identical event 5 times a second.
            if self.derived is not None:
                for derived_event in self.derived.evaluate(
                    schema,
                    row,
                    t_dev_ms=t_row,
                    node_id=envelope.node_id,
                    experiment_id=envelope.exp,
                ):
                    await self._publish_event(derived_event)

            # The orchestrator only needs the oxygen trend to judge phase conditions, so
            # it is fed every sample rather than polled: polling would miss the shape of a
            # fast rise and make a phase transition late. It stores two numbers and returns.
            if self.orchestrator is not None:
                self.orchestrator.observe(role=envelope.role, row=row, t_dev_ms=t_row)

        await self.live.publish({"kind": "telemetry", "role": envelope.role, "seq": envelope.seq})

    # -- sequence gap detection -------------------------------------------

    async def _check_sequence(self, envelope: Envelope) -> None:
        """
        Detect lost messages via the ``(node_id, boot_id, seq)`` triple.

        QoS 1 is at-least-once, so duplicates are expected and ignored; *gaps* mean
        something was lost, and that must be visible. The previous system had no
        equivalent check, which is why a node could stop reporting without anyone
        noticing.
        """
        key = (envelope.node_id, envelope.boot_id)
        previous = self._last_seq.get(key)

        gap = detect_sequence_gap(previous, envelope.seq)
        if gap > 0:
            severity = Severity.WARNING if gap >= self.settings.telemetry_gap_warn else Severity.INFO
            await self._emit(
                EventType.TELEMETRY_GAP_DETECTED,
                node_id=envelope.node_id,
                experiment_id=envelope.exp,
                t_dev_ms=envelope.t_dev_ms,
                severity=severity,
                detail={"gap": gap, "previous_seq": previous, "current_seq": envelope.seq},
            )
        elif previous is not None and envelope.seq <= previous:
            await self._emit(
                EventType.OUT_OF_ORDER_BATCH,
                node_id=envelope.node_id,
                t_dev_ms=envelope.t_dev_ms,
                detail={"previous_seq": previous, "current_seq": envelope.seq},
            )

        self._last_seq[key] = envelope.seq

    def _record_offset(self, envelope: Envelope, offset_ms: int, arrival: float) -> bool:
        """
        Track a node's clock offset. Returns ``True`` on the first sample of a session.

        The first sample of a `(node_id, boot_id)` is when the offset is most informative
        and least contaminated by transit jitter — the node has just started and is not
        yet mid-burst. Callers turn that first observation into a ``clock_resynced``
        event, which is what the export reads back.
        """
        key = (envelope.node_id, envelope.boot_id)
        samples = self._offset_samples.setdefault(key, [])
        first = not samples
        samples.append(offset_ms)
        if len(samples) > 120:
            del samples[:-120]
        return first

    def offset_stats(self, node_id: str, boot_id: int) -> dict[str, float] | None:
        """Mean and spread of recent offsets, reported in the export manifest."""
        samples = self._offset_samples.get((node_id, boot_id))
        if not samples:
            return None
        mean = sum(samples) / len(samples)
        spread = max(samples) - min(samples)
        return {"mean_ms": mean, "spread_ms": spread, "n": len(samples)}

    # -- acks, presence, node events ---------------------------------------

    async def _handle_ack(self, role: str, payload: bytes) -> None:
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return
        result = str(body.get("result", "unknown"))
        event_type = {
            "applied": EventType.COMMAND_ACKNOWLEDGED,
            "rejected": EventType.COMMAND_REJECTED,
            "failed": EventType.COMMAND_FAILED,
            "expired": EventType.COMMAND_EXPIRED,
        }.get(result, EventType.PROTOCOL_ERROR)

        # Settle the outbox row before emitting. The event log records what happened;
        # the row is what stops a command being re-served, so it is updated first.
        command_id = body.get("cmd_id")
        if self.commands is not None and isinstance(command_id, str):
            try:
                await self.commands.acknowledge(
                    command_id=command_id,
                    result=result,
                    error=body.get("error"),
                )
            except Exception:  # noqa: BLE001
                log.exception("could not settle command", extra={"cmd_id": command_id})

        await self._emit(
            event_type,
            node_id=body.get("node_id"),
            t_dev_ms=body.get("t_dev_ms"),
            correlation_id=command_id,
            detail=body,
        )

    async def _handle_presence(self, node_id: str, payload: bytes, *, retained: bool = False) -> None:
        """
        Presence from Last Will & Testament.

        This is what replaces the previous 60-second heartbeat heuristic: the broker
        reports a drop the moment the connection dies, rather than the dashboard
        inferring it a minute later from a stale timestamp.
        """
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            body = {}
        online = bool(body.get("online", False))

        # Resolve the role from the *topic's* node id, which is canonical, not from the
        # payload's `role`, which is the node's own claim and can be wrong. One node's
        # Last Will names the wrong role; trusting it marked an unrelated role offline
        # while the node it belonged to stayed marked online forever. The payload is
        # still the fallback, for a node the registry has never heard of.
        role = self.registry.role_for_node_id(node_id)
        if role is None:
            claimed = body.get("role")
            role = claimed if isinstance(claimed, str) else None
        if role is not None:
            await self.live.set_presence(
                role, online=online, reason=str(body.get("reason")), retained=retained
            )
        await self._emit(
            EventType.NODE_ONLINE if online else EventType.NODE_OFFLINE,
            node_id=node_id,
            detail=body,
        )

    async def _handle_node_event(self, topic: str, payload: bytes) -> None:
        """Firmware-originated events, buffered on the node's SD and replayed after a gap."""
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return
        raw_type = body.get("event_type")
        try:
            event_type = EventType(raw_type)
        except ValueError:
            await self._emit(
                EventType.PROTOCOL_ERROR,
                node_id=body.get("node_id"),
                detail={"reason": "unknown_event_type", "event_type": raw_type},
            )
            return
        await self._emit(
            event_type,
            node_id=body.get("node_id"),
            experiment_id=body.get("experiment_id"),
            t_dev_ms=body.get("t_dev_ms"),
            detail=body.get("detail") or {},
            source=Source.FIRMWARE,
        )

    # -- outbound ----------------------------------------------------------

    async def publish_command(self, role: str, payload: Mapping[str, Any]) -> None:
        if not self._publisher:
            raise RuntimeError("mqtt not connected")
        await self._publisher.publish(topics.command(role), json.dumps(payload).encode(), qos=1)

    async def _publish_time(self) -> None:
        """
        Broadcast the gateway's clock, retained.

        The Pi has no hardware RTC, so on power loss it has no idea of the time until
        NTP answers. Nodes therefore treat this as one source among several and fall
        back to the sensor's DS3231 — see docs/TIME-SYNC.md.
        """
        if not self._publisher:
            return
        payload = {"t_dev_ms": int(time.time() * 1000), "t_src": "ntp", "uncertainty_ms": 5}
        await self._publisher.publish(topics.TIME, json.dumps(payload).encode(), qos=1, retain=True)

    # -- helpers -----------------------------------------------------------

    async def _emit(
        self,
        event_type: EventType,
        *,
        node_id: str | None = None,
        experiment_id: str | None = None,
        t_dev_ms: int | None = None,
        severity: Severity | None = None,
        detail: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        source: Source = Source.GATEWAY,
    ) -> None:
        event = Event.make(
            event_type,
            source=source,
            node_id=node_id,
            experiment_id=experiment_id,
            t_dev_ms=t_dev_ms,
            severity=severity,
            correlation_id=correlation_id,
            detail=detail,
        )
        log.info("event", extra={"event_type": event.event_type.value, "severity": event.severity.value})
        await self._publish_event(event)

    async def _publish_event(self, event: Event) -> None:
        """
        Persist an event and push it to the live view.

        Never raises. A database problem must not propagate into the MQTT ingest loop,
        because that would let a failure in *derived* data stop *telemetry* — and the
        telemetry CSV is the durable record.
        """
        if self.events is None:
            return
        try:
            await self.events.emit(event, live=self.live)
        except Exception:  # noqa: BLE001 - EventStream already guards; this is defence in depth
            log.exception("event emit failed", extra={"event_type": event.event_type.value})

    async def _flush_rollups(self, completed: list[Any]) -> None:
        """Write completed rollup buckets. Swallows database failure by design."""
        if self.rollup is None or self.db is None:
            return
        await self.rollup.flush(self.db, buckets=completed)

    async def _flush_unknown(self) -> None:
        """Write accumulated unknown fields to the side channel."""
        if not self._unknown_buffer:
            return
        import json as _json

        target = self.settings.archive.unknown_fields_path
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{datetime.now(tz=self._timezone).date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as sink:
            for entry in self._unknown_buffer:
                sink.write(_json.dumps(entry, separators=(",", ":")) + "\n")
        log.warning("unknown telemetry fields received", extra={"count": len(self._unknown_buffer), "path": str(path)})
        self._unknown_buffer.clear()

    # -- observability -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self._client is not None,
            "tracked_nodes": len({node for node, _ in self._last_seq}),
            "unknown_pending": len(self._unknown_buffer),
        }
