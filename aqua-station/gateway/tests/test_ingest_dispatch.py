"""
Regression: telemetry dispatch must not depend on an attribute nothing sets.

`_handle_telemetry` calls ``datetime.fromtimestamp(..., tz=self._timezone)`` to pick the
archive's day, and ``_flush_unknown`` does the same for its side-channel path. Nothing in
``__init__`` set ``_timezone``, so every envelope raised
``AttributeError: 'MqttIngest' object has no attribute '_timezone'``.

The failure was invisible to the rest of the suite because ``_dispatch`` swallows
exceptions by design ("a malformed message must not kill the loop") and logs them — so
the symptom was a gateway that connected, accepted telemetry, acknowledged commands, and
archived nothing. Exactly the class of bug a test at the boundary exists to catch.

These tests drive one real envelope through the real handler with a real archive writer,
so the day-boundary call is exercised rather than mocked.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aqua_gateway.archive.csv_writer import CsvArchiveWriter
from aqua_gateway.config import ArchiveSettings, BackupSettings, MqttSettings, Settings
from aqua_gateway.live.cache import LiveCache
from aqua_gateway.mqtt.client import MqttIngest
from aqua_gateway.schema.registry import load_registry

ROLE = "od_sensor"
TZ = "America/Lima"
# 2026-10-09T12:00:00Z = 07:00 in Lima, so the archive day is unambiguously the 9th.
T_DEV_MS = 1_791_547_200_000


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        timezone=TZ,
        backup=BackupSettings(enabled=False),
        mqtt=MqttSettings(host="127.0.0.1", port=1883),
        archive=ArchiveSettings(
            root=tmp_path / "telemetry",
            unknown_fields_path=tmp_path / "telemetry" / "unknown",
        ),
    )


def _envelope(*, seq: int = 1) -> bytes:
    return json.dumps(
        {
            "v": 2,
            "role": ROLE,
            "node_id": "a0000000-0000-0000-0000-000000000001",
            "exp": None,
            "seq": seq,
            "t_dev_ms": T_DEV_MS,
            "boot_id": 7,
            "t_src": "rtc",
            "samples": [
                {"t_dev_ms": T_DEV_MS, "od_do_mg_l": 7.874, "od_do_raw": 7874},
            ],
        }
    ).encode()


def _ingest(tmp_path: Path) -> MqttIngest:
    settings = _settings(tmp_path)
    archive = CsvArchiveWriter(
        settings.archive.root,
        # The writer buffers 64 KB before a block write, which is right in production and
        # wrong for a one-envelope test: flush every append so the assertion sees the file.
        block_bytes=0,
        timezone=settings.timezone,
    )
    return MqttIngest(
        settings=settings,
        registry=load_registry(),
        live=LiveCache(),
        archive=archive,
        manifest=None,  # type: ignore[arg-type]
    )


def test_timezone_attribute_is_set_from_settings(tmp_path: Path) -> None:
    """The day boundary must come from the configured zone, not a missing attribute."""
    ingest = _ingest(tmp_path)
    assert ingest._timezone == ZoneInfo(TZ)


@pytest.mark.asyncio
async def test_valid_envelope_is_archived(tmp_path: Path) -> None:
    """One real envelope must reach the CSV archive with the configured day."""
    ingest = _ingest(tmp_path)
    await ingest._handle_telemetry(ROLE, _envelope())

    day_file = tmp_path / "telemetry" / ROLE / "2026-10-09.csv"
    assert day_file.exists(), "envelope was accepted but never archived"
    body = day_file.read_text()
    assert "od_do_mg_l" in body.splitlines()[0], "header missing the metric"


@pytest.mark.asyncio
async def test_flush_unknown_uses_the_configured_day(tmp_path: Path) -> None:
    """The unknown-field side channel shares the same day-boundary attribute."""
    ingest = _ingest(tmp_path)
    ingest._unknown_buffer.append({"t_arr": 0.0, "role": ROLE, "node_id": "n", "fields": ["x"], "sample": {"x": 1}})
    await ingest._flush_unknown()
    today = datetime.now(tz=ZoneInfo(TZ)).date().isoformat()
    assert (tmp_path / "telemetry" / "unknown" / f"{today}.jsonl").exists()


# ---------------------------------------------------------------------------
# Presence must degrade when a node goes silent
# ---------------------------------------------------------------------------


def test_a_silent_node_stops_being_online():
    """
    A node that stops sending must stop counting as online.

    `update()` sets `online = True` on every envelope and nothing ever cleared it: presence
    was set by an LWT, and an LWT only fires when the broker notices the socket is gone. A
    process killed outright can leave the TCP session half-open until the MQTT keepalive
    expires, and nothing else downgraded the flag — so a killed node stayed "online" forever
    while the dashboard showed an age of several minutes beside it.

    That is worse than a cosmetic bug: `online` is what an operator reads to decide whether a
    node is healthy, so a frozen `True` reports a dead node as a live one that has merely gone
    quiet.
    """
    from aqua_gateway.live.cache import NodeState, _STALE_AFTER_S

    state = NodeState(role="pump", node_id="d")
    state.online = True
    state.last_seen = time.time()

    assert state.is_online() is True

    # Same node, telemetry that stopped just past the staleness window.
    state.last_seen = time.time() - (_STALE_AFTER_S + 1)
    assert state.is_online() is False, "a node silent past the window must not read as online"
    assert state.snapshot()["online"] is False, "snapshot() must agree with is_online()"


def test_a_never_seen_node_is_not_online():
    """
    No evidence is not evidence of health.

    A freshly started gateway registers every role with `last_seen = 0`. Reading the stored
    flag alone reported all of them as up before a single envelope had arrived.
    """
    from aqua_gateway.live.cache import NodeState

    state = NodeState(role="mixer", node_id="c")
    assert state.last_seen == 0.0
    assert state.snapshot()["online"] is False


def test_an_explicit_offline_wins_over_a_fresh_timestamp():
    """
    LWT is notification, not inference, so it must not be overridden by a recent arrival.

    A node can publish presence `online:false` and still have a recent `last_seen` — it said
    goodbye on the way out, or the LWT arrived just after its last telemetry. Trusting the
    timestamp would flip it straight back to online.
    """
    from aqua_gateway.live.cache import NodeState

    state = NodeState(role="aerator", node_id="b")
    state.online = False
    state.last_seen = time.time()  # seen one instant ago
    assert state.is_online() is False


@pytest.mark.asyncio
async def test_presence_alone_marks_a_node_online():
    """
    A node that announces presence and then sits idle counts as online until the window expires.

    The bug: `set_presence(online=True)` set the flag but left `last_seen = 0`, so `is_online()`
    computed `age = None` and returned False. The OD sensor, which publishes presence but no
    samples until told to monitor, therefore read as offline while the broker saw it connected --
    the gateway and the broker disagreeing about the same node. Presence is the node being seen,
    so it must move the same clock telemetry does.
    """
    from aqua_gateway.live.cache import LiveCache
    from aqua_gateway.schema.registry import load_registry

    live = LiveCache()
    live.register(load_registry().for_role("od_sensor"))
    assert live.snapshot()["nodes"]["od_sensor"]["online"] is False  # never seen yet

    await live.set_presence("od_sensor", online=True, reason="boot")

    node = live.snapshot()["nodes"]["od_sensor"]
    assert node["online"] is True, "presence alone must mark a node online"
    assert node["age_s"] is not None and node["age_s"] < 1.0, "presence must refresh last_seen"


async def test_retained_presence_does_not_falsely_mark_a_node_online():
    """A historical retained presence packet replayed by the broker must not fake activity."""
    from aqua_gateway.live.cache import LiveCache
    from aqua_gateway.schema.registry import load_registry

    live = LiveCache()
    live.register(load_registry().for_role("od_sensor"))
    assert live.snapshot()["nodes"]["od_sensor"]["online"] is False

    await live.set_presence("od_sensor", online=True, reason="boot", retained=True)

    node = live.snapshot()["nodes"]["od_sensor"]
    assert node["online"] is False, "replayed retained presence must not mark a node online"
    assert node["age_s"] is None, "replayed retained presence must not set last_seen"


def test_a_slow_but_healthy_node_stays_online():
    """
    The window must not be tighter than the slowest declared cadence.

    `od_sensor` and an idle `aerator` legitimately push once per 60 s, so a threshold below
    that would mark a working node offline between its own messages — the same false report
    this check exists to prevent, pointing the other way.
    """
    from aqua_gateway.live.cache import NodeState, _STALE_AFTER_S

    assert _STALE_AFTER_S >= 120.0, "must tolerate a 60 s cadence with room for a missed push"

    state = NodeState(role="od_sensor", node_id="a")
    state.online = True
    state.last_seen = time.time() - 60.0  # exactly one slow interval ago
    assert state.is_online() is True


def test_summarise_counts_only_live_nodes():
    """`/healthz` and `/api/live` must not disagree about who is up."""
    from aqua_gateway.live.cache import NodeState, summarise, _STALE_AFTER_S

    live = NodeState(role="aerator", node_id="b")
    live.online = True
    live.last_seen = time.time()

    dead = NodeState(role="pump", node_id="d")
    dead.online = True
    dead.last_seen = time.time() - (_STALE_AFTER_S + 5)

    summary = summarise([live, dead])
    assert summary["nodes_total"] == 2
    assert summary["nodes_online"] == 1
    assert summary["roles_offline"] == ["pump"]
