"""
Regression tests for three defects that only appear once the system has data in it.

All three were found by running the stack against real firmware rather than by reading
it, and all three share a shape: they are invisible on a fresh install, invisible in
isolation, and each one needs a node to have connected, a command to have been issued, or
a sample to have arrived before it can happen at all. "The tests pass on an empty
database" was true the whole time.

* ``nodes`` was created by ``001_core.sql`` and never written to. ``control_commands``
  has a foreign key to it, so **every operator command failed and returned a 500** --
  the entire control path was dead, while telemetry kept flowing and made the system look
  healthy.
* ``recent()`` returned raw database rows instead of ``CommandRecord.as_dict()``, so
  ``target`` arrived as a JSON string and ``superseded`` -- computed at issue time, not a
  column -- was simply absent. ``command.superseded.length`` then threw and took the
  whole dashboard down.
* ``LiveCache``'s ``schema_version`` was never assigned anywhere, so it stayed 0 and the
  dashboard told the operator that correctly migrated firmware had not been migrated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aqua_gateway.commands.outbox import CommandOutbox
from aqua_gateway.config import BackupSettings, Settings
from aqua_gateway.live.cache import LiveCache
from aqua_gateway.main import Gateway
from aqua_gateway.mqtt.client import MqttIngest
from aqua_gateway.schema.registry import load_registry

FIXED_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class FakeDB:
    """Captures ``executemany`` batches and replays canned rows from ``fetch``."""

    def __init__(self, *, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.batches: list[tuple[str, list[tuple]]] = []
        self.available = True

    async def fetch(self, query: str, *args: object) -> list[dict]:
        return self.rows

    async def executemany(self, query: str, rows: object) -> None:
        self.batches.append((query, [tuple(r) for r in rows]))  # type: ignore[union-attr]


class _StubGateway:
    """Just enough of ``Gateway`` for the unbound ``_sync_nodes`` to run."""

    def __init__(self, db: FakeDB, registry: object) -> None:
        self.db = db
        self.registry = registry


# -- nodes seeding ------------------------------------------------------------


async def test_sync_nodes_seeds_every_registry_role() -> None:
    """
    The table that the command path has a foreign key to.

    Before this ran, ``nodes`` was empty forever, so ``INSERT INTO control_commands``
    always failed the ``control_commands_node_id_fkey`` constraint and the API turned
    that into a bare 500. Nothing about the node, the broker or the firmware was wrong.
    """
    registry = load_registry()
    db = FakeDB()

    await Gateway._sync_nodes(_StubGateway(db, registry))  # type: ignore[arg-type]

    assert len(db.batches) == 1, "one batch, not one statement per role"
    query, rows = db.batches[0]

    assert "INSERT INTO nodes" in query
    assert "ON CONFLICT (id) DO UPDATE" in query, "restarting must not duplicate rows"

    by_role = {row[1]: row for row in rows}
    assert set(by_role) == set(registry.roles)

    # Ids come from roles.yaml, which the registry's own header declares to be the source
    # of truth for which nodes exist. Repeating them here would let the two drift.
    for schema in registry.all_roles():
        assert by_role[schema.role][0] == schema.node_id
        assert by_role[schema.role][2] == schema.display_name


async def test_sync_nodes_carries_the_expected_cadence() -> None:
    """The cadence is what lets the gateway notice a node that went quiet without dropping."""
    registry = load_registry()
    db = FakeDB()

    await Gateway._sync_nodes(_StubGateway(db, registry))  # type: ignore[arg-type]

    _, rows = db.batches[0]
    od = next(row for row in rows if row[1] == "od_sensor")
    # `main.cpp` samples every `experimentIntervalMs` (5000 ms) and flushes on the same
    # boundary: one sample per 5 s, so 0.2 Hz. See the rollup cadence tests.
    assert od[3] == pytest.approx(0.2), "expected_hz_experiment"
    assert od[4] == pytest.approx(0.0167), "expected_hz_idle"


# -- command list shape -------------------------------------------------------

_COMMAND_ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "role": "od_sensor",
    "action": "start_monitor",
    # asyncpg hands JSONB back as a string unless a codec is registered, and this pool
    # registers none. That is the whole reason the old code shipped a JSON blob where the
    # UI expected an object.
    "target": '{"interval_sec": 5}',
    "status": "published",
    "experiment_id": None,
    "requested_by": "bench",
    "issued_at": FIXED_NOW,
    "deadline_at": FIXED_NOW,
    "published_at": FIXED_NOW,
    "ack_at": None,
    "superseded_by": None,
    "error": None,
}


async def test_recent_matches_the_shape_the_issue_path_returns() -> None:
    """
    GET /api/commands must agree with POST /api/commands about what a command looks like.

    They disagreed: POST returned ``as_dict()``, GET returned raw rows. The UI is written
    against ``as_dict()``, so the list view rendered a JSON string as a target and crashed
    outright on the missing ``superseded`` key.
    """
    outbox = CommandOutbox(FakeDB(rows=[dict(_COMMAND_ROW)]))  # type: ignore[arg-type]

    out = await outbox.recent(limit=5)

    assert len(out) == 1
    record = out[0]

    assert record["target"] == {"interval_sec": 5}, "target must be an object, not a string"
    assert record["superseded"] == [], "the dashboard reads .length off this"
    assert record["status"] == "published"
    assert record["issued_at"] == FIXED_NOW.isoformat(), "timestamps must be JSON-ready"


@pytest.mark.parametrize("value", [None, "", "not json at all", "[1, 2, 3]", 42, 3.5])
def test_unreadable_target_degrades_to_an_empty_mapping(value: object) -> None:
    """
    A malformed target must not cost the operator the rest of the record.

    The action and the status are what the console is actually reporting on; losing those
    because one column would not decode is the worse failure.
    """
    # Imported here rather than at module scope: a missing private helper should fail the
    # tests that exercise it, not break collection for every other test in the file.
    from aqua_gateway.commands.outbox import _as_mapping

    assert _as_mapping(value) == {}


def test_readable_targets_are_decoded() -> None:
    from aqua_gateway.commands.outbox import _as_mapping

    assert _as_mapping('{"interval_sec": 5}') == {"interval_sec": 5}
    assert _as_mapping({"interval_sec": 5}) == {"interval_sec": 5}


# -- live state ---------------------------------------------------------------


async def test_live_update_records_the_envelope_schema_version() -> None:
    """
    The dashboard compares this against what the registry expects and warns when they
    disagree. It read 0 for everything, so a node publishing valid v2 firmware was
    reported as "Publishing schema v0 ... Firmware not yet migrated".
    """
    registry = load_registry()
    schema = registry.for_role("od_sensor")
    cache = LiveCache()
    cache.register(schema)

    await cache.update(
        schema,
        row={"od_do_mg_l": 7.5},
        t_dev_ms=1_791_547_200_000,
        t_src="rtc",
        seq=1,
        boot_id=7,
        rssi=-70,
        flagged=False,
        schema_version=2,
    )

    node = cache.snapshot()["nodes"]["od_sensor"]
    assert node["schema_version"] == 2


async def test_live_update_refuses_to_default_the_schema_version() -> None:
    """
    ``schema_version`` is deliberately a required argument.

    It was a field with a default and no assignment, which is exactly how it stayed wrong
    for so long without anything failing. Omitting it now fails loudly instead.
    """
    registry = load_registry()
    schema = registry.for_role("od_sensor")
    cache = LiveCache()
    cache.register(schema)

    with pytest.raises(TypeError):
        await cache.update(  # type: ignore[call-arg]
            schema,
            row={},
            t_dev_ms=0,
            t_src="monotonic",
            seq=1,
            boot_id=1,
            rssi=None,
            flagged=False,
        )


def test_an_unconnected_node_reports_no_schema_version() -> None:
    """
    A node that has never sent an envelope has made no claim about its firmware, so it
    reports ``None`` — not ``0``.

    It used to report 0, and the dashboard compares that against the registry's version:
    the result was "Publishing schema v0 … Firmware not yet migrated" printed against a node
    that had simply never connected. A defect report about nothing is worse than silence,
    because it trains the operator to ignore the line that would one day be real.
    """
    registry = load_registry()
    cache = LiveCache()
    cache.register(registry.for_role("od_sensor"))

    assert cache.snapshot()["nodes"]["od_sensor"]["schema_version"] is None


async def test_observed_rate_is_measured_from_the_nodes_own_timestamps() -> None:
    """
    The declared cadence is a declaration; this is the measurement that can contradict it.

    Samples carry their own time, so a node that batches 25 of them into one message is
    measured at its sampling rate and not at its batch rate. Arrival times would have
    reported 0.2 Hz for a node genuinely sampling at 5 — which is how a stopped node stayed
    labelled "5 Hz running".
    """
    registry = load_registry()
    schema = registry.for_role("aerator")
    cache = LiveCache()
    cache.register(schema)

    base = 1_791_547_200_000
    for index in range(51):  # 50 intervals of 200 ms == 5 Hz
        await cache.update(
            schema,
            row={"drv_rpm": 100.0},
            t_dev_ms=base + index * 200,
            t_src="ntp",
            seq=index + 1,
            boot_id=1,
            rssi=-60,
            flagged=False,
            schema_version=2,
        )

    assert cache.snapshot()["nodes"]["aerator"]["observed_hz"] == 5.0


async def test_observed_rate_is_absent_until_two_samples_arrive() -> None:
    """Null, not zero: a rate cannot be measured from a single sample, and 0 would read
    as "the node has stopped" when it has merely just started."""
    registry = load_registry()
    schema = registry.for_role("aerator")
    cache = LiveCache()
    cache.register(schema)

    assert cache.snapshot()["nodes"]["aerator"]["observed_hz"] is None

    await cache.update(
        schema,
        row={"drv_rpm": 1.0},
        t_dev_ms=1_791_547_200_000,
        t_src="ntp",
        seq=1,
        boot_id=1,
        rssi=-60,
        flagged=False,
        schema_version=2,
    )

    assert cache.snapshot()["nodes"]["aerator"]["observed_hz"] is None


# -- presence resolution ----------------------------------------------------
#
# A node's claim about its own role is a claim, not a fact, and one node gets it wrong: the
# aerator's Last Will named ``mixer``. Trusting the payload marked the *mixer* offline every
# time the *aerator* dropped, and left the aerator's own presence stuck online forever — a
# phantom node the dashboard had no way to correct, since the retained message never changed.
# The topic carries the canonical node id, so that is what decides.


class FakeLive:
    """Records what the ingest told the live cache, which is the observable effect."""

    def __init__(self) -> None:
        self.presence: list[tuple[str, bool]] = []

    async def set_presence(
        self, role: str, *, online: bool, reason: str | None = None, retained: bool = False
    ) -> None:
        self.presence.append((role, online))


def _ingest(registry: object, live: object) -> MqttIngest:
    """Only the collaborators presence touches. ``events`` is left out on purpose:
    ``_publish_event`` tolerates a missing stream, and this is about which role the
    presence lands on, not about the event it also writes."""
    return MqttIngest(
        settings=Settings(backup=BackupSettings(enabled=False)),
        registry=registry,  # type: ignore[arg-type]
        live=live,  # type: ignore[arg-type]
        archive=None,  # type: ignore[arg-type]
        manifest=None,  # type: ignore[arg-type]
        events=None,
    )


def test_role_for_node_id_covers_the_registry() -> None:
    registry = load_registry()

    for schema in registry.all_roles():
        assert registry.role_for_node_id(schema.node_id) == schema.role

    assert registry.role_for_node_id("ffffffff-0000-0000-0000-000000000000") is None


async def test_presence_is_resolved_from_the_topic_node_id() -> None:
    registry = load_registry()
    live = FakeLive()
    ingest = _ingest(registry, live)

    payload = json.dumps({"v": 2, "role": "mixer", "online": False, "reason": "lwt"}).encode()
    await ingest._handle_presence(  # type: ignore[attr-defined]
        "b0000000-0000-0000-0000-000000000002", payload
    )

    assert live.presence == [("aerator", False)]


async def test_presence_falls_back_to_the_payload_role_for_an_unknown_node() -> None:
    """A node the registry has never heard of must still be able to report presence."""
    registry = load_registry()
    live = FakeLive()
    ingest = _ingest(registry, live)

    payload = json.dumps({"v": 2, "role": "od_sensor", "online": True}).encode()
    await ingest._handle_presence(  # type: ignore[attr-defined]
        "ffffffff-0000-0000-0000-000000000000", payload
    )

    assert live.presence[0][:2] == ("od_sensor", True)
