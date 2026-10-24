"""
Experiment id generation.

The ``experiments`` table documents its key as ``EXP-YYYY-MM-DD-NN`` -- human-readable
because it is quoted in notes, exports and the run log. Deriving the next one from the ids
already present keeps that property, where a UUID or a sequence would not.

The subtle case is gaps. Ids are reused never, but a failed create can leave a hole, so the
next id is the highest suffix plus one rather than the count plus one. Counting would collide
with an existing id the moment a gap existed, and the collision would land on a *live*
experiment -- which is the one thing this must not do.
"""

from __future__ import annotations

from datetime import date

from aqua_gateway.api.app import _next_experiment_id

DAY = date(2026, 10, 9)
PREFIX = "EXP-2026-10-09-"


def test_first_experiment_of_the_day_is_zero_one() -> None:
    assert _next_experiment_id(DAY, []) == f"{PREFIX}01"


def test_increments_past_the_highest_existing() -> None:
    assert _next_experiment_id(DAY, [f"{PREFIX}01", f"{PREFIX}02"]) == f"{PREFIX}03"


def test_does_not_reuse_a_gap() -> None:
    """
    Highest-plus-one, not count-plus-one.

    With 01 and 03 present, counting gives 03 -- which already exists. Landing a new
    experiment on an id that has data is not a cosmetic bug.
    """
    assert _next_experiment_id(DAY, [f"{PREFIX}01", f"{PREFIX}03"]) == f"{PREFIX}04"


def test_ignores_other_days() -> None:
    other = [f"EXP-2026-10-08-{n:02d}" for n in range(1, 10)]
    assert _next_experiment_id(DAY, other) == f"{PREFIX}01"


def test_ignores_ids_that_do_not_match_the_pattern() -> None:
    """Hand-written ids are allowed, so the generator must not be derailed by one."""
    noisy = ["manual-run-1", f"{PREFIX}02", "EXP-2026-10-09-", "EXP-2026-10-09-abc"]
    assert _next_experiment_id(DAY, noisy) == f"{PREFIX}03"


def test_widens_past_two_digits() -> None:
    """``NN`` is a display convention, so the tenth must not wrap to 10 -> 11 incorrectly."""
    many = [f"{PREFIX}{n:02d}" for n in range(1, 10)] + [f"{PREFIX}10"]
    assert _next_experiment_id(DAY, many) == f"{PREFIX}11"


def test_padding_is_stable_for_single_digits() -> None:
    assert _next_experiment_id(DAY, [f"{PREFIX}09"]).endswith("-10")
