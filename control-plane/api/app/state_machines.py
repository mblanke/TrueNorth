"""Allowed exercise state transitions (``ExerciseState.can_transition_to``).

Kept out of models.py, whose line count is capped (ADR 0003). Range transitions live in
app/range_states.py and are re-exported here, so models.py imports both in one place.
"""

from .range_states import RANGE_TRANSITIONS as _RANGE_TRANSITIONS

__all__ = ["_EXERCISE_TRANSITIONS", "_RANGE_TRANSITIONS"]

_EXERCISE_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running", "cancelled"],
    "running": ["paused", "completed", "cancelled"],
    "paused": ["running", "cancelled"],
    "completed": [],
    "cancelled": [],
}
