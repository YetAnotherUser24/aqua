"""
Entry point and composition root.

Everything is wired here explicitly rather than via a DI framework, because the
whole system is small enough that a reader should be able to see the data path in
one screen.

Startup order matters: the archive and manifest must be ready before the first
MQTT message can be acknowledged, or a QoS 1 delivery would be lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn

from .archive.csv_writer import CsvArchiveWriter
from .archive.manifest import Manifest
from .archive.rotate import finalise_stale
from .commands.outbox import CommandOutbox
from .config import Settings, get_settings
from .db.engine import Database
from .events.derived import DerivedRules
from .events.stream import EventStream
from .hardware import HardwareMonitor
from .live.cache import LiveCache
from .logging_setup import configure_logging
from .mqtt.client import MqttIngest
from .orchestration.engine import Orchestrator
from .rollup.aggregator import RollupAggregator
from .schema.registry import load_registry

log = logging.getLogger("aqua.gateway")


class Gateway:
    """Owns the long-lived objects and the run loop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = load_registry()
        self.manifest = Manifest(settings.manifest_path)
        self.live = LiveCache(history_points=settings.live_history_points)
        self.hardware = HardwareMonitor()
        self.archive = CsvArchiveWriter(
            settings.archive.root,
            block_bytes=settings.archive.block_bytes,
            max_buffer_age_s=settings.archive.max_buffer_age_s,
            fsync_on_rotate=settings.archive.fsync_on_rotate,
            timezone=settings.timezone,
            manifest=self.manifest,
        )
        self.db = Database(settings)
        self.rollup = RollupAggregator(self.registry, bucket_seconds=settings.rollup_interval_s)
        self.derived = DerivedRules(settings, self.registry)
        self.events = EventStream(
            self.db,
            # The spill file is what keeps the event log honest across a database
            # outage: on overflow, events are appended here rather than dropped, and
            # replayed once the database returns.
            spill_path=settings.manifest_path.parent / "events-spill.jsonl",
        )
        self.commands = CommandOutbox(self.db, self.events)

        # The orchestrator sequences runs. It is constructed before the ingest client
        # because ingest forwards samples to it, and it issues commands through the same
        # outbox every operator command uses -- it is a client of the control path, never
        # a second one.
        self.orchestrator = Orchestrator(self)

        self.ingest = MqttIngest(
            settings=settings,
            registry=self.registry,
            live=self.live,
            archive=self.archive,
            manifest=self.manifest,
            db=self.db,
            rollup=self.rollup,
            derived=self.derived,
            events=self.events,
            commands=self.commands,
            orchestrator=self.orchestrator,
        )

        # Bound after both objects exist: the outbox publishes through the ingest client,
        # and the ingest client needs the outbox to settle acknowledgements. A setter
        # breaks that cycle without either module importing the other.
        self.commands.bind_publisher(self.ingest.publish_command)

        self._stopping = asyncio.Event()
        self._maintenance_task: asyncio.Task[None] | None = None

    async def _sync_nodes(self) -> None:
        """
        Materialise the role registry into the ``nodes`` table.

        ``control_commands.node_id`` is a foreign key to ``nodes(id)``, and nothing else
        in this codebase ever wrote that table -- the table was created by
        ``001_core.sql`` and then never populated. So every operator command failed the
        foreign key, surfaced to the caller as a bare 500, and the whole control path was
        dead. It failed in the way that is hardest to attribute: the node was connected,
        the broker was healthy, the telemetry was flowing, and only the command never
        moved.

        ``roles.yaml`` is the source of truth for which nodes exist -- its own header says
        adding a node type is a data change, never a code change -- so this syncs from the
        registry rather than repeating the ids here, where they would be free to drift.

        Upsert, not insert: a node added to the registry must appear on the next start,
        and a row that is already present must not become a duplicate.
        """
        rows = []
        for schema in self.registry.all_roles():
            hz = schema.expected_rate_hz or {}
            rows.append(
                (
                    schema.node_id,
                    schema.role,
                    schema.display_name,
                    hz.get("experiment"),
                    hz.get("idle"),
                )
            )
        if not rows:
            return
        await self.db.executemany(
            """
            INSERT INTO nodes (id, role, display_name, expected_hz_experiment, expected_hz_idle)
            VALUES ($1::uuid, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET
                role = EXCLUDED.role,
                display_name = EXCLUDED.display_name,
                expected_hz_experiment = EXCLUDED.expected_hz_experiment,
                expected_hz_idle = EXCLUDED.expected_hz_idle,
                updated_at = now()
            """,
            rows,
        )
        # Logged because an empty or stale nodes table is exactly the condition that used
        # to be invisible until someone tried to send a command.
        log.info("node registry synced: %d node(s)", len(rows))

    async def start(self) -> None:
        for schema in self.registry.all_roles():
            self.live.register(schema)
            self.archive.root.joinpath(schema.role).mkdir(parents=True, exist_ok=True)

        # A manifest inconsistency means bookkeeping disagrees with reality. Report
        # it and keep running: refusing to start would take the dashboard down for a
        # problem that only affects pruning.
        inconsistent = self.manifest.inconsistent()
        if inconsistent:
            log.error("manifest inconsistency: %d row(s) claim to be archived without proof", len(inconsistent))
            for record in inconsistent[:10]:
                log.error(
                    "  %s (flushed_at=%s verified=%s sha256=%s)",
                    record.path,
                    record.flushed_at,
                    record.verified,
                    record.sha256,
                )

        # Recover from an ungraceful shutdown before opening anything new.
        #
        # This runs at startup because it is the only moment the set is unambiguous:
        # this process has not opened a file yet, so every record with
        # `closed_at IS NULL` whose day has already ended belongs to a previous run
        # and can safely be hashed and closed.
        #
        # Without it, a file left open when the Pi browned out at 23:59 would keep
        # `closed_at IS NULL` forever: never compressed, never flushed, never pruned.
        # That is a silent leak which surfaces only as unexplained disk growth months
        # later, long after the cause has been forgotten.
        self._recover_orphans()

        # Database is attempted, never required. A failure here degrades rollups and
        # events; it does not stop telemetry, which is the durable record.
        if await self.db.connect():
            await self.db.apply_migrations()
            await self.db.ensure_event_partition(1)
            await self._sync_nodes()
        else:
            log.warning("starting without a database; rollups and events will be deferred")
        self.db.start_retry_loop()
        self.events.start()
        self.orchestrator.start()
        self._maintenance_task = asyncio.create_task(self._maintenance_forever(), name="maintenance")

        await self.ingest.start()
        # Reconcile to a defined, safe state *after* ingest is up, not before. It disarms every
        # role, and a `commands.issue` only publishes once the MQTT link is connected -- run
        # ahead of `ingest.start()` and every stop fails with `mqtt not connected`, so an
        # actuator left armed by the crashed process keeps running. That is exactly what a
        # boot reconcile must not do, so the ordering here is the safety property, not a
        # convenience: the disarm must have a live link to reach the node.
        await self.orchestrator.reconcile_on_boot()
        log.info(
            "gateway ready: roles=%s archive=%s manifest=%s tz=%s",
            ",".join(sorted(self.registry.roles)),
            self.settings.archive.root,
            self.settings.manifest_path,
            self.settings.timezone,
        )

    async def stop(self) -> None:
        log.info("shutting down")
        self._stopping.set()
        await self.orchestrator.stop()
        await self.ingest.stop()

        # Flush and close archive files so nothing is left half-written. This is the
        # one step that must complete: it is the durable record.
        await self.archive.close_all()

        # Best-effort persist of in-flight derived data. Losing these costs a
        # re-aggregation, not data, so they must not delay shutdown for long.
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance_task
        try:
            await self.rollup.flush(self.db)
            await self.events.stop()
        except Exception:  # noqa: BLE001
            log.warning("derived-data flush on shutdown failed", exc_info=True)
        await self.db.close()

        self.manifest.close()

    async def _maintenance_forever(self) -> None:
        """
        Periodic housekeeping that no single request can do.

        Three jobs. The first must run whether or not the database is up; the other two
        tolerate a missing one:

        * **Release archive buffers that have aged out.** The writer's own size and age
          checks only run when a row arrives, so a role that goes quiet holding a part-full
          buffer would keep those rows in RAM indefinitely.
        * **Close elapsed rollup buckets.** A bucket normally completes when the *next*
          sample arrives, but a role that goes quiet at the end of an experiment would
          leave its final partial minute open indefinitely.
        * **Expire overdue commands.** This is what stops a days-old command executing
          when a node finally wakes from deep sleep. Nodes also enforce their own
          deadline, so this is the queue's record catching up with reality rather than
          the safety mechanism itself.
        """
        interval = max(5.0, self.settings.rollup_interval_s)
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            try:
                # Ahead of the database check on purpose: the archive is the durable
                # record, so a database outage must not extend how long rows sit in RAM.
                await self.archive.flush_aged()
                if not self.db.available:
                    continue
                await self.rollup.flush(self.db)
                await self.commands.expire_stale()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("maintenance tick failed")

    def _recover_orphans(self) -> None:
        """
        Close archive records left open by a previous, ungraceful run.

        Delegates to ``rotate.finalise_stale``, which touches only records whose day
        has already ended — today's file is left alone, because the writer is about to
        resume it.

        Non-fatal by design: if this fails, the cost is deferred disk growth (files
        that never become flushable), not data loss. Refusing to boot the gateway over
        it would take the dashboard down for a housekeeping problem.
        """
        try:
            today = datetime.now(tz=ZoneInfo(self.settings.timezone)).date()
            closed = finalise_stale(self.manifest, today=today, timezone=self.settings.timezone)
        except Exception:
            log.exception("orphan recovery failed; continuing")
            return

        if closed:
            # A warning, not an error: this is exactly what the recovery path is for,
            # and it means the last shutdown was not graceful.
            log.warning(
                "recovered %d archive file(s) left open by a previous run: %s",
                len(closed),
                ", ".join(str(p.name) for p in closed),
            )

    async def run(self) -> None:
        await self.start()
        try:
            await self._stopping.wait()
        finally:
            await self.stop()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, gateway: Gateway) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(gateway.stop()))


