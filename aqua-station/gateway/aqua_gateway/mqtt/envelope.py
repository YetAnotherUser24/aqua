"""
MQTT message envelope — schema version 2.

The envelope is identical for every role; only the ``samples`` payload differs.
See docs/PROTOCOL.md §3.

Design rules that this module enforces:

* ``t_dev_ms`` is ALWAYS Unix epoch milliseconds UTC. There is no heuristic for
  "ms since 2000" — the previous system guessed between the two formats using a
  magic threshold, and that ambiguity is banned here.
* ``(boot_id, seq)`` is the idempotency key. QoS 1 means duplicates are expected,
  so the gateway drops them rather than double-counting samples.
* Unknown fields are never silently dropped. They are routed to the unknown-field
  side channel so firmware and gateway cannot drift apart unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

SUPPORTED_MAJOR = 2
"""Envelope major version this gateway implements."""

MAX_BATCH = 200
"""Hard cap on samples per message, protecting the Pi from abusive payloads."""


class TimeSource(str, Enum):
    """Where a node's clock came from. Ordered best -> worst."""

    NTP = "ntp"
    ESPNOW = "espnow"
    RTC = "rtc"
    MONOTONIC = "monotonic"

    @property
    def trust(self) -> int:
        """Higher is better. Used to decide whether to flag a sample in exports."""
        return {
            TimeSource.NTP: 3,
            TimeSource.ESPNOW: 2,
            TimeSource.RTC: 1,
            TimeSource.MONOTONIC: 0,
        }[self]


