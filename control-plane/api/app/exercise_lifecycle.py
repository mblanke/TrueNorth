"""Creating and withdrawing an exercise for a booking: the exercises section's side of
the scheduler (ADR 0004 slice 11). The scheduler never writes the exercises table
itself; it asks here."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from . import scenario_objectives
from .models import Exercise, ExerciseState, Scenario


def create_for_booking(
    db: Session, *, tenant_id: uuid.UUID, range_id: uuid.UUID, scenario_id: uuid.UUID, name: str
) -> Exercise:
    """A pending exercise on the booked range. The instructor starts it, as for any
    exercise; nothing here runs the scenario. Does not commit."""
    ex = Exercise(
        name=name,
        range_id=range_id,
        scenario_id=scenario_id,
        tenant_id=tenant_id,
        state=ExerciseState.pending,
        max_score=100,
    )
    db.add(ex)
    db.flush()
    # As POST /exercises: the scenario's objectives are what the exercise is scored on.
    # tenant-safe: the booking's scenario, checked against its tenant when it was booked.
    sc = db.get(Scenario, scenario_id)
    points = scenario_objectives.materialise(db, ex.id, sc.yaml if sc else None)
    if points:
        ex.max_score = points
    return ex


def withdraw_if_unstarted(db: Session, exercise_id: uuid.UUID) -> bool:
    """Cancel a booking's exercise if it never started. Guarded: a running or finished
    exercise is left alone. Does not commit."""
    moved = (
        db.query(Exercise)
        .filter(Exercise.id == exercise_id, Exercise.state == ExerciseState.pending)
        .update({Exercise.state: ExerciseState.cancelled}, synchronize_session=False)
    )
    return moved == 1
