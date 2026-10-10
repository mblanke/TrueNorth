"""Who takes part in an exercise, for a Student's detection submission (ADR 0005, M3).

There is no Student-to-exercise table. The relations that exist, the same ones the
telemetry rule uses (app/telemetry_access.py, security sweep M2), are:

- a calendar booking (``scheduled_events``, not cancelled) that ties the exercise, or the
  range it runs on, to a course: its participants are the Students actively enrolled in
  that course;
- a Student's own lab session on the exercise's range.

A Student submits a detection only as a participant. An exercise with no roster (no booked
course, not the Student's own lab) has no Student participants, so it is refused unless
``DETECTION_SUBMIT_PARTICIPANTS_ONLY=false``, which restores the previous rule for such an
exercise: any Student of the tenant (the roster still applies when there is one). Staff
(``exercise:start``: instructors, admins) may always submit; theirs is recorded as a staff
detection, not Student evidence.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .enrollment import active_course_ids
from .models import Exercise
from .rbac import Permission, user_has_permission


def participants_only() -> bool:
    """``DETECTION_SUBMIT_PARTICIPANTS_ONLY`` (default true)."""
    return os.getenv("DETECTION_SUBMIT_PARTICIPANTS_ONLY", "true").strip().lower() not in {"0", "false", "no", "off"}


def roster_courses(db: Session, ex: Exercise) -> set[uuid.UUID]:
    """Courses whose live bookings tie this exercise, or its range, to a course."""
    from .scheduler.models import EventState, ScheduledEvent

    links = [ScheduledEvent.exercise_id == ex.id]
    if ex.range_id is not None:
        links.append(ScheduledEvent.range_id == ex.range_id)
    return {
        cid
        for (cid,) in db.query(ScheduledEvent.course_id).filter(
            ScheduledEvent.tenant_id == ex.tenant_id,
            ScheduledEvent.course_id.isnot(None),
            ScheduledEvent.state != EventState.cancelled,
            or_(*links),
        )
    }


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
        return bool(courses & set(active_course_ids(db, user_id)))
    return not participants_only()
