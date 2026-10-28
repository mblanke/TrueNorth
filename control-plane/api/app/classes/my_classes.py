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
from ..models import Course, Range, RangeState, User
from ..rbac import Permission, require_permission
from ..scheduler.lifecycle import HOLDING
from ..scheduler.models import BookingApproval, EventState, ScheduledEvent
from ..scheduler.schemas import as_utc
from ..scheduler.service import ApprovedWindow
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
SLACK = timedelta(days=1)
EPSILON = timedelta(microseconds=1)

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


# Built but powered off, or powering back on: ``ranges.status`` calls these ready (staff see
# the range's own state); a Student cannot use it yet, so Join waits.
PAUSED = (RangeState.stopping, RangeState.stopped, RangeState.starting)


def _now() -> datetime:
    return datetime.now(UTC)


@router.get(
    "/attending", response_model=AttendingClassesOut, operation_id="listAttendingClasses",
    summary="My classes: the caller's own classes as a Student, their status and Join",
)
def list_attending_classes(
    start: datetime | None = Query(
        None, description="The window's start (default: now). Send it with the viewer's UTC offset: class dates "
        "are calendar days, read in that offset"),
    end: datetime | None = Query(
        None, description="The window's end, exclusive (default: a week after start; at most 45 days)"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_ATTEND)),
) -> AttendingClassesOut:
    """The classes whose roll call the caller is on (or lost their seat on) and that run in
    the window, with their sessions in it, the caller's seat and onboarding, and the caller's
    own range for the current or next session.  **class:attend**"""
    now = _now()
    lo = as_utc(start) if start else now
    hi = as_utc(end) if end else lo + DEFAULT_WINDOW
    # The window's calendar days, in the viewer's own offset when the times carry one (a
    # Monday-to-Monday week in EST is 05:00Z to 05:00Z); its end is exclusive (N2).
    first_day = (start if start is not None and start.tzinfo else lo).date()
    local_end = end if end is not None and end.tzinfo else (
        hi.astimezone(start.tzinfo) if start is not None and start.tzinfo else hi)
    last_day = (local_end - EPSILON).date()
    if hi <= lo:
        raise HTTPException(422, "The window's end must be after its start")
    if hi - lo > MAX_WINDOW:
        raise HTTPException(422, "The window is at most 45 days")
    tid, uid = tenant_uuid(user), uuid.UUID(user.id)
    ack = acknowledgement.status(db, uid, tid)

    # A day's slack each side: class dates are local calendar days and the window is UTC, and
    # a last session may run past the end date (``ranges._ends``). A class found only through
    # the slack is kept only when it has a session in the window.
    rows = (
        db.query(RollCallEntry, Class)
        .join(Class, Class.id == RollCallEntry.class_id)
        .filter(
            RollCallEntry.user_id == uid,
            RollCallEntry.tenant_id == tid,
            Class.tenant_id == tid,
            RollCallEntry.status.in_(MINE),
            Class.status == ClassStatus.open.value,
            Class.start_date <= (hi + SLACK).date(),
            Class.end_date >= (lo - SLACK).date(),
        )
        .order_by(Class.start_date, Class.name)
        .limit(100)
        .all()
    )
    facts = _Facts.load(db, tid, rows)
    out = []
    for entry, cls in rows:
        c = _class_out(facts, cls, entry, lo, hi, now)
        in_dates = cls.start_date <= last_day and cls.end_date >= first_day
        if in_dates or c.sessions:
            out.append(c)
    return AttendingClassesOut(acknowledgement_signed=not ack.required, window_start=lo, window_end=hi, classes=out)


