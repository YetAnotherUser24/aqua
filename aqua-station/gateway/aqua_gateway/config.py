"""
Configuration for the aqua-gateway.

Loaded from environment with validation, so a malformed setting fails at boot
rather than at 03:00 when the flush timer fires.

Two settings deserve emphasis because they are easy to get wrong:

* ``timezone`` — rotation, retention and the daily flush all key off this, not the
  host clock. The Pi shipped on ``Europe/London`` while the project is
  ``America/Lima``; hardcoding local time would have silently misfiled every
  nightly file.
* ``retention`` — flush-aware by design. See ``archive/prune.py`` for the rule.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .archive.prune import PruneMode, RetentionPolicy


class MqttSettings(BaseSettings):
    """Broker connection. LAN-local, plain TCP by deliberate choice — see docs/PROTOCOL.md."""

    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "aqua-gateway"
    keepalive: int = 60
    # Reconnect backoff. A broker restart should not spin the loop.
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 30.0
    # Retained presence means a gateway restart immediately learns current state
    # instead of waiting for the next heartbeat.
    presence_retained: bool = True


class ArchiveSettings(BaseSettings):
    """On-Pi CSV archive."""

    root: Path = Path("/data/telemetry")
    block_bytes: int = 64 * 1024
    # A part-full buffer is released once it is this old, whichever comes first. Without it
    # the exposure is a fixed *number of bytes*, so its duration follows the sampling rate:
    # minutes for a 5 Hz node, over an hour for the od_sensor's real 0.2 Hz. The question
    # during a power cut is how much time, so this caps it by clock instead. Every release is
    # fsynced, so this is also the whole exposure -- there is no page-cache interval on top.
    # 300 rather than 60 because a release is one write AND one commit: at 60 s an
    # idle-cadence role released 1440 times a day, five times the write count of 300 s.
    max_buffer_age_s: float = 300.0
    # Off by default: the node's own SD card is the archival record for an
    # experiment window, so losing the last seconds of a live file is acceptable
    # and the SD writes are not. Enabled only at rotation.
    fsync_on_rotate: bool = True
    compress_on_rotate: bool = True
    unknown_fields_path: Path = Path("/data/telemetry/unknown")


class BackupSettings(BaseSettings):
    """Daily flush to Supabase Storage (S3-compatible)."""

    enabled: bool = True
    endpoint_url: str | None = None
    bucket: str = "aqua-archive"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    region: str = "us-east-1"
    # Verify sha256 and row count against the remote object before marking a file
    # flushed. Skipping this is how a truncated upload becomes the only copy.
    verify_after_upload: bool = True
    max_uploads_per_run: int = 200


class Settings(BaseSettings):
    """Root settings object."""

    model_config = SettingsConfigDict(
        env_prefix="AQUA_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="forbid",
    )

    environment: str = "production"
    timezone: str = "America/Lima"
    log_level: str = "INFO"
    # JSON logs to stderr; journald is configured volatile so logs never reach the SD card.
    log_json: bool = True

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # The dashboard is served by Caddy on the same origin, so relative /api URLs work
    # with zero UI changes. CORS is therefore not needed in production.
    cors_origins: list[str] = Field(default_factory=list)

    manifest_path: Path = Path("/var/lib/aqua-gateway/manifest.db")
    #: Where experiment bundles are assembled. Separate from the archive so a bundle can
    #: be pruned independently of the telemetry it was derived from — the raw CSV is the
    #: record of what happened, the bundle is a convenience for analysis.
    export_root: Path = Path("/data/exports")

    mqtt: MqttSettings = Field(default_factory=MqttSettings)
    archive: ArchiveSettings = Field(default_factory=ArchiveSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)

    # -- retention ---------------------------------------------------------
    retention_days: int = 14
    max_local_bytes: int = 8 * 1024**3
    min_free_bytes: int = 2 * 1024**3
    prune_mode: PruneMode = PruneMode.BOTH

    # -- live / rollup -----------------------------------------------------
    live_sse_hz: float = 1.0
    rollup_interval_s: int = 60
    # Buffer depth for the live cache. Bounded so a stalled consumer cannot grow
    # memory without limit on a 905 MB box.
    live_history_points: int = 600

    # -- alerting thresholds (also drive derived process events) -----------
    do_warning_mg_l: float = 5.0
    do_critical_mg_l: float = 3.0
    temp_warning_c: float = 30.0
    clock_offset_warn_ms: int = 500
    telemetry_gap_warn: int = 3

    # -- database ----------------------------------------------------------
    database_url: str = "postgresql://aqua@127.0.0.1:5432/aqua"
    db_pool_min: int = 1
    db_pool_max: int = 8

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.do_critical_mg_l >= self.do_warning_mg_l:
            raise ValueError("do_critical_mg_l must be below do_warning_mg_l")
        if self.db_pool_min < 1 or self.db_pool_max < self.db_pool_min:
            raise ValueError("invalid db pool bounds")
        if self.backup.enabled and not (self.backup.access_key_id and self.backup.secret_access_key):
            raise ValueError("backup enabled but storage credentials are missing")
        return self

    @property
    def retention(self) -> RetentionPolicy:
        """Assemble the retention policy. Kept as a property so it cannot drift from settings."""
        return RetentionPolicy(
            retention_days=self.retention_days,
            max_local_bytes=self.max_local_bytes,
            min_free_bytes=self.min_free_bytes,
            prune_mode=self.prune_mode,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()
