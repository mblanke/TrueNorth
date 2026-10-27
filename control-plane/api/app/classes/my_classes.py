"""A Student's "My classes" page (ADR 0014 §20; issue #284).

==================================  ===========================================================
Endpoint                            Who
==================================  ===========================================================
GET /classes/attending              class:attend (Students): the caller's own classes, by their
                                    own roll-call entries, in their own tenant
==================================  ===========================================================

Why a page of its own rather than ``GET /classes`` plus ``/classes/{id}/ranges``: a Student
who lost their seat is off the roll call, so ``/classes`` no longer shows that class, and the
page has to tell them to ask for reinstatement (ADR 0014 §4). One call also answers, for every
class at once, which range Join opens.

**What it shows, per class:** its name, course, lead instructor's name (no email), dates and
sessions in the window asked for; the caller's seat (on the roll call, or forfeited with the
deadline they missed); whether they have onboarded; and their range for the current or next
session (not built, building, ready or failed, never why a build failed).

**Join** (``join_range_id``) is set only when the credentials endpoint would hand them over
(``ranges.student_participation``): their own seat, ready, while they are on the roll call of
an open class; or the class's shared range, ready, while a collective session on it is under
way. The credentials endpoint decides again on every call: this is a hint, not the gate.

**Never here:** another Student's seat, name or email; any staff email; a credential.
Removed entries and additions awaiting an approver are not the caller's classes. Another
tenant's classes are never read: entries and classes are filtered on the caller's tenant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..acknowledgement import rule as acknowledgement
from ..auth import CurrentUser
from ..db import get_db
from ..models import Course, Range, User
from ..rbac import Permission, require_permission
from ..scheduler.lifecycle import HOLDING
from ..scheduler.models import EventState, ScheduledEvent
from ..scheduler.schemas import as_utc
from ..tenancy import tenant_uuid
from . import forfeit, ranges
from .models import (
    SHARED_SEAT,
    Class,
    ClassRange,
    ClassRangeKind,
    ClassSession,
    ClassStatus,
    RangeMode,
    RollCallEntry,
    RollCallStatus,
)
from .my_classes_schemas import (
    AttendingClassesOut,
    AttendingClassOut,
    AttendingRangeStatus,
    AttendingSeatStatus,
    AttendingSessionOut,
)

router = APIRouter(prefix="/classes", tags=["classes"])

# The page's week or month, with room for a month view's six-week grid.
MAX_WINDOW = timedelta(days=45)
DEFAULT_WINDOW = timedelta(days=7)

# Entries that are the caller's classes: on the roll call, or forfeited (to be reinstated).
MINE = (RollCallStatus.invited.value, RollCallStatus.signed_in.value, RollCallStatus.ready.value,
        RollCallStatus.forfeited.value)
ONBOARDED = (RollCallStatus.signed_in.value, RollCallStatus.ready.value)

# Sessions shown: approved ones, and those already over. Never drafts, pending, rejected or cancelled.
SHOWN = (*HOLDING, EventState.completed)
# A collective session hands out its shared range while it is under way (as student_participation).
UNDER_WAY = (EventState.provisioning, EventState.active)

# ranges.status -> what a Student is told. Never why a build failed (staff only).
_STATUS = {
    "pending": AttendingRangeStatus.not_built,
    "building": AttendingRangeStatus.building,
    "ready": AttendingRangeStatus.ready,
    "failed": AttendingRangeStatus.failed,
    "tearing_down": AttendingRangeStatus.not_built,
    "torn_down": AttendingRangeStatus.not_built,
}


def _now() -> datetime:
    return datetime.now(UTC)


@router.get(
    "/attending", response_model=AttendingClassesOut, operation_id="listAttendingClasses",
    summary="My classes: the caller's own classes as a Student, their status and Join",
)
def list_attending_classes(
    start: datetime | None = Query(None, description="The window's start (default: now)"),
    end: datetime | None = Query(None, description="The window's end (default: a week after start; at most 45 days)"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_ATTEND)),
) -> AttendingClassesOut:
    """The classes whose roll call the caller is on (or lost their seat on) and that run in
    the window, with their sessions in it, the caller's seat and onboarding, and the caller's
    own range for the current or next session.  **class:attend**"""
    now = _now()
    lo = as_utc(start) if start else now
    hi = as_utc(end) if end else lo + DEFAULT_WINDOW
    if hi <= lo:
        raise HTTPException(422, "The window's end must be after its start")
    if hi - lo > MAX_WINDOW:
        raise HTTPException(422, "The window is at most 45 days")
    tid, uid = tenant_uuid(user), uuid.UUID(user.id)
    ack = acknowledgement.status(db, uid, tid)

    rows = (
        db.query(RollCallEntry, Class)
        .join(Class, Class.id == RollCallEntry.class_id)
        .filter(
            RollCallEntry.user_id == uid,
            RollCallEntry.tenant_id == tid,
            Class.tenant_id == tid,
            RollCallEntry.status.in_(MINE),
            Class.status == ClassStatus.open.value,
            Class.start_date <= hi.date(),
            Class.end_date >= lo.date(),
        )
        .order_by(Class.start_date, Class.name)
        .limit(100)
        .all()
    )
    out = [_class_out(db, cls, entry, lo, hi, now) for entry, cls in rows]
    return AttendingClassesOut(acknowledgement_signed=not ack.required, window_start=lo, window_end=hi, classes=out)


def _class_out(db: Session, cls: Class, entry: RollCallEntry, lo: datetime, hi: datetime, now: datetime) -> AttendingClassOut:
    # tenant-safe: the class's own course and lead, in the class's tenant.
    course = db.query(Course.name).filter(Course.id == cls.course_id).scalar() or ""
    lead = (
        db.query(User.display_name)
        .filter(User.id == cls.lead_instructor_id, User.tenant_id == cls.tenant_id)
        .scalar()
    )
    events = (
        db.query(ScheduledEvent, ClassSession.range_mode)
        .join(ClassSession, ClassSession.event_id == ScheduledEvent.id)
        .filter(
            ClassSession.class_id == cls.id,
            ClassSession.tenant_id == cls.tenant_id,
            ScheduledEvent.tenant_id == cls.tenant_id,
            ScheduledEvent.state.in_(SHOWN),
        )
        .order_by(ScheduledEvent.start_time)
        .limit(500)
        .all()
    )
    forfeited = entry.status == RollCallStatus.forfeited.value
    seat_rows = {} if forfeited else _seat_rows(db, cls, entry)
    rngs = _ranges(db, cls, seat_rows)

    sessions_out: list[AttendingSessionOut] = []
    upcoming: list[tuple[ScheduledEvent, str]] = []
    for evt, mode in events:
        s, e = as_utc(evt.start_time), as_utc(evt.end_time)
        live = evt.state in UNDER_WAY and s <= now < e
        status = None if forfeited else _status(_row_for(evt, mode, seat_rows), rngs)
        if s < hi and e > lo:
            sessions_out.append(AttendingSessionOut(
                event_id=evt.id, name=evt.name, start_time=s, end_time=e, range_mode=mode, live=live,
                range_status=status,
            ))
        if e > now and evt.state in HOLDING:
            upcoming.append((evt, mode))

    nxt = upcoming[0] if upcoming else None
    range_status = join = None
    if nxt is not None and not forfeited:
        evt, mode = nxt
        row = _row_for(evt, mode, seat_rows)
        range_status = _status(row, rngs)
        if range_status == AttendingRangeStatus.ready and _may_join(evt, mode, row, now):
            join = row.range_id
    windows = forfeit.class_windows(db, cls) if forfeited else None
    return AttendingClassOut(
        id=cls.id, name=cls.name, course_name=course, lead_instructor=lead, start_date=cls.start_date,
        end_date=cls.end_date, onboarded=entry.status in ONBOARDED,
        seat=AttendingSeatStatus.forfeited if forfeited else AttendingSeatStatus.ready,
        forfeit_deadline=forfeit.deadline(windows, entry) if windows else None,
        forfeited_at=entry.forfeited_at if forfeited else None,
        sessions=sessions_out, range_status=range_status, next_session_id=nxt[0].id if nxt else None,
        join_range_id=join,
    )


def _seat_rows(db: Session, cls: Class, entry: RollCallEntry) -> dict[tuple[str, uuid.UUID], ClassRange]:
    """The caller's own seats and the class's shared ranges, by (kind, template), standing
    ones only. Never another Student's seat."""
    # tenant-safe: the class's own ranges, in its tenant.
    rows = (
        db.query(ClassRange)
        .filter(
            ClassRange.class_id == cls.id,
            ClassRange.tenant_id == cls.tenant_id,
            ClassRange.torn_down_at.is_(None),
            ((ClassRange.kind == ClassRangeKind.seat.value) & (ClassRange.entry_id == entry.id))
            | ((ClassRange.kind == ClassRangeKind.shared.value) & (ClassRange.seat_key == SHARED_SEAT)),
        )
        .all()
    )
    return {(r.kind, r.template_id): r for r in rows}