class _Facts:
    """Everything the page needs about the caller's classes, read in a fixed number of
    queries whatever their count (N1), all scoped to the caller's tenant."""

    def __init__(self):
        self.courses: dict[uuid.UUID, str] = {}
        self.leads: dict[uuid.UUID, str] = {}
        self.events: dict[uuid.UUID, list[tuple[ScheduledEvent, str]]] = {}
        self.rows: dict[uuid.UUID, list[ClassRange]] = {}
        self.ranges: dict[uuid.UUID, Range] = {}
        self.approved_at: dict[uuid.UUID, datetime] = {}

    @classmethod
    def load(cls, db: Session, tid: uuid.UUID, rows: list[tuple[RollCallEntry, Class]]) -> _Facts:
        f = cls()
        if not rows:
            return f
        ids = [c.id for _, c in rows]
        # A shared catalogue course may belong to another tenant (CourseRangeMode): only its name.
        f.courses = dict(db.query(Course.id, Course.name).filter(Course.id.in_({c.course_id for _, c in rows})).all())
        # tenant-safe: the classes' own leads, in the caller's tenant.
        f.leads = dict(db.query(User.id, User.display_name).filter(
            User.id.in_({c.lead_instructor_id for _, c in rows}), User.tenant_id == tid).all())
        # tenant-safe: the classes' own sessions, in the caller's tenant.
        for evt, mode, class_id in (
            db.query(ScheduledEvent, ClassSession.range_mode, ClassSession.class_id)
            .join(ClassSession, ClassSession.event_id == ScheduledEvent.id)
            .filter(
                ClassSession.class_id.in_(ids),
                ClassSession.tenant_id == tid,
                ScheduledEvent.tenant_id == tid,
                ScheduledEvent.state.in_(SHOWN),
            )
            .order_by(ScheduledEvent.start_time, ScheduledEvent.id)
            .limit(500 * len(ids))
        ):
            f.events.setdefault(class_id, []).append((evt, mode))
        # The caller's own seats and the classes' shared ranges, standing ones only; never
        # another Student's seat, and nothing for a seat they lost.
        mine = [e.id for e, _ in rows if e.status != RollCallStatus.forfeited.value]
        on = [c.id for e, c in rows if e.status != RollCallStatus.forfeited.value]
        if on:
            # tenant-safe: the classes' own ranges, in the caller's tenant.
            for r in db.query(ClassRange).filter(
                ClassRange.class_id.in_(on),
                ClassRange.tenant_id == tid,
                ClassRange.torn_down_at.is_(None),
                ((ClassRange.kind == ClassRangeKind.seat.value) & ClassRange.entry_id.in_(mine))
                | ((ClassRange.kind == ClassRangeKind.shared.value) & (ClassRange.seat_key == SHARED_SEAT)),
            ):
                f.rows.setdefault(r.class_id, []).append(r)
        # Their ranges, and the ranges collective sessions book (an exercise's own range, N3).
        # A deleted range opens nothing (``readable_range``). tenant-safe: the caller's tenant.
        range_ids = {r.range_id for rs in f.rows.values() for r in rs if r.range_id}
        range_ids |= {e.range_id for es in f.events.values() for e, mode in es
                      if e.range_id and mode == RangeMode.collective.value}
        if range_ids:
            f.ranges = {r.id: r for r in db.query(Range).filter(
                Range.id.in_(range_ids), Range.tenant_id == tid, Range.deleted_at.is_(None))}
        # The forfeit deadline's approvals (``forfeit.class_windows``), for seats they lost only.
        lost = [c.id for e, c in rows if e.status == RollCallStatus.forfeited.value]
        lost_events = [e.id for cid in lost for e, _ in f.events.get(cid, [])]
        if lost_events:
            f.approved_at = {
                eid: as_utc(at) for eid, at in db.query(BookingApproval.event_id, BookingApproval.decided_at).filter(
                    BookingApproval.event_id.in_(lost_events)) if at is not None
            }
        return f

    def windows(self, cls: Class) -> list[ApprovedWindow]:
        """As ``scheduler.approved_windows`` reads them: approved and holding, or over."""
        return [
            ApprovedWindow(as_utc(e.start_time), as_utc(e.end_time), self.approved_at.get(e.id))
            for e, _ in self.events.get(cls.id, [])
        ]


