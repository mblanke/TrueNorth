"""Who takes part in an exercise, for a Student's detection submission (ADR 0005, M3).

There is no Student-to-exercise table. The relations that exist, the same ones the
telemetry rule uses (app/telemetry_access.py, security sweep M2), are:

- a calendar booking (``scheduled_events``, not cancelled) that ties the exercise, or the
  range it runs on, to a course, and whose time overlaps the exercise's (started to
  completed, or to now while it runs): its participants are the Students actively
  enrolled in that course by assignment (``enrollment.rostered_course_ids``; a Student's
  own self-enrolment gives course content, not a seat in another class's exercise);
- a Student's own lab session on the exercise's range.

A Student submits a detection only as a participant. An exercise with no roster (no booked
course, not the Student's own lab) has no Student participants, so it is refused unless
``DETECTION_SUBMIT_PARTICIPANTS_ONLY=false``, which restores the previous rule for such an
exercise: any Student of the tenant (the roster still applies when there is one, and an
exercise or range booked only at other times has a roster with no seat now). Staff
(``exercise:start``: instructors, admins) may always submit; theirs is recorded as a staff
detection, not Student evidence.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .enrollment import rostered_course_ids
from .models import Exercise
from .rbac import Permission, user_has_permission


def participants_only() -> bool:
    """``DETECTION_SUBMIT_PARTICIPANTS_ONLY`` (default true)."""
    return os.getenv("DETECTION_SUBMIT_PARTICIPANTS_ONLY", "true").strip().lower() not in {"0", "false", "no", "off"}


def exercise_window(ex: Exercise, now: datetime | None = None) -> tuple[datetime, datetime]:
    """When the exercise runs: ``started_at`` (else now) to ``completed_at`` (else now)."""
    now = now or datetime.now(UTC)
    return ex.started_at or now, ex.completed_at or now


def _bookings(db: Session, ex: Exercise):
    """Live (not cancelled) course bookings that tie this exercise, or its range, to a course."""
    from .classes.sessions import all_session_ids
    from .scheduler.models import EventState, ScheduledEvent

    links = [ScheduledEvent.exercise_id == ex.id]
    if ex.range_id is not None:
        links.append(ScheduledEvent.range_id == ex.range_id)
    return db.query(ScheduledEvent.course_id).filter(
        ScheduledEvent.tenant_id == ex.tenant_id,
        ScheduledEvent.course_id.isnot(None),
        ScheduledEvent.state.notin_((EventState.cancelled, EventState.rejected, EventState.pending_approval)),
        # A class session counts once it is real: never as a draft (ADR 0014 §15).
        or_(ScheduledEvent.state != EventState.draft, ScheduledEvent.id.notin_(all_session_ids())),
        or_(*links),
    )


def roster_courses(db: Session, ex: Exercise, now: datetime | None = None) -> set[uuid.UUID]:
    """Courses of the live bookings (``_bookings``) whose time overlaps the exercise's
    (``exercise_window``). Until 2026-10-11 any non-cancelled booking counted, however long
    ago: a range is reused class after class, so every past class on it stayed a
    participant of every later exercise there. ``now``: the caller's clock (one per request)."""
    from .scheduler.models import ScheduledEvent

    start, end = exercise_window(ex, now)
    query = _bookings(db, ex).filter(ScheduledEvent.start_time <= end, ScheduledEvent.end_time >= start)
    return {cid for (cid,) in query}


def may_submit(db: Session, user: CurrentUser, ex: Exercise) -> bool:
    """Whether ``user`` may submit a detection on ``ex`` (already known to be in their tenant)."""
    if user_has_permission(user, Permission.EXERCISE_START):
        return True  # staff: recorded as a staff detection
    from .telemetry_access import _own_lab

    user_id = uuid.UUID(user.id)
    if ex.range_id is not None and _own_lab(db, user_id, ex.range_id):
        return True
    courses = roster_courses(db, ex)
    if courses:
        # Assigned enrolments only: a self-enrolment on a shared course is no seat here.
        return bool(courses & set(rostered_course_ids(db, user_id)))
    if _bookings(db, ex).first() is not None:
        # Booked, but at another time: a roster with no seat now. Not "no roster", so the
        # opt-out below does not open it to every Student of the tenant.
        return False
    return not participants_only()