class EnvelopeError(ValueError):
    """Raised when a message cannot be accepted. Always carries a machine code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Envelope:
    """A validated telemetry message."""

    v: int
    role: str
    node_id: str
    exp: str | None
    seq: int
    t_dev_ms: int
    t_src: TimeSource
    boot_id: int
    samples: Sequence[Mapping[str, Any]]
    rssi: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> tuple[str, int, int]:
        """``(node_id, boot_id, seq)`` — the dedupe key for QoS 1 redelivery."""
        return (self.node_id, self.boot_id, self.seq)

    @property
    def is_time_trustworthy(self) -> bool:
        """False when the sample set must be flagged in experiment exports."""
        return self.t_src.trust >= TimeSource.ESPNOW.trust


# Fields the envelope owns. Anything else at the top level lands in ``extra``.
_ENVELOPE_FIELDS = frozenset({"v", "role", "node_id", "exp", "seq", "t_dev_ms", "t_src", "boot_id", "rssi", "samples"})

_REQUIRED = ("v", "role", "node_id", "seq", "t_dev_ms", "t_src", "boot_id", "samples")

# Keys the format reserves inside a sample object. They are not registry fields -- no
# ``roles.yaml`` declares them and the schema must not project them into a row -- but
# they are also not unknown, so the unknown-field side channel has to ignore them.
# Without this, every sample a node stamps with its own time would be reported as an
# unexpected field and a healthy node would look like it was sending junk.
SAMPLE_RESERVED_FIELDS = frozenset({"t_dev_ms"})


def parse_envelope(raw: Mapping[str, Any], *, expect_role: str | None = None) -> Envelope:
    """
    Validate a decoded JSON payload and return an :class:`Envelope`.

    :param raw: decoded JSON object from ``aqua/tlm/<role>``
    :param expect_role: role implied by the topic; a mismatch is a protocol error
    :raises EnvelopeError: with a machine-readable ``code``
    """
    if not isinstance(raw, Mapping):
        raise EnvelopeError("not_an_object", f"expected JSON object, got {type(raw).__name__}")

    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise EnvelopeError("missing_fields", f"missing {', '.join(missing)}")

    version = raw["v"]
    if not isinstance(version, int):
        raise EnvelopeError("bad_version", f"v must be an integer, got {version!r}")
    if version != SUPPORTED_MAJOR and version != SUPPORTED_MAJOR - 1:
        # Accept the current major and one behind; reject anything else loudly.
        #
        # This used to be `version // 100 != SUPPORTED_MAJOR // 100`, which never fired
        # for a single-digit major: `99 // 100` is 0, and so is `2 // 100`, so `v=99`
        # was accepted as a supported version. The documented rule (docs/PROTOCOL.md
        # §8) is stated in majors, so compare majors directly.
        raise EnvelopeError("unsupported_version", f"v={version}, gateway supports {SUPPORTED_MAJOR}")

    role = raw["role"]
    if not isinstance(role, str) or not role:
        raise EnvelopeError("bad_role", f"role must be a non-empty string, got {role!r}")
    if expect_role is not None and role != expect_role:
        # Topic and payload disagreeing means firmware routing is broken.
        raise EnvelopeError("role_topic_mismatch", f"payload role={role!r} but topic says {expect_role!r}")

    node_id = raw["node_id"]
    if not isinstance(node_id, str) or not node_id:
        raise EnvelopeError("bad_node_id", f"node_id must be a non-empty string, got {node_id!r}")

    seq = raw["seq"]
    if not isinstance(seq, int) or seq < 0:
        raise EnvelopeError("bad_seq", f"seq must be a non-negative integer, got {seq!r}")

    boot_id = raw["boot_id"]
    if not isinstance(boot_id, int) or boot_id < 0:
        raise EnvelopeError("bad_boot_id", f"boot_id must be a non-negative integer, got {boot_id!r}")

    t_dev_ms = raw["t_dev_ms"]
    if not isinstance(t_dev_ms, int) or t_dev_ms <= 0:
        raise EnvelopeError("bad_time", f"t_dev_ms must be a positive integer (Unix epoch ms UTC), got {t_dev_ms!r}")

    try:
        t_src = TimeSource(raw["t_src"])
    except ValueError as exc:
        valid = ", ".join(s.value for s in TimeSource)
        raise EnvelopeError("bad_time_source", f"t_src must be one of {valid}; got {raw['t_src']!r}") from exc

    samples = raw["samples"]
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise EnvelopeError("bad_samples", f"samples must be an array, got {type(samples).__name__}")
    if not samples:
        raise EnvelopeError("empty_samples", "samples array is empty")
    if len(samples) > MAX_BATCH:
        raise EnvelopeError("batch_too_large", f"{len(samples)} samples exceeds MAX_BATCH={MAX_BATCH}")
    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise EnvelopeError("bad_sample", f"samples[{index}] is {type(sample).__name__}, expected object")

    exp = raw.get("exp")
    if exp is not None and not isinstance(exp, str):
        raise EnvelopeError("bad_experiment", f"exp must be a string or null, got {exp!r}")
    if exp == "backend_resolved":
        # Retired: firmware used to send this literal, which produced orphaned rows.
        raise EnvelopeError("retired_sentinel", "'backend_resolved' is retired; send the real experiment id or null")

    rssi = raw.get("rssi")
    if rssi is not None and not isinstance(rssi, int):
        raise EnvelopeError("bad_rssi", f"rssi must be an integer dBm or absent, got {rssi!r}")

    extra = {k: v for k, v in raw.items() if k not in _ENVELOPE_FIELDS}

    return Envelope(
        v=version,
        role=role,
        node_id=node_id,
        exp=exp,
        seq=seq,
        t_dev_ms=t_dev_ms,
        t_src=t_src,
        boot_id=boot_id,
        samples=tuple(samples),
        rssi=rssi,
        extra=extra,
    )


def split_known_unknown(sample: Mapping[str, Any], known: frozenset[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Partition a sample into registry-known and unknown fields.

    The unknown half is written to the side channel rather than discarded. The
    previous system lost 5 Hz fidelity for weeks precisely because an unexpected
    condition failed quietly; losing a new firmware field silently would be the
    same bug wearing a different hat.
    """
    known_values: dict[str, Any] = {}
    unknown_values: dict[str, Any] = {}
    for key, value in sample.items():
        (known_values if key in known else unknown_values)[key] = value
    return known_values, unknown_values


def detect_sequence_gap(previous: int | None, current: int) -> int:
    """
    Return the number of messages apparently missing between two seq values.

    ``0`` means contiguous or first-seen. A negative result means a node rebooted
    mid-stream or messages arrived out of order, which is reported separately
    rather than treated as a gap.
    """
    if previous is None:
        return 0
    return max(0, current - previous - 1)