def _class_out(
    f: _Facts, cls: Class, entry: RollCallEntry, lo: datetime, hi: datetime, now: datetime
) -> AttendingClassOut:
    forfeited = entry.status == RollCallStatus.forfeited.value
    rows = [] if forfeited else f.rows.get(cls.id, [])
    rngs = f.ranges

    sessions_out: list[AttendingSessionOut] = []
    live_now: list[tuple[ScheduledEvent, str]] = []
    to_come: list[tuple[ScheduledEvent, str]] = []
    for evt, mode in f.events.get(cls.id, []):
        s, e = as_utc(evt.start_time), as_utc(evt.end_time)
        live = evt.state in UNDER_WAY and s <= now < e
        status = None if forfeited else _status(evt, mode, rows, rngs)
        if s < hi and e > lo:
            sessions_out.append(AttendingSessionOut(
                event_id=evt.id, name=evt.name, start_time=s, end_time=e, range_mode=mode, live=live,
                range_status=status,
            ))
        if e > now and evt.state in HOLDING:
            (live_now if live else to_come).append((evt, mode))

    # The session the page is about: one under way whose range opens now (an exercise inside a
    # day-long lab wins), else the first under way or to come that uses a range.
    range_status = join = target = None
    if not forfeited:
        # A collective session under way first: its shared range opens only now.
        live_now.sort(key=lambda em: em[1] != RangeMode.collective.value)
        for evt, mode in live_now:
            row = _row_for(evt, mode, rows)
            if _status(evt, mode, rows, rngs) == AttendingRangeStatus.ready and _may_join(evt, row, rngs, now):
                target, range_status, join = evt, AttendingRangeStatus.ready, row.range_id
                break
        if target is None:
            for evt, mode in live_now + to_come:
                status = _status(evt, mode, rows, rngs)
                if status is None:
                    continue  # a session with no range (a lecture): look further
                row = _row_for(evt, mode, rows)
                target, range_status = evt, status
                if status == AttendingRangeStatus.ready and _may_join(evt, row, rngs, now):
                    join = row.range_id
                break
    if target is None and (live_now or to_come):
        target = (live_now + to_come)[0][0]
    windows = f.windows(cls) if forfeited else None
    return AttendingClassOut(
        id=cls.id, name=cls.name, course_name=f.courses.get(cls.course_id, ""),
        lead_instructor=f.leads.get(cls.lead_instructor_id), start_date=cls.start_date,
        end_date=cls.end_date, onboarded=entry.status in ONBOARDED,
        seat=AttendingSeatStatus.forfeited if forfeited else AttendingSeatStatus.ready,
        forfeit_deadline=forfeit.deadline(windows, entry) if windows else None,
        forfeited_at=entry.forfeited_at if forfeited else None,
        sessions=sessions_out, range_status=range_status, next_session_id=target.id if target else None,
        join_range_id=join,
    )


def _row_for(evt: ScheduledEvent, mode: str, rows: list[ClassRange]) -> ClassRange | None:
    """The caller's range for a session: their own seat from its template (individual), or
    the class's shared range it books, else the one built from its template (collective)."""
    if mode == RangeMode.individual.value:
        return next((r for r in rows if r.kind == ClassRangeKind.seat.value and r.template_id == evt.template_id),
                    None) if evt.template_id else None
    shared = [r for r in rows if r.kind == ClassRangeKind.shared.value]
    if evt.range_id is not None:
        # The range it books, if it is the class's own (else ``_status`` reads that range itself).
        return next((r for r in shared if r.range_id == evt.range_id), None)
    return next((r for r in shared if r.template_id == evt.template_id), None) if evt.template_id else None


def _uses_range(evt: ScheduledEvent, mode: str) -> bool:
    return evt.template_id is not None or (mode == RangeMode.collective.value and evt.range_id is not None)


# A range's own state, for a range the class does not own (N3).
_STATE = {
    RangeState.created: AttendingRangeStatus.not_built,
    RangeState.provisioning: AttendingRangeStatus.building,
    RangeState.failed: AttendingRangeStatus.failed,
    RangeState.destroying: AttendingRangeStatus.not_built,
    RangeState.destroyed: AttendingRangeStatus.not_built,
}


def _status(evt: ScheduledEvent, mode: str, rows: list[ClassRange], rngs: dict) -> AttendingRangeStatus | None:
    """None: the session uses no range. A range not created yet (before the provisioning
    lead), torn down or deleted: not built. A collective session on a range the class does
    not own (an exercise's own range): that range's state."""
    if not _uses_range(evt, mode):
        return None
    row = _row_for(evt, mode, rows)
    if row is None:
        booked = rngs.get(evt.range_id) if mode == RangeMode.collective.value and evt.range_id else None
        if booked is None:
            return AttendingRangeStatus.not_built
        return AttendingRangeStatus.paused if booked.state in PAUSED else _STATE.get(
            booked.state, AttendingRangeStatus.ready)
    rng = rngs.get(row.range_id) if row.range_id else None
    status = _STATUS[ranges.status(row, rng)]
    if status == AttendingRangeStatus.ready and rng is not None and rng.state in PAUSED:
        return AttendingRangeStatus.paused
    return status


def _may_join(evt: ScheduledEvent, row: ClassRange | None, rngs: dict, now: datetime) -> bool:
    """As ``ranges.student_participation`` and ``readable_range`` decide it: an own seat whose
    range exists, whenever it stands; the shared range only while a session booking it is
    under way. Never a range the class does not own (``row`` is None)."""
    if row is None or row.range_id is None or row.range_id not in rngs:
        return False
    if row.kind == ClassRangeKind.seat.value:
        return True
    return (evt.state in UNDER_WAY and evt.range_id == row.range_id
            and as_utc(evt.start_time) <= now < as_utc(evt.end_time))
