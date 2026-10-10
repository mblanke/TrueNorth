"""Who may search a range's telemetry (``GET /telemetry/{range_id}/search``).

Until 2026-10-08 (security sweep M2) any signed-in user of the tenant could search any of
its ranges, including other Students' labs and ranges nobody had put them on. Now:

- users who hold ``telemetry:read`` (instructors, range ops, observers, admins) read the
  tenant's ranges as ``routers/ranges._readable_range`` does: a Student's lab only with
  infrastructure rights (``infra:read``);
- everyone else (Students) reads only
    * the range of their own lab session (current, or one it was rebuilt away from), and
    * the range of an exercise running or paused on it that they take part in. There is
      no Student-to-exercise table; the roster that exists is a calendar booking's course:
      when a booking ties the exercise or its range to a course, only Students actively
      enrolled in that course qualify. An exercise with no such booking stays readable by
      the tenant's Students here. Detection submission is stricter since 2026-10-09
      (app/exercise_participants.py, ADR 0005): no roster, no Student submissions.

Anything else is 404, as for a range of another tenant.
"""

from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .enrollment import active_course_ids
from .models import Exercise, ExerciseState, Range
from .rbac import Permission, user_has_permission
from .tenancy import get_owned

ACTIVE_EXERCISE = (ExerciseState.running, ExerciseState.paused)


def _own_lab(db: Session, user_id: uuid.UUID, range_id: uuid.UUID) -> bool:
    from .lab_sessions.models import LabSession

    for current, retired in db.query(LabSession.range_id, LabSession.retired_ranges).filter(LabSession.user_id == user_id):
        if current == range_id or str(range_id) in json.loads(retired or "[]"):
            return True
    return False


def _takes_part(db: Session, user: CurrentUser, rng: Range) -> bool:
    from .scheduler.models import EventState, ScheduledEvent

    exercise_ids = [
        eid
        for (eid,) in db.query(Exercise.id).filter(
            Exercise.range_id == rng.id,
            Exercise.tenant_id == rng.tenant_id,
            Exercise.state.in_(ACTIVE_EXERCISE),
            Exercise.deleted_at.is_(None),
        )
    ]
    if not exercise_ids:
        return False
    courses = {
        cid
        for (cid,) in db.query(ScheduledEvent.course_id).filter(
            ScheduledEvent.tenant_id == rng.tenant_id,
            ScheduledEvent.course_id.isnot(None),
            ScheduledEvent.state != EventState.cancelled,
            or_(ScheduledEvent.exercise_id.in_(exercise_ids), ScheduledEvent.range_id == rng.id),
        )
    }
    if not courses:
        return True  # no roster to check: readable (submitting a detection is not; ADR 0005)
    return bool(courses & set(active_course_ids(db, uuid.UUID(user.id))))


def readable_telemetry_range(db: Session, range_id: uuid.UUID, user: CurrentUser) -> Range:
    """The range, if ``user`` may search its telemetry; 404 otherwise."""
    rng = get_owned(db, Range, range_id, user, not_found="Range not found")
    if user_has_permission(user, Permission.TELEMETRY_READ):
        if user_has_permission(user, Permission.INFRA_READ):
            return rng
        from .lab_sessions.service import lab_range_ids

        if rng.id not in lab_range_ids(db, [rng.id]):
            return rng
    elif _own_lab(db, uuid.UUID(user.id), rng.id) or _takes_part(db, user, rng):
        return rng
    raise HTTPException(404, "Range not found")
