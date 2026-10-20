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
      when a booking ties the exercise or its range to a course, and its time overlaps the
      exercise's (started to completed, or to now while it runs), only Students actively
      enrolled in that course by assignment qualify (not a self-enrolment). A range booked only at other times (last month's
      class, next week's) has a roster with no seat now. An exercise with no booking at
      all stays readable by the tenant's Students here. Detection submission is stricter
      since 2026-10-09 (app/exercise_participants.py, ADR 0005): no roster, no Student
      submissions.

Anything else is 404, as for a range of another tenant.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .enrollment import rostered_course_ids
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


def lab_sessions_on(db: Session, tenant_id, range_id: uuid.UUID) -> list:
    """Every lab session ``_own_lab`` would match on ``range_id`` (current, or a range a
    rebuild replaced), newest first: the same relation, for the whole class at once."""
    from .lab_sessions.models import LabSession

    rid = str(range_id)
    rows = (
        db.query(LabSession)
        .filter(
            LabSession.tenant_id == tenant_id,
            or_(LabSession.range_id == range_id, LabSession.retired_ranges.contains(rid)),
        )
        .order_by(LabSession.created_at.desc())
        .all()
    )
    return [s for s in rows if s.range_id == range_id or rid in json.loads(s.retired_ranges or "[]")]


def _takes_part(db: Session, user: CurrentUser, rng: Range) -> bool:
    """Until 2026-10-11 any non-cancelled booking counted, however long ago: a range is
    reused class after class, so every past class on it kept reading the telemetry of every
    later exercise there. Now a booking counts only while it overlaps an active exercise's
    window: started_at to completed_at, or to now while it runs. (Detection submission,
    ``exercise_participants.roster_courses``, does not apply a time window yet.)"""
    from .scheduler.models import EventState, ScheduledEvent

    exercises = db.query(Exercise.id, Exercise.started_at, Exercise.completed_at).filter(
        Exercise.range_id == rng.id,
        Exercise.tenant_id == rng.tenant_id,
        Exercise.state.in_(ACTIVE_EXERCISE),
        Exercise.deleted_at.is_(None),
    ).all()
    if not exercises:
        return False
    bookings = db.query(ScheduledEvent.course_id).filter(
        ScheduledEvent.tenant_id == rng.tenant_id,
        ScheduledEvent.course_id.isnot(None),
        ScheduledEvent.state != EventState.cancelled,
        or_(ScheduledEvent.exercise_id.in_([eid for eid, _, _ in exercises]), ScheduledEvent.range_id == rng.id),
    )
    if bookings.first() is None:
        return True  # no roster to check: readable (submitting a detection is not; ADR 0005)
    now = datetime.now(UTC)
    courses: set[uuid.UUID] = set()
    for eid, started, completed in exercises:
        start, end = started or now, completed or now
        courses |= {
            cid
            for (cid,) in bookings.filter(
                or_(ScheduledEvent.exercise_id == eid, ScheduledEvent.range_id == rng.id),
                ScheduledEvent.start_time <= end,
                ScheduledEvent.end_time >= start,
            )
        }
    # Assigned enrolments only: self-enrolling on a shared course is no seat in its class.
    return bool(courses & set(rostered_course_ids(db, uuid.UUID(user.id))))


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
