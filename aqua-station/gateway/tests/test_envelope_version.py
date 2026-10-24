"""
Regression: only the current envelope major and one behind are accepted.

`docs/PROTOCOL.md` §8 states the rule in majors — "the gateway accepts the current major and
one behind". The implementation expressed it as a hundreds-digit comparison:

    if version // 100 != SUPPORTED_MAJOR // 100:

which never fired for a single-digit major, because ``99 // 100`` and ``2 // 100`` are both 0.
A bogus ``v=99`` envelope was therefore accepted as a supported version.

TEST-PLAN §B7 asserts the rejection explicitly ("Rejected as an unknown major").
"""

from __future__ import annotations

import json

import pytest

from aqua_gateway.mqtt.envelope import EnvelopeError, parse_envelope

ROLE = "od_sensor"
T_DEV_MS = 1_791_547_200_000


def _envelope() -> dict:
    return {
        "v": 2,
        "role": ROLE,
        "node_id": "a0000000-0000-0000-0000-000000000001",
        "exp": None,
        "seq": 1,
        "t_dev_ms": T_DEV_MS,
        "boot_id": 7,
        "t_src": "rtc",
        "samples": [{"t_dev_ms": T_DEV_MS, "od_do_mg_l": 7.874, "od_do_raw": 7874}],
    }


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        (2, True),  # the current major
        (1, True),  # one behind, per docs/PROTOCOL.md §8
        (0, False),
        (3, False),  # one ahead is not yet supported
        (99, False),  # a bogus single-digit major used to slip through
        (100, False),
    ],
)
def test_envelope_version_gate(version: int, accepted: bool) -> None:
    raw = _envelope()
    raw["v"] = version
    if accepted:
        assert parse_envelope(raw, expect_role=ROLE).v == version
    else:
        with pytest.raises(EnvelopeError):
            parse_envelope(raw, expect_role=ROLE)


def test_non_integer_version_is_rejected() -> None:
    """A version that is not an int is a different error, but still a rejection."""
    raw = _envelope()
    raw["v"] = "2"
    with pytest.raises(EnvelopeError):
        parse_envelope(raw, expect_role=ROLE)
