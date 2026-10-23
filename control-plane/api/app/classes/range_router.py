"""A class's ranges, its sessions' modes and teams (ADR 0014 §9-11, §19; issue #282).

=====================================================  ========================================
Endpoint                                               Who
=====================================================  ========================================
GET  /classes/course-range-modes/{course_id}           class:create: the course's default mode
PUT  /classes/course-range-modes/{course_id}           course:author: set it for the tenant
GET  /classes/{id}/ranges                              its staff (every seat), its Students
                                                       (their own seat and shared ranges)
POST /classes/{id}/ranges/{class_range_id}/retry       its lead or an admin: a failed build
GET  /classes/{id}/sessions/{event_id}/teams           its lead, assistants, observers, admins
POST /classes/{id}/sessions/{event_id}/teams           its lead or an admin: balanced split
PUT  /classes/{id}/sessions/{event_id}/teams/members/  its lead or an admin: move a Student
     {user_id}
=====================================================  ========================================

No endpoint here carries a credential. A Student reads their own seat's through
``/ranges/{range_id}/credentials`` (ADR 0013 §3, audited); instructors through the same;
observers never (ADR 0014 §19). Another tenant's class, or one the caller has no part in, is
404 (``access``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import AuditLog, Range, User
from ..rbac import Permission, require_permission
from ..tenancy import tenant_uuid
from . import access, ranges, service, sessions, teams
from .models import ClassRange, ClassRangeKind, ClassSession, CourseRangeMode, RangeMode, RollCallEntry
from .range_schemas import (
    ClassRangeRowOut,
    ClassRangesOut,
    ClassRangeStatus,
    ClassSessionModeOut,
    CourseRangeModeIn,
    CourseRangeModeOut,
    SessionTeamMember,
    SessionTeamOut,
    SessionTeamsOut,
    TeamMoveIn,
    TeamSplitIn,
    UnassignedOut,
)
from .schemas import ClassRole

router = APIRouter(prefix="/classes", tags=["classes"])


# ── The course's default mode ────────────────────────────────────────────────


@router.get(
    "/course-range-modes/{course_id}", response_model=CourseRangeModeOut, operation_id="getCourseRangeMode",
    summary="A course's default range mode for class sessions",
)
def get_course_range_mode(
    course_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_CREATE)),
) -> CourseRangeModeOut:
    """The caller's organisation's default for this course: individual (a range per Student)
    or collective (one shared range). Collective until set.  **class:create**"""
    course = _course(db, user, course_id)
    mode, source = sessions.course_mode(db, tenant_uuid(user), course.id)
    return CourseRangeModeOut(course_id=course.id, mode=mode.value, source=source)


@router.put(
    "/course-range-modes/{course_id}", response_model=CourseRangeModeOut, operation_id="setCourseRangeMode",
    summary="Set a course's default range mode for class sessions",
)
def set_course_range_mode(
    course_id: uuid.UUID,
    body: CourseRangeModeIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.COURSE_AUTHOR)),
) -> CourseRangeModeOut:
    """For the caller's organisation only; sessions already booked keep their mode. Audited.
    **course:author**"""
    course = _course(db, user, course_id)
    tid = tenant_uuid(user)
    # tenant-safe: keyed by the caller's own tenant id.
    row = db.get(CourseRangeMode, (tid, course.id))
    if row is None:
        row = CourseRangeMode(tenant_id=tid, course_id=course.id, mode=body.mode.value)
        db.add(row)
    row.mode, row.updated_by = body.mode.value, uuid.UUID(user.id)
    db.add(AuditLog(user_id=uuid.UUID(user.id), tenant_id=tid, action="course_range_mode_set", resource_type="course",
                    resource_id=str(course.id), detail=body.mode.value))
    db.commit()
    return CourseRangeModeOut(course_id=course.id, mode=body.mode.value, source="setting")


def _course(db: Session, user: CurrentUser, course_id: uuid.UUID):
    try:
        return service.course_for(db, tenant_uuid(user), course_id)
    except HTTPException:
        raise HTTPException(404, "Course not found") from None


# ── A class's ranges ─────────────────────────────────────────────────────────


@router.get(
    "/{class_id}/ranges", response_model=ClassRangesOut, operation_id="getClassRanges",
    summary="A class's sessions (with their mode) and its ranges (with their status)",
)
def get_class_ranges(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ClassRangesOut:
    """Its lead, assistants, observers and admins see every seat, with the Student and why a
    build failed. A Student on its roll call sees their own seat and the shared ranges only.
    No credentials here."""
    cls, role = access.get_visible(db, class_id, user)
    staff = role in access.STAFF
    rows = ranges.rows_of(db, cls)
    entries = {
        e.id: e for e in db.query(RollCallEntry).filter(
            RollCallEntry.class_id == cls.id, RollCallEntry.tenant_id == cls.tenant_id)
    }
    if not staff:
        mine = {eid for eid, e in entries.items() if str(e.user_id) == str(user.id)}
        rows = [r for r in rows if r.kind == ClassRangeKind.shared.value or r.entry_id in mine]
    # tenant-safe: the class's own ranges and the names of its own roll call.
    rngs = {r.id: r for r in db.query(Range).filter(
        Range.id.in_([r.range_id for r in rows if r.range_id]), Range.tenant_id == cls.tenant_id)} if rows else {}
    names = dict(db.query(User.id, User.display_name).filter(
        User.id.in_({e.user_id for e in entries.values() if e.user_id}), User.tenant_id == cls.tenant_id).all())
    out = []
    for r in rows:
        rng = rngs.get(r.range_id)
        entry = entries.get(r.entry_id) if r.entry_id else None
        out.append(ClassRangeRowOut(
            id=r.id, kind=r.kind, template_id=r.template_id, entry_id=r.entry_id,
            email=entry.email if (entry and staff) else None,
            display_name=names.get(entry.user_id) if entry and entry.user_id else None,
            range_id=r.range_id, status=ranges.status(r, rng), error=ranges.error_of(r, rng) if staff else None,
            builds=r.builds or 0, built_at=r.built_at, torn_down_at=r.torn_down_at, teardown_reason=r.teardown_reason,
        ))
    return ClassRangesOut(
        sessions=_sessions(db, cls, staff), ranges=out,
        failed=sum(1 for r in out if r.status == ClassRangeStatus.failed),
    )


def _sessions(db: Session, cls, staff: bool) -> list[ClassSessionModeOut]:
    from ..scheduler.lifecycle import HOLDING
    from ..scheduler.models import ScheduledEvent
    from ..scheduler.schemas import as_utc

    q = (
        db.query(ScheduledEvent, ClassSession.range_mode)
        .join(ClassSession, ClassSession.event_id == ScheduledEvent.id)
        .filter(ClassSession.class_id == cls.id, ScheduledEvent.tenant_id == cls.tenant_id)
    )
    if not staff:
        q = q.filter(ScheduledEvent.state.in_(HOLDING))
    return [
        ClassSessionModeOut(
            event_id=e.id, name=e.name, state=e.state.value, start_time=as_utc(e.start_time),
            end_time=as_utc(e.end_time), range_mode=mode, template_id=e.template_id, range_id=e.range_id,
        )
        for e, mode in q.order_by(ScheduledEvent.start_time).limit(200).all()
    ]


@router.post(
    "/{class_id}/ranges/{class_range_id}/retry", response_model=ClassRangeRowOut, operation_id="retryClassRange",
    summary="Build a class range whose build failed again",
)
def retry_class_range(
    class_id: uuid.UUID,
    class_range_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_CREATE)),
) -> ClassRangeRowOut:
    """Its lead or an admin. Only a range whose build failed (409 otherwise). Audited."""
    cls, role = access.get_visible(db, class_id, user)
    access.require_editor(role)
    row = (
        db.query(ClassRange)
        .filter(ClassRange.id == class_range_id, ClassRange.class_id == cls.id, ClassRange.tenant_id == cls.tenant_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Class range not found")
    ranges.retry(db, cls, row, user)
    db.refresh(row)
    # tenant-safe: the class's own range.
    rng = db.get(Range, row.range_id)
    return ClassRangeRowOut(
        id=row.id, kind=row.kind, template_id=row.template_id, entry_id=row.entry_id, range_id=row.range_id,
        status=ranges.status(row, rng), error=ranges.error_of(row, rng), builds=row.builds or 0,
        built_at=row.built_at, torn_down_at=row.torn_down_at, teardown_reason=row.teardown_reason,
    )


# ── Teams (collective sessions) ──────────────────────────────────────────────


def _teams_out(db: Session, cls, link, evt, role: ClassRole) -> SessionTeamsOut:
    from ..models import TeamMembership

    rows = teams.teams_of(db, link)
    members = (
        db.query(TeamMembership.team_id, User.id, User.display_name)
        .join(User, User.id == TeamMembership.user_id)
        .filter(TeamMembership.team_id.in_([t.id for t in rows]), User.tenant_id == cls.tenant_id)
        .order_by(User.display_name)
        .all()
        if rows else []
    )
    _, waiting = teams.roster(db, cls)
    editable = role in access.EDITORS and link.range_mode == RangeMode.collective.value and not teams.started(evt)
    return SessionTeamsOut(
        event_id=evt.id, editable=editable,
        teams=[
            SessionTeamOut(id=t.id, name=t.name, members=[
                SessionTeamMember(user_id=uid, display_name=name) for tid, uid, name in members if tid == t.id])
            for t in rows
        ],
        unassigned=[UnassignedOut(email=e.email) for e in waiting],
    )


def _session(db: Session, class_id: uuid.UUID, event_id: uuid.UUID, user: CurrentUser, *, edit: bool):
    cls, role = access.get_visible(db, class_id, user)
    if edit:
        access.require_editor(role)
    else:
        access.require_staff(role)
    link, evt = teams.session_of(db, cls, event_id)
    return cls, role, link, evt


@router.get(
    "/{class_id}/sessions/{event_id}/teams", response_model=SessionTeamsOut, operation_id="getClassSessionTeams",
    summary="A collective session's teams",
)
def get_session_teams(
    class_id: uuid.UUID,
    event_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_READ)),
) -> SessionTeamsOut:
    """Its lead, assistants, observers and admins.  **class:read**"""
    cls, role, link, evt = _session(db, class_id, event_id, user, edit=False)
    return _teams_out(db, cls, link, evt, role)


@router.post(
    "/{class_id}/sessions/{event_id}/teams", response_model=SessionTeamsOut, operation_id="splitClassSessionTeams",
    summary="Split the roll call into balanced teams for a collective session",
)
def split_session_teams(
    class_id: uuid.UUID,
    event_id: uuid.UUID,
    body: TeamSplitIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_CREATE)),
) -> SessionTeamsOut:
    """Its lead or an admin, before the session starts (409 after, or for an individual
    session). Sizes differ by at most one; splitting again places everyone again. Audited."""
    cls, role, link, evt = _session(db, class_id, event_id, user, edit=True)
    teams.split(db, cls, link, evt, body.team_count, user)
    db.commit()
    return _teams_out(db, cls, link, evt, role)


@router.put(
    "/{class_id}/sessions/{event_id}/teams/members/{user_id}", response_model=SessionTeamsOut,
    operation_id="moveClassSessionTeamMember", summary="Move a Student to another of a session's teams",
)
def move_session_team_member(
    class_id: uuid.UUID,
    event_id: uuid.UUID,
    user_id: uuid.UUID,
    body: TeamMoveIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_CREATE)),
) -> SessionTeamsOut:
    """Its lead or an admin, before the session starts. Only a Student on the roll call (422).
    Audited."""
    cls, role, link, evt = _session(db, class_id, event_id, user, edit=True)
    teams.move(db, cls, link, evt, user_id, body.team_id, user)
    db.commit()
    return _teams_out(db, cls, link, evt, role)


