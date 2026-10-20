"""Which courses a caller teaches and which exercises they run (#200, rule (a)).

Courses and exercises carry no owner or instructor column. The one record of who teaches
what is the calendar (ADR 0004): a ``ScheduledEvent`` names its ``instructor_id`` and,
optionally, the class (``course_id``) and what runs on the range (``exercise_id``).

Rule (a), chosen by the owner after the first adversarial review:

- **Scope.** You may enrol and withdraw on course C, or create and change the teams of
  exercise E, only while *your own* booking of C / E is upcoming or running: it names you
  its instructor, it has not ended (``end_time`` in the future) and it is not draft,
  cancelled or completed. A past or completed booking grants nothing.
- **No interference.** 409, never a change, while a run you are not in charge of is live:
  * another booking of the same C / E is running (any booking that is not yours, an
    admin's with no instructor included); a booking of E is one naming E or E's range, as
    ``exercise_participants`` links them. "Running" is provisioning or active, or
    scheduled with now inside its window;
  * for E: the exercise itself is running or paused and no running booking of it is yours
    (it was started by hand, or overran its booking);
  * for C: an exercise rostered through C (``roster_courses``) is running or paused and no
    running booking of it is yours, so a colleague cannot pull Students out of an
    overrunning run.
- Bookings stay self-service (schedule:write), but only an administrator moves a booking
  from one instructor to another (scheduler/router.py ``update_event``): you hand off your
  own booking, or take one with no instructor.

The tenant is the hard boundary (every lookup is the caller's tenant; another tenant's
row is 404). The targets are narrowed too: an instructor enrols, withdraws and puts on
teams only Students, and only Students with an active enrolment of ``source ==
"assigned"`` (staff enrolments, approvals, imports, LTI/Moodle sync and lab launches;
never a self-enrolment) in a course of their own booking of that exercise, because team
membership opens the exercise's AAR (aar_access.took_part). A completed or cancelled
exercise's teams are frozen, for everyone: no team created, changed, deleted, or member
added or removed (409).

Admins keep ``user:update`` over every team and person in their tenant; these rules are
for the holders of ``enrollment:manage_own`` / ``team:manage_own`` without it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..enrollment import ACTIVE_STATUSES, ASSIGNED
from ..models import (
    AuditLog,
    Course,
    Enrollment,
    Exercise,
    ExerciseState,
    Nation,
    OrganizationalUnit,
    Team,
    User,
    UserRole,
)
from ..rbac import Permission, user_has_permission
from ..scheduler.models import EventState, ScheduledEvent
from ..tenancy import get_owned, tenant_uuid

OVER = (EventState.draft, EventState.cancelled, EventState.completed)
FINISHED_EXERCISE = (ExerciseState.completed, ExerciseState.cancelled)
LIVE_RUN = (ExerciseState.running, ExerciseState.paused)


def _caller_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _my_live_bookings(db: Session, user: CurrentUser):
    """The caller's own bookings that are upcoming or running."""
    return db.query(ScheduledEvent).filter(
        ScheduledEvent.tenant_id == tenant_uuid(user),
        ScheduledEvent.instructor_id == _caller_uuid(user),
        ScheduledEvent.state.notin_(OVER),
        ScheduledEvent.end_time > _now(),
    )


def _running_now():
    now = _now()
    return or_(
        ScheduledEvent.state.in_([EventState.provisioning, EventState.active]),
        (ScheduledEvent.start_time <= now) & (ScheduledEvent.end_time > now),
    )


def _others_running(db: Session, user: CurrentUser, link):
    """Bookings matching ``link`` that are running and not the caller's."""
    me = _caller_uuid(user)
    return db.query(ScheduledEvent).filter(
        ScheduledEvent.tenant_id == tenant_uuid(user),
        link,
        ScheduledEvent.state.notin_(OVER),
        or_(ScheduledEvent.instructor_id.is_(None), ScheduledEvent.instructor_id != me),
        _running_now(),
    )


def _exercise_link(ex: Exercise):
    """A booking of ``ex``: it names the exercise, or the range it runs on (as
    exercise_participants._bookings links them)."""
    if ex.range_id is None:
        return ScheduledEvent.exercise_id == ex.id
    return or_(ScheduledEvent.exercise_id == ex.id, ScheduledEvent.range_id == ex.range_id)


def _mine_running(db: Session, user: CurrentUser, ex: Exercise) -> bool:
    """The caller's own booking of ``ex`` is running now: the live run is theirs."""
    return (
        db.query(ScheduledEvent.id)
        .filter(
            ScheduledEvent.tenant_id == tenant_uuid(user),
            ScheduledEvent.instructor_id == _caller_uuid(user),
            ScheduledEvent.state.notin_(OVER),
            ScheduledEvent.start_time <= _now(),
            ScheduledEvent.end_time > _now(),
            _exercise_link(ex),
        )
        .first()
        is not None
    )