async def serve(settings: Settings) -> None:
    """Run the gateway and the API in one event loop."""
    from .api.app import create_app

    gateway = Gateway(settings)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, gateway)

    await gateway.start()

    app = create_app(gateway)
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        timeout_graceful_shutdown=3,
        # Single worker: this process owns the MQTT session and the archive. Two
        # workers would double-subscribe and interleave writes to the same files.
        workers=1,
    )
    server = uvicorn.Server(config)

    try:
        await asyncio.gather(server.serve(), gateway._stopping.wait())
    finally:
        await gateway.stop()


def cli() -> None:
    """Console entry point. Also used by the systemd unit."""
    settings = get_settings()
    configure_logging(level=settings.log_level, as_json=settings.log_json)
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()


def describe() -> dict[str, Any]:
    """Diagnostic snapshot used by ``tools/verify-system.py``."""
    settings = get_settings()
    registry = load_registry()
    return {
        "schema_version": registry.version,
        "max_batch": registry.max_batch,
        "roles": {r.role: len(r.fields) for r in registry.all_roles()},
        "archive_root": str(settings.archive.root),
        "timezone": settings.timezone,
        "retention_days": settings.retention_days,
        "paths": {
            "manifest": str(settings.manifest_path),
            "roles_yaml": str(registry.source),
        },
    }


def _assert_paths_writable() -> None:
    """Fail fast at boot if the archive or manifest locations are unusable."""
    settings = get_settings()
    for path in (settings.archive.root, Path(settings.manifest_path).parent):
        if not path.exists():
            raise SystemExit(f"missing directory {path} — run deploy/install.sh")
        probe = path / ".aqua-write-probe"
        try:
            probe.touch()
            probe.unlink()
        except OSError as exc:
            raise SystemExit(f"{path} is not writable: {exc}") from exc
