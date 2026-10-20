"""Which courses a caller teaches and which exercises they run (#200).

Courses and exercises carry no owner or instructor column. The one record of who teaches
what is the calendar (ADR 0004): a ``ScheduledEvent`` names its ``instructor_id`` and,
optionally, the class (``course_id``) and what runs on the range (``exercise_id``). So:

- you **teach** course C when a booking in your tenant names you its instructor and C its
  course;
- you **run** exercise E when such a booking names E as its exercise (the clock also
  writes ``exercise_id`` back when it creates the exercise for a booked scenario);
- only a live booking counts: not ``draft`` (not yet a session, holds nothing) and not
  ``cancelled``. The same states ``scheduler.service.my_sessions`` treats as "yours".
  A ``completed`` booking still counts, so an instructor can tidy their class's records
  after the session; nothing else ends it.

Why not ``created_by``: who booked a session is not who teaches it (an admin books for an
instructor). Why not the exercise's range or a time window (as the participant roster in
``exercise_participants`` uses): those decide who *takes part*, not who is in charge.

The tenant is the hard boundary (every lookup is the caller's tenant; another tenant's
row is 404). Inside it, the booking is the narrower guard: an instructor can book
themselves (schedule:write), and that booking is on the calendar and in the audit log.
The targets are narrowed too: an instructor enrols, withdraws and puts on teams only
Students, never staff.

Admins keep ``user:update`` over every team and person in their tenant; these rules are
for the holders of ``enrollment:manage_own`` / ``team:manage_own`` without it.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, Course, Exercise, Team, User, UserRole
from ..rbac import Permission, user_has_permission
from ..scheduler.models import EventState, ScheduledEvent
from ..tenancy import get_owned, tenant_uuid

NOT_LIVE = (EventState.draft, EventState.cancelled)


def _caller_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None


def _my_bookings(db: Session, user: CurrentUser):
    me = _caller_uuid(user)
    return db.query(ScheduledEvent).filter(
        ScheduledEvent.tenant_id == tenant_uuid(user),
        ScheduledEvent.instructor_id == me,
        ScheduledEvent.state.notin_(NOT_LIVE),
    )


def taught_course_ids(db: Session, user: CurrentUser) -> set[uuid.UUID]:
    """Courses of the caller's live bookings as instructor."""
    q = _my_bookings(db, user).filter(ScheduledEvent.course_id.isnot(None))
    return {cid for (cid,) in q.with_entities(ScheduledEvent.course_id).distinct()}


def run_exercise_ids(db: Session, user: CurrentUser) -> set[uuid.UUID]:
    """Exercises of the caller's live bookings as instructor."""
    q = _my_bookings(db, user).filter(ScheduledEvent.exercise_id.isnot(None))
    return {eid for (eid,) in q.with_entities(ScheduledEvent.exercise_id).distinct()}


def teaches_course(db: Session, user: CurrentUser, course_id: uuid.UUID) -> bool:
    return _my_bookings(db, user).filter(ScheduledEvent.course_id == course_id).first() is not None


def runs_exercise(db: Session, user: CurrentUser, exercise_id: uuid.UUID | None) -> bool:
    if exercise_id is None:
        return False
    return _my_bookings(db, user).filter(ScheduledEvent.exercise_id == exercise_id).first() is not None


def own_student(db: Session, user: CurrentUser, subject_id: uuid.UUID | str) -> User:
    """A Student of the caller's tenant: 404 outside it (ids are not probed across tenants),
    403 for anyone but a Student (staff accounts are an admin's to place)."""
    target = get_owned(db, User, subject_id, user, not_found="User not found")
    if target.role != UserRole.student:
        raise HTTPException(403, "You can enrol and place only Students; staff are placed by an administrator")
    return target


def authorize_enrolment_of_other(db: Session, user: CurrentUser, course: Course, subject_id: uuid.UUID) -> None:
    """Whether the caller may enrol or withdraw ``subject_id`` (not themselves) on ``course``.

    ``user:update`` (an admin): anyone in the tenant, as before #200. ``enrollment:manage_own``
    (an instructor): a Student of the tenant, on a course they teach. Otherwise 403.
    """
    if user_has_permission(user, Permission.USER_UPDATE):
        get_owned(db, User, subject_id, user, not_found="User not found")
        return
    if not user_has_permission(user, Permission.ENROLLMENT_MANAGE_OWN):
        raise HTTPException(403, f"Missing permission: {Permission.USER_UPDATE.value}")
    own_student(db, user, subject_id)
    if not teaches_course(db, user, course.id):
        raise HTTPException(403, "You can enrol Students only on a course you teach (a session booked with you as instructor)")


def authorize_team(db: Session, user: CurrentUser, team: Team) -> None:
    """Whether the caller may change ``team`` (already known to be in their tenant).

    ``user:update``: any team of the tenant. ``team:manage_own``: a team of an exercise
    they run; a standing team (no exercise) stays an admin's.
    """
    if user_has_permission(user, Permission.USER_UPDATE):
        return
    if user_has_permission(user, Permission.TEAM_MANAGE_OWN) and runs_exercise(db, user, team.exercise_id):
        return
    raise HTTPException(403, "You can change only the teams of an exercise you run")


def authorize_new_team(db: Session, user: CurrentUser, exercise_id: uuid.UUID | None) -> uuid.UUID | None:
    """The exercise a new team belongs to, checked. Another tenant's exercise is 404 for
    everyone; ``team:manage_own`` without ``user:update`` needs one they run."""
    if exercise_id is not None:
        exercise_id = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found").id
    if user_has_permission(user, Permission.USER_UPDATE):
        return exercise_id
    if exercise_id is None:
        raise HTTPException(403, "Name the exercise you run (exercise_id); standing teams are created by an administrator")
    if not runs_exercise(db, user, exercise_id):
        raise HTTPException(403, "You can create teams only for an exercise you run")
    return exercise_id


def team_member_target(db: Session, user: CurrentUser, subject_id: uuid.UUID) -> User:
    """Who may be put on a team: anyone in the tenant with ``user:update``, else a Student."""
    if user_has_permission(user, Permission.USER_UPDATE):
        return get_owned(db, User, subject_id, user, not_found="User not found")
    return own_student(db, user, subject_id)


def audit(db: Session, user: CurrentUser, action: str, resource_type: str, resource_id: str, detail: str) -> None:
    db.add(AuditLog(user_id=_caller_uuid(user), tenant_id=tenant_uuid(user), action=action,
                    resource_type=resource_type, resource_id=resource_id, detail=detail))
