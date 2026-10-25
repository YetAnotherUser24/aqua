"""
Live state cache and SSE fan-out.

This is the path that lets the dashboard show current values without touching
disk. The previous architecture had the browser poll four endpoints every 4-15 s
and reassemble state on every tick; here the gateway already holds the latest
value per metric, so a refresh is a memory read and a push.

Bounded on purpose: on a 905 MB box an unbounded history buffer is a slow memory
leak that only appears after a long experiment.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Sequence

from ..mqtt.envelope import SAMPLE_RESERVED_FIELDS
from ..schema.registry import RoleSchema

#: Window over which the observed sample rate is measured. Long enough that a node
#: reporting once a minute still lands twice inside it, short enough to react to a change
#: within a couple of minutes.
_RATE_WINDOW_MS = 120_000

#: How long a node may go silent before the live view stops calling it online.
#:
#: LWT is the fast path and the primary signal, but it is not sufficient on its own: it only
#: fires when the broker notices the connection is gone, and a process killed with SIGKILL can
#: leave the TCP session half-open until the MQTT keepalive expires. Nothing else downgrades the
#: flag -- `update()` sets `online = True` on every envelope and never clears it -- so a node that
#: was killed simply froze as "online" forever, which reads as a healthy node that has stopped
#: sending rather than as an absent one.
#:
#: The floor is set by the slowest declared cadence, not by the keepalive: `od_sensor` and an
#: idle `aerator` legitimately report once per 60 s, so anything below ~2 minutes would mark a
#: working node offline between pushes. 3x the slowest interval leaves room for a missed push
#: and a late reconnect while still clearing a dead node promptly.
_STALE_AFTER_S = 180.0


@dataclass(slots=True)
class Sample:
    """One metric reading with its provenance, retained for the live view."""

    t_dev_ms: int
    t_arr: float
    """Gateway arrival time. The difference from ``t_dev_ms`` is the clock offset."""
    value: Any
    t_src: str
    seq: int
    boot_id: int
    flagged: bool = False
    """True when the source clock was low-trust (``monotonic``) or offset was excessive."""


@dataclass(slots=True)
class NodeState:
    """Current known state of one node role."""

    role: str
    node_id: str
    online: bool = False
    last_seen: float = 0.0
    last_t_dev_ms: int = 0
    last_seq: int | None = None
    boot_id: int | None = None
    rssi: int | None = None
    t_src: str = "monotonic"
    #: Schema version of the last envelope, or ``None`` until a node has reported one.
    #: Deliberately not 0: 0 is a claim about firmware, and a node that has never connected
    #: has made no claim at all. Reading the default as "v0" is what told operators that
    #: working, migrated firmware had not been migrated.
    schema_version: int | None = None
    clock_offset_ms: int | None = None
    metrics: dict[str, Sample] = field(default_factory=dict)
    #: Device timestamps of recent samples, for the observed rate. Keyed by *device* time,
    #: not arrival time: a node that batches 25 samples into one message delivers them all
    #: in the same instant, so arrival times would measure the batch rate rather than the
    #: sampling rate — 0.2 Hz for a node genuinely sampling at 5.
    device_times: deque[int] = field(default_factory=deque)

    def observed_hz(self) -> float | None:
        """
        Samples per second the node is actually delivering, or ``None`` until two arrive.

        The cadence in ``roles.yaml`` is a *declaration* — what a role is expected to do.
        The dashboard rendered it as though it were happening, so a node that had stopped
        was still labelled "5 Hz running". Measuring the interval between the samples the
        node itself timestamped is the only way to answer "is it sending?".
        """
        if len(self.device_times) < 2:
            return None
        span_ms = self.device_times[-1] - self.device_times[0]
        if span_ms <= 0:
            return None
        return round((len(self.device_times) - 1) * 1000.0 / span_ms, 4)

    def is_online(self) -> bool:
        """
        Whether this node counts as online right now.

        Derived rather than read from ``self.online`` so that silence downgrades a node. The
        stored flag is only ever *set* -- by a presence message or by any telemetry -- and never
        cleared, so reading it directly reported a killed node as online forever.

        LWT still wins when it says offline: it is notification of a disconnect rather than an
        inference from absence, and it may arrive before the staleness window expires. A node
        that has never reported stays offline, because "no evidence" is not "up".
        """
        if not self.online:
            return False
        age = time.time() - self.last_seen if self.last_seen else None
        return age is not None and age <= _STALE_AFTER_S

    def snapshot(self) -> dict[str, Any]:
        age = time.time() - self.last_seen if self.last_seen else None
        return {
            "role": self.role,
            "node_id": self.node_id,
            "online": self.is_online(),
            "age_s": round(age, 1) if age is not None else None,
            "rssi": self.rssi,
            "t_src": self.t_src,
            "schema_version": self.schema_version,
            "clock_offset_ms": self.clock_offset_ms,
            "observed_hz": self.observed_hz(),
            "metrics": {name: sample.value for name, sample in self.metrics.items()},
        }


class LiveCache:
    """
    In-memory last-value-per-metric plus a short history for sparklines.

    Deliberately not persisted. Anything worth keeping goes to the CSV archive or
    the rollup tables; this exists to answer "what is true right now" cheaply.
    """

    def __init__(self, *, history_points: int = 600) -> None:
        self._nodes: dict[str, NodeState] = {}
        self._history: dict[tuple[str, str], deque[tuple[int, Any]]] = {}
        self._history_points = history_points
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    def register(self, schema: RoleSchema) -> None:
        self._nodes[schema.role] = NodeState(role=schema.role, node_id=schema.node_id)

    async def update(
        self,
        schema: RoleSchema,
        *,
        row: Mapping[str, Any],
        t_dev_ms: int,
        t_src: str,
        seq: int,
        boot_id: int,
        rssi: int | None,
        flagged: bool,
        schema_version: int,
    ) -> None:
        """
        Fold one sample into live state. Called per sample; must stay cheap.

        ``schema_version`` has no default on purpose. It used to be a field on ``NodeState``
        that nothing ever assigned, so it stayed 0 -- and the dashboard, which compares it
        against what the registry expects, told the operator "Publishing schema v0 ...
        Firmware not yet migrated" about firmware that was migrated and working perfectly.
        A default here would restore exactly that silence the next time a call site is added.
        """
        async with self._lock:
            state = self._nodes.get(schema.role) or NodeState(role=schema.role, node_id=schema.node_id)
            self._nodes[schema.role] = state

            arrival = time.time()
            state.online = True
            state.last_seen = arrival
            state.last_t_dev_ms = t_dev_ms
            state.last_seq = seq
            state.boot_id = boot_id
            state.rssi = rssi
            state.t_src = t_src
            state.schema_version = schema_version
            state.clock_offset_ms = int(arrival * 1000) - t_dev_ms

            # Bounded by device time, so the window covers the same span of the node's own
            # sampling regardless of how the gateway happened to receive it.
            state.device_times.append(t_dev_ms)
            horizon = t_dev_ms - _RATE_WINDOW_MS
            while state.device_times and state.device_times[0] < horizon:
                state.device_times.popleft()

            for name, value in row.items():
                # Reserved format keys are not measurements. ``t_dev_ms`` rides in the row
                # because the CSV writer needs it for the first column, so without this it
                # was stored as a live metric -- publishing the node's clock through
                # ``/api/live`` and listing it in the dashboard as something a sensor had
                # measured. The rollup never had the problem because it is driven by
                # ``METRIC_SPECS``; only this loop reads the row's keys.
                if value is None or name in SAMPLE_RESERVED_FIELDS:
                    continue
                state.metrics[name] = Sample(
                    t_dev_ms=t_dev_ms,
                    t_arr=arrival,
                    value=value,
                    t_src=t_src,
                    seq=seq,
                    boot_id=boot_id,
                    flagged=flagged,
                )
                history = self._history.setdefault((schema.role, name), deque(maxlen=self._history_points))
                history.append((t_dev_ms, value))

    async def set_presence(
        self, role: str, *, online: bool, reason: str | None = None, retained: bool = False
    ) -> None:
        """
        Update presence from MQTT Last Will & Testament or online announcements.

        LWT replaces the previous 60-second heartbeat heuristic: the broker tells
        us the moment a node drops, instead of us inferring it a minute later from
        a stale timestamp.

        ``last_seen`` is refreshed on a LIVE online presence announcement, not on historical
        retained messages replayed by the broker at startup. Without the ``not retained`` guard,
        a dead node whose retained online packet outlived a broker reboot would be claimed
        as seen in this instant, showing up as online for up to _STALE_AFTER_S seconds.
        """
        async with self._lock:
            state = self._nodes.get(role)
            if state is None:
                return
            state.online = online
            if online and not retained:
                state.last_seen = time.time()
            if reason:
                state.metrics.pop("_presence_reason", None)

    def snapshot(self) -> dict[str, Any]:
        """Full live view, served by ``GET /api/live`` and pushed over SSE."""
        return {
            "ts": time.time(),
            "nodes": {role: state.snapshot() for role, state in self._nodes.items()},
        }

    def history(self, role: str, metric: str, *, limit: int = 120) -> list[tuple[int, Any]]:
        points = self._history.get((role, metric))
        return list(points)[-limit:] if points else []

    # -- SSE fan-out -------------------------------------------------------

    async def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(self, payload: Mapping[str, Any]) -> None:
        """
        Push to subscribers.

        Slow consumers are dropped rather than allowed to block the ingest loop:
        a stalled browser tab must not stall the archive.
        """
        message = dict(payload)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def stream(self, *, interval_s: float) -> AsyncIterator[str]:
        """
        Yield SSE frames at a fixed cadence.

        One long-lived connection replaces four polling endpoints per open tab. The
        dashboard refreshes at 1 Hz, which the user confirmed is sufficient.
        """
        queue = await self.subscribe()
        try:
            while True:
                await asyncio.sleep(interval_s)
                frame = self.snapshot()
                yield f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"
        finally:
            await self.unsubscribe(queue)


def summarise(nodes: Sequence[NodeState]) -> dict[str, Any]:
    """Compact health summary for ``/healthz``."""
    return {
        "nodes_total": len(nodes),
        # Via the accessor, so /healthz agrees with /api/live. Reading `.online` here reported a
        # stale node as up while the dashboard showed it as down.
        "nodes_online": sum(1 for n in nodes if n.is_online()),
        "roles_offline": [n.role for n in nodes if not n.is_online()],
    }
