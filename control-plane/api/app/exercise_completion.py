"""Closing an exercise: one path for an instructor's /complete and for its duration running out.

Both totals the score under a lock on the exercise row (so a detection credited at the same
moment is either in the total or refused, never lost), then for every participant queues
competency auto-assessment, the LTI grade pass-back and the xAPI ``completed`` statement.

Exercises have no Student owner, so the participants are whoever closed it (if anyone) and
every user who submitted a detection in this run (ADR 0005).

The clock (``sweep_overdue``) runs in the API next to the lab sweep: a real-backend exercise
stays running after its timeline, and ends when an instructor completes it or its scenario's
``duration_minutes`` (or ``duration_min``) has passed since it started. Wall time: paused
time counts, and a paused exercise past its duration is closed too.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from sqlalchemy import func
from sqlalchemy.orm import Session

from .detections.models import DetectionSubmission
from .models import Exercise, ExerciseState, Objective, Scenario, User

logger = logging.getLogger("truenorth.api.exercise_completion")
SWEEP_SECONDS = float(os.getenv("EXERCISE_CLOCK_SECONDS", "60"))
DURATION_KEYS = ("duration_minutes", "duration_min")
OPEN = (ExerciseState.running, ExerciseState.paused)


@dataclass(frozen=True)
class Participant:
    user_id: uuid.UUID
    email: str
    name: str


def lock(db: Session, exercise_id: uuid.UUID) -> Exercise | None:
    """The exercise row, locked for this transaction (a no-op on SQLite)."""
    return db.query(Exercise).filter(Exercise.id == exercise_id).with_for_update().one_or_none()


def total(db: Session, exercise_id: uuid.UUID) -> int:
    return (
        db.query(func.coalesce(func.sum(Objective.points), 0))
        .filter(Objective.exercise_id == exercise_id, Objective.achieved.is_(True))
        .scalar()
    )


def participants(db: Session, ex: Exercise, closer: Participant | None = None) -> list[Participant]:
    """Whoever closed it, plus every user who submitted a detection in this run."""
    rows = (
        db.query(User.id, User.email, User.display_name)
        .join(DetectionSubmission, DetectionSubmission.user_id == User.id)
        .filter(DetectionSubmission.exercise_id == ex.id, DetectionSubmission.window_start >= ex.started_at)
        .distinct()
        .all()
        if ex.started_at
        else []
    )
    out = {p.user_id: p for p in (Participant(r[0], r[1] or "", r[2] or "") for r in rows)}
    if closer is not None:
        out.setdefault(closer.user_id, closer)
    return list(out.values())


def close(db: Session, exercise_id: uuid.UUID, background_tasks: Any, closer: Participant | None = None) -> Exercise | None:
    """Complete a running or paused exercise and queue its results. None if it was not open."""
    from .celery_client import dispatch
    from .routers.exercises import _push_exercise_lti_grade
    from .xapi import emit_lifecycle, exercise_result

    ex = lock(db, exercise_id)
    if ex is None or ex.state not in OPEN:
        db.commit()  # releases the row lock; nothing changed
        return None
    ex.state = ExerciseState.completed
    ex.completed_at = datetime.now(UTC)
    ex.total_score = total(db, ex.id)
    people = participants(db, ex, closer)
    db.commit()
    db.refresh(ex)

    for p in people:
        if dispatch("auto_assess_competency", str(ex.id), str(p.user_id)) is None:
            logger.warning("Failed to dispatch auto-assess for exercise %s user %s", ex.id, p.user_id)
        if background_tasks is not None:
            # Moodle/LTI grade pass-back (no-op unless that user launched it via LTI)
            background_tasks.add_task(_push_exercise_lti_grade, p.user_id, ex.id, ex.total_score or 0, ex.max_score or 100)
            emit_lifecycle(
                background_tasks,
                verb_key="completed",
                user_email=p.email or f"{p.user_id}@truenorth.local",
                user_name=p.name,
                activity_type="exercise",
                activity_id=str(ex.id),
                activity_name=ex.name,
                result=exercise_result(ex.total_score, ex.max_score),
            )
    return ex


# -- the clock ---------------------------------------------------------------------------
def duration(scenario_yaml: str | None) -> timedelta | None:
    """The scenario's planned length, or None when it sets none (or a nonsense one)."""
    try:
        doc = yaml.safe_load(scenario_yaml or "") or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    for key in DURATION_KEYS:
        try:
            minutes = float(doc[key])
        except (KeyError, TypeError, ValueError):
            continue
        if minutes > 0:
            return timedelta(minutes=minutes)
    return None


class _RunNow:
    """BackgroundTasks stand-in for the sweep: there is no response to wait for."""

    def add_task(self, fn, *args, **kwargs) -> None:
        try:
            result = fn(*args, **kwargs)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception:  # noqa: BLE001 — advisory side effects, as in the request path
            logger.debug("exercise close side effect failed", exc_info=True)


def overdue_ids(db: Session, now: datetime) -> list[uuid.UUID]:
    rows = (
        db.query(Exercise.id, Exercise.started_at, Scenario.yaml)
        .outerjoin(Scenario, Exercise.scenario_id == Scenario.id)
        .filter(Exercise.state.in_(OPEN), Exercise.started_at.is_not(None))
        .all()
    )
    out = []
    for exercise_id, started_at, scenario_yaml in rows:
        length = duration(scenario_yaml)
        start = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
        if length is not None and start + length <= now:
            out.append(exercise_id)
    return out


def sweep_overdue(db: Session, now: datetime | None = None) -> list[uuid.UUID]:
    """Complete every open exercise past its duration; return the ids closed."""
    closed = []
    for exercise_id in overdue_ids(db, now or datetime.now(UTC)):
        if close(db, exercise_id, _RunNow()) is not None:
            logger.info("exercise %s completed: duration elapsed", exercise_id)
            closed.append(exercise_id)
    return closed


def sweep_once() -> int:
    from .db import SessionLocal

    db = SessionLocal()
    try:
        return len(sweep_overdue(db))
    except Exception:  # noqa: BLE001 — a failed pass is logged; the next one tries again
        logger.exception("exercise clock sweep failed")
        return 0
    finally:
        db.close()


async def loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        await asyncio.get_running_loop().run_in_executor(None, sweep_once)