def _live_runs_through_course(db: Session, user: CurrentUser, course_id: uuid.UUID) -> list[Exercise]:
    """Exercises running or paused now whose roster comes through ``course_id``
    (exercise_participants.roster_courses: a booking of the course over the run's time),
    whether or not that booking is still running (a run can overrun its booking)."""
    from ..exercise_participants import roster_courses

    links = (
        db.query(ScheduledEvent.exercise_id, ScheduledEvent.range_id)
        .filter(
            ScheduledEvent.tenant_id == tenant_uuid(user),
            ScheduledEvent.course_id == course_id,
            ScheduledEvent.state != EventState.cancelled,
        )
        .all()
    )
    ex_ids = {e for e, _ in links if e is not None}
    range_ids = {r for _, r in links if r is not None}
    if not ex_ids and not range_ids:
        return []
    runs = (
        db.query(Exercise)
        .filter(
            Exercise.tenant_id == tenant_uuid(user),
            Exercise.state.in_(LIVE_RUN),
            Exercise.deleted_at.is_(None),
            or_(Exercise.id.in_(ex_ids), Exercise.range_id.in_(range_ids)),
        )
        .all()
    )
    return [ex for ex in runs if course_id in roster_courses(db, ex)]


def taught_course_ids(db: Session, user: CurrentUser) -> set[uuid.UUID]:
    """Courses of the caller's upcoming or running bookings."""
    q = _my_live_bookings(db, user).filter(ScheduledEvent.course_id.isnot(None))
    return {cid for (cid,) in q.with_entities(ScheduledEvent.course_id).distinct()}


def run_exercise_ids(db: Session, user: CurrentUser) -> set[uuid.UUID]:
    """Exercises of the caller's upcoming or running bookings."""
    q = _my_live_bookings(db, user).filter(ScheduledEvent.exercise_id.isnot(None))
    return {eid for (eid,) in q.with_entities(ScheduledEvent.exercise_id).distinct()}


def _require_own(db: Session, user: CurrentUser, column, value: uuid.UUID | None, what: str) -> None:
    mine = value is not None and _my_live_bookings(db, user).filter(column == value).first() is not None
    if not mine:
        raise HTTPException(
            403, f"You can change only a {what} of your own upcoming or running session (booked with you as instructor)"
        )


def _busy(what: str) -> HTTPException:
    return HTTPException(
        409, f"Another instructor's session of this {what} is running; it can be changed when that session ends, "
        "or by an administrator"
    )


def require_course_scope(db: Session, user: CurrentUser, course_id: uuid.UUID) -> None:
    """403 without your own live session of the course. 409 while another booking of it is
    running, or while an exercise rostered through it is running or paused and is not your
    own running session's (the overrun case: a run outlasting its booking still seats the
    course's Students)."""
    _require_own(db, user, ScheduledEvent.course_id, course_id, "course")
    if _others_running(db, user, ScheduledEvent.course_id == course_id).first() is not None:
        raise _busy("course")
    for ex in _live_runs_through_course(db, user, course_id):
        if not _mine_running(db, user, ex):
            raise HTTPException(
                409, f"An exercise of this course's class ({ex.name}) is {ex.state.value}; its Students cannot be "
                "enrolled or withdrawn until it ends, except by an administrator"
            )