def _ranges(db: Session, cls: Class, rows: dict) -> dict[uuid.UUID, Range]:
    ids = [r.range_id for r in rows.values() if r.range_id]
    if not ids:
        return {}
    # tenant-safe: the class's own ranges.
    return {r.id: r for r in db.query(Range).filter(Range.id.in_(ids), Range.tenant_id == cls.tenant_id)}


def _row_for(evt: ScheduledEvent, mode: str, rows: dict) -> ClassRange | None:
    """The caller's range for a session: their own seat (individual) or the class's shared
    range (collective), from the session's template."""
    if evt.template_id is None:
        return None
    kind = ClassRangeKind.seat if mode == RangeMode.individual.value else ClassRangeKind.shared
    row = rows.get((kind.value, evt.template_id))
    if row is not None and kind == ClassRangeKind.shared and evt.range_id is not None and row.range_id != evt.range_id:
        return None  # the session books a range that is not the class's: not handed out here
    return row


def _status(row: ClassRange | None, rngs: dict) -> AttendingRangeStatus | None:
    if row is None:
        return None
    return _STATUS[ranges.status(row, rngs.get(row.range_id) if row.range_id else None)]


def _may_join(evt: ScheduledEvent, mode: str, row: ClassRange | None, now: datetime) -> bool:
    """As ``ranges.student_participation`` decides it: an own seat whenever it stands; the
    shared range only while a collective session on it is under way."""
    if row is None or row.range_id is None:
        return False
    if row.kind == ClassRangeKind.seat.value:
        return True
    return (evt.state in UNDER_WAY and evt.range_id == row.range_id
            and as_utc(evt.start_time) <= now < as_utc(evt.end_time))
