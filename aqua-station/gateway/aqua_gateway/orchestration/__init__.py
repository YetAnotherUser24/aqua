"""
Orchestration: recipes, phase conditions, and the run state machine.

``conditions.py`` is imported by ``events/derived.py`` for the shared trend estimator, so
this package's ``__init__`` deliberately does **not** re-export the engine: importing the
engine here would pull in ``..main`` and re-enter the events package during its own import.
Import ``Orchestrator`` from ``.engine`` directly.
"""

from .conditions import PhaseContext, evaluate, least_squares_rate_per_min
from .recipes import RECIPES, Phase, Recipe, validate_recipes

__all__ = [
    "RECIPES",
    "Phase",
    "PhaseContext",
    "Recipe",
    "evaluate",
    "least_squares_rate_per_min",
    "validate_recipes",
]