def require_exercise_scope(db: Session, user: CurrentUser, exercise_id: uuid.UUID | None) -> None:
    """403 without your own live session of the exercise. 409 while another booking of it
    (by the exercise or its range) is running, or while the exercise itself is running or
    paused and not under your own running session (started by hand, or overrunning)."""
    _require_own(db, user, ScheduledEvent.exercise_id, exercise_id, "exercise")
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if _others_running(db, user, _exercise_link(ex)).first() is not None:
        raise _busy("exercise")
    if ex.state in LIVE_RUN and not _mine_running(db, user, ex):
        raise HTTPException(
            409, f"The exercise is {ex.state.value} outside your own session; its teams can be changed when it ends, "
            "or by an administrator"
        )


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
    (an instructor): a Student of the tenant, on a course of their own upcoming or running
    session, and not while another instructor's session of it runs (409). Otherwise 403.
    """
    if user_has_permission(user, Permission.USER_UPDATE):
        get_owned(db, User, subject_id, user, not_found="User not found")
        return
    if not user_has_permission(user, Permission.ENROLLMENT_MANAGE_OWN):
        raise HTTPException(403, f"Missing permission: {Permission.USER_UPDATE.value}")
    own_student(db, user, subject_id)
    require_course_scope(db, user, course.id)


def authorize_team(db: Session, user: CurrentUser, team: Team) -> None:
    """Whether the caller may change ``team`` (already known to be in their tenant).

    ``user:update``: any team of the tenant. ``team:manage_own``: a team of an exercise of
    their own upcoming or running session (409 while another's runs); a standing team (no
    exercise) stays an admin's.
    """
    if user_has_permission(user, Permission.USER_UPDATE):
        return
    if not user_has_permission(user, Permission.TEAM_MANAGE_OWN) or team.exercise_id is None:
        raise HTTPException(403, "You can change only the teams of an exercise you run")
    require_exercise_scope(db, user, team.exercise_id)


def _refuse_finished(ex: Exercise) -> None:
    if ex.state in FINISHED_EXERCISE:
        raise HTTPException(409, f"The exercise is {ex.state.value}; its teams are its record and are not changed")


def refuse_frozen_team(db: Session, user: CurrentUser, team: Team) -> None:
    """409 for a team of a completed or cancelled exercise, whoever asks (admins too): its
    members are who took part in that run and read its AAR (aar_access.took_part)."""
    if team.exercise_id is None:
        return
    # tenant-safe: the team is the caller's tenant's; a soft-deleted exercise still freezes.
    ex = db.query(Exercise).filter(Exercise.id == team.exercise_id, Exercise.tenant_id == tenant_uuid(user)).first()
    if ex is not None:
        _refuse_finished(ex)


def authorize_new_team(db: Session, user: CurrentUser, exercise_id: uuid.UUID | None) -> uuid.UUID | None:
    """The exercise a new team belongs to, checked. Another tenant's exercise is 404 for
    everyone, a completed or cancelled one 409; ``team:manage_own`` without ``user:update``
    needs one of their own upcoming or running session."""
    if exercise_id is not None:
        ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
        _refuse_finished(ex)
        exercise_id = ex.id
    if user_has_permission(user, Permission.USER_UPDATE):
        return exercise_id
    if exercise_id is None:
        raise HTTPException(403, "Name the exercise you run (exercise_id); standing teams are created by an administrator")
    require_exercise_scope(db, user, exercise_id)
    return exercise_id


def team_member_target(db: Session, user: CurrentUser, team: Team, subject_id: uuid.UUID) -> User:
    """Who may be put on ``team`` (scope already checked by ``authorize_team``).

    Nobody, on a completed or cancelled exercise's team (it would open that run's AAR to
    them). ``user:update``: anyone in the tenant. Otherwise a Student with an active
    (enrolled or in_progress) enrolment of ``source == "assigned"`` (which includes LTI/Moodle
    sync and lab launches, never a self-enrolment) in a course of the caller's own live
    session of the exercise.
    """
    refuse_frozen_team(db, user, team)
    if user_has_permission(user, Permission.USER_UPDATE):
        return get_owned(db, User, subject_id, user, not_found="User not found")
    target = own_student(db, user, subject_id)
    courses = {
        cid
        for (cid,) in _my_live_bookings(db, user)
        .filter(ScheduledEvent.exercise_id == team.exercise_id, ScheduledEvent.course_id.isnot(None))
        .with_entities(ScheduledEvent.course_id)
    }
    enrolled = courses and (
        db.query(Enrollment.id)
        .filter(
            Enrollment.user_id == target.id,
            Enrollment.course_id.in_(courses),
            Enrollment.source == ASSIGNED,
            Enrollment.status.in_(ACTIVE_STATUSES),
        )
        .first()
    )
    if not enrolled:
        raise HTTPException(
            403, "Only Students enrolled in your session's course can join this exercise's teams; enrol them first"
        )
    return target


def check_team_refs(db: Session, user: CurrentUser, ou_id: uuid.UUID | None, nation_id: uuid.UUID | None) -> None:
    """A team's OU must be the caller's tenant's and its nation must exist: 422 otherwise."""
    if ou_id is not None:
        found = db.query(OrganizationalUnit.id).filter(
            OrganizationalUnit.id == ou_id, OrganizationalUnit.tenant_id == tenant_uuid(user)
        ).first()
        if not found:
            raise HTTPException(422, "ou_id is not an organisational unit of your tenant")
    if nation_id is not None and not db.query(Nation.id).filter(Nation.id == nation_id).first():
        raise HTTPException(422, "nation_id is not a known nation")


def audit(db: Session, user: CurrentUser, action: str, resource_type: str, resource_id: str, detail: str) -> None:
    db.add(AuditLog(user_id=_caller_uuid(user), tenant_id=tenant_uuid(user), action=action,
                    resource_type=resource_type, resource_id=resource_id, detail=detail))
