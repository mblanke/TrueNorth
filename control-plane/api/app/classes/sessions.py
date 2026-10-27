"""A class's sessions: its scheduler bookings (ADR 0014 §8, §14-17; issue #281).

- Only the class's lead (or an admin of its tenant) books a session for it, and only while
  the class is open (``class_for_booking``). The booking then waits for an approver.
- Once approved, a session's invites and reminders go to the class's roll call
  (``roll_call_for_booking``), not to its course's enrolment.
- Roll-call changes after approval (§17): taking a Student off is always allowed; adding one
  is automatic while every approved session still has room for one more seat
  (``room_for_seats``); an addition that does not fit waits for an approver.

**Individual or collective (ADR 0014 §9; #282).** Each session has a mode, set when it is
booked: the course's default for the tenant (``course_mode``; collective when none is set)
unless the instructor picks the other. Changing it later is a change of size, so an approved
session goes back for approval (§16).

- *Collective*: one shared range, sized by the session's template (or its typed size), as
  every booking was before. A seat adds nothing.
- *Individual*: one small range per Student on the roll call, each built from the session's
  range template, so the session needs a template and names no range of its own. **A seat
  costs the template's size** (``seat_demand``); the session's size is that times its seats
  (``session_demand``), and approval sizes it from the roll call at that moment
  (``size_for_approval``).

**Roll-call changes after approval, deliberately (the #297 review note).** An addition that
fits every approved session (``room_for_seats``, under the booking lock) grows them by one
seat (``take_seats``) with no approver: that is §17's rule, not a size change by the
instructor, so it never re-pends a session. The instructor editing the session afterwards
does not either: the scheduler sizes a class session through ``session_demand``, which
counts the same roll call, so an unchanged edit asks for the size it already holds. An
addition that does not fit waits for an approver whatever the over-capacity policy.

The ranges themselves are ``ranges.py``'s. The scheduler is reached only through
``scheduler.service`` (ADR 0004 §1).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import Template
from ..range_topology import template_demand
from ..scheduler import service as scheduler
from ..scheduler.capacity import Resources
from ..scheduler.models import ScheduledEvent
from . import access
from .models import (
    DEFAULT_RANGE_MODE,
    OFF_ROLL_CALL,
    Class,
    ClassSession,
    ClassStaff,
    ClassStatus,
    CourseRangeMode,
    RangeMode,
    RollCallEntry,
    RollCallStatus,
    StaffRole,
)


def class_for_booking(db: Session, user: CurrentUser, class_id: str) -> Class:
    """The class a new session is booked for: the caller's tenant's, visible to them (404
    otherwise), led by them or they are an admin (403), and open (409)."""
    try:
        cid = uuid.UUID(str(class_id))
    except ValueError:
        raise HTTPException(404, "Class not found") from None
    cls, role = access.get_visible(db, cid, user)
    if role not in access.EDITORS:
        raise HTTPException(403, "Only the class's lead instructor or an admin books its sessions")
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, f"The class is {cls.status}; it takes no new sessions")
    return cls


def link(
    db: Session, cls: Class, evt: ScheduledEvent, actor: CurrentUser, mode: RangeMode = DEFAULT_RANGE_MODE
) -> ClassSession:
    """Make ``evt`` a session of ``cls``, in ``mode``. Both are the same tenant's. Does not commit."""
    if evt.tenant_id != cls.tenant_id:  # never: the caller checked both against its tenant
        raise HTTPException(404, "Class not found")
    row = ClassSession(
        class_id=cls.id, event_id=evt.id, tenant_id=cls.tenant_id, created_by=_uid(actor), range_mode=mode.value
    )
    db.add(row)
    return row


def class_ids_for(db: Session, event_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """booking id -> class id, for those of ``event_ids`` that are class sessions."""
    return {eid: cid for eid, (cid, _) in links_for(db, event_ids).items()}


def links_for(db: Session, event_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
    """booking id -> (class id, range mode), for those of ``event_ids`` that are class sessions."""
    if not event_ids:
        return {}
    # tenant-safe: keyed by bookings the caller already loaded tenant-scoped.
    rows = (
        db.query(ClassSession.event_id, ClassSession.class_id, ClassSession.range_mode)
        .filter(ClassSession.event_id.in_(event_ids))
        .all()
    )
    return {eid: (cid, mode) for eid, cid, mode in rows}


# ── Individual or collective (ADR 0014 §9) ───────────────────────────────────


def course_mode(db: Session, tenant_id: uuid.UUID, course_id: uuid.UUID) -> tuple[RangeMode, str]:
    """(mode, source): the tenant's default for this course's sessions (``setting``), else
    collective (``default``)."""
    # tenant-safe: keyed by the tenant id itself.
    row = db.get(CourseRangeMode, (tenant_id, course_id))
    return (RangeMode(row.mode), "setting") if row is not None else (DEFAULT_RANGE_MODE, "default")


def resolve_mode(db: Session, cls: Class, requested: str | None) -> RangeMode:
    """A new session's mode: the one asked for, else its course's default."""
    if requested:
        return RangeMode(requested)
    return course_mode(db, cls.tenant_id, cls.course_id)[0]


def mode_of(db: Session, evt: ScheduledEvent) -> RangeMode | None:
    """A class session's mode; None when ``evt`` is no class's session."""
    link_row = links_for(db, [evt.id]).get(evt.id)
    return RangeMode(link_row[1]) if link_row else None


def check_mode(
    mode: RangeMode, template_id: str | uuid.UUID | None, range_id: uuid.UUID | None, exercise_id: uuid.UUID | None,
    scenario_id: uuid.UUID | None = None,
) -> None:
    """An individual session builds each Student's range from its template: it needs one, and
    names no range, exercise or scenario of its own (422): a scenario runs as one exercise on
    one range, and per-Student exercises are not built."""
    if mode != RangeMode.individual:
        return
    if not template_id:
        raise HTTPException(422, "An individual session needs a range template: each Student's range is built from it")
    if range_id or exercise_id or scenario_id:
        raise HTTPException(
            422, "An individual session builds a range per Student; it cannot name one range, exercise or scenario "
            "(book a collective session for an exercise)"
        )


def set_mode(db: Session, evt: ScheduledEvent, mode: RangeMode) -> None:
    """Change a session's mode (the scheduler re-checks its size). Does not commit."""
    # tenant-safe: the link of a booking the caller already loaded tenant-scoped.
    db.query(ClassSession).filter(ClassSession.event_id == evt.id).update(
        {ClassSession.range_mode: mode.value}, synchronize_session=False
    )


def active_seats(db: Session, cls: Class) -> int:
    """Students on the roll call now: removed entries and additions awaiting an approver
    hold no seat."""
    return (
        db.query(RollCallEntry.id)
        .filter(
            RollCallEntry.class_id == cls.id,
            RollCallEntry.tenant_id == cls.tenant_id,
            RollCallEntry.status.notin_(OFF_ROLL_CALL),
        )
        .count()
    )


def per_seat(db: Session, cls: Class, template_id: uuid.UUID | str | None) -> tuple[Resources, int]:
    """One Student's range: the template's VMs (resources, VM count). Nothing for no template,
    one the class's tenant cannot use, or one that declares no VMs."""
    if not template_id:
        return Resources(), 0
    tid = template_id if isinstance(template_id, uuid.UUID) else uuid.UUID(str(template_id))
    tmpl = (
        db.query(Template.yaml)
        .filter(
            Template.id == tid,
            Template.deleted_at.is_(None),
            (Template.tenant_id == cls.tenant_id) | (Template.is_public == True),  # noqa: E712
        )
        .first()
    )
    d = template_demand(tmpl[0]) if tmpl else None
    if d is None:
        return Resources(), 0
    return Resources(d.vcpu, d.ram_mb, d.disk_gb), d.vm_count


def session_demand(
    db: Session, cls: Class, mode: RangeMode, template_id: uuid.UUID | str | None, demand: scheduler.Demand
) -> scheduler.Demand:
    """A class session's size: ``demand`` (its template's or its typed size) when collective;
    when individual, one seat's size (``template_id``) times the seats on the roll call now."""
    if mode != RangeMode.individual:
        return demand
    seat, vms = per_seat(db, cls, template_id)
    n = active_seats(db, cls)
    return scheduler.Demand(_scaled(seat, n), vms * n, "seats")


def size_for_approval(db: Session, evt: ScheduledEvent) -> None:
    """Approving an individual session holds one seat per Student on the roll call at that
    moment (Students added while it waited cost nothing until now). Does not commit."""
    cls = class_of(db, evt)
    if cls is None or mode_of(db, evt) != RangeMode.individual:
        return
    seat, vms = per_seat(db, cls, evt.template_id)
    n = active_seats(db, cls)
    scheduler.set_size(db, evt, _scaled(seat, n), vms * n)


def class_of(db: Session, evt: ScheduledEvent) -> Class | None:
    """The class ``evt`` is a session of, in the booking's own tenant; None when it is none."""
    return (
        db.query(Class)
        .join(ClassSession, ClassSession.class_id == Class.id)
        .filter(ClassSession.event_id == evt.id, Class.tenant_id == evt.tenant_id)
        .first()
    )


def all_session_ids() -> Select:
    """Every booking that is some class's session (a subquery)."""
    return select(ClassSession.event_id)


def session_ids_on_roll_call(user_id: uuid.UUID) -> Select:
    """The class sessions whose roll call ``user_id`` is on (a subquery): a Student's own
    sessions, where the roll call replaces course enrolment (ADR 0014)."""
    return (
        select(ClassSession.event_id)
        .join(RollCallEntry, RollCallEntry.class_id == ClassSession.class_id)
        .where(
            RollCallEntry.user_id == user_id,
            RollCallEntry.tenant_id == ClassSession.tenant_id,
            RollCallEntry.status.notin_(OFF_ROLL_CALL),
        )
    )


def class_running_course(
    db: Session, tenant_id: uuid.UUID, course_id: uuid.UUID, start: datetime, end: datetime
) -> Class | None:
    """An open class of the tenant on ``course_id`` whose dates meet [start, end): a booking
    of that course then is, in substance, that class's session (ADR 0014 §14)."""
    return (
        db.query(Class)
        .filter(
            Class.tenant_id == tenant_id,
            Class.course_id == course_id,
            Class.status == ClassStatus.open.value,
            Class.start_date <= end.date(),
            Class.end_date >= start.date(),
        )
        .order_by(Class.start_date)
        .first()
    )


def class_names(db: Session, class_ids: set[uuid.UUID], tenant_id: uuid.UUID) -> dict[uuid.UUID, str]:
    if not class_ids:
        return {}
    rows = db.query(Class.id, Class.name).filter(Class.id.in_(class_ids), Class.tenant_id == tenant_id).all()
    return dict(rows)


def roll_call_for_booking(db: Session, evt: ScheduledEvent) -> list[str] | None:
    """Who a class session's invites and reminders go to: its class's roll call
    (``service.class_roll_call_emails``: tenant-pinned, removed and pending entries left
    out). None when ``evt`` is no class's session (its course's Students attend instead).
    A session whose class is not in the booking's tenant has nobody: never another tenant's."""
    from .service import class_roll_call_emails

    # tenant-safe: the link is looked up by booking; the class must be the booking's tenant's.
    link_row = db.query(ClassSession).filter(ClassSession.event_id == evt.id).first()
    if link_row is None:
        return None
    cls = class_of(db, evt)
    return class_roll_call_emails(db, cls) if cls is not None else []


def edits_class_of(db: Session, evt: ScheduledEvent, user: CurrentUser) -> bool:
    """Whether the caller is the lead or an admin of the class ``evt`` is a session of."""
    cls = class_of(db, evt)
    return cls is not None and access.role_in(db, cls, user) in access.EDITORS


def teachers(db: Session, cls: Class) -> set[uuid.UUID]:
    """Who may teach a class's sessions: its lead and its assistant instructors."""
    assistants = {
        uid
        for (uid,) in db.query(ClassStaff.user_id).filter(
            ClassStaff.class_id == cls.id, ClassStaff.role == StaffRole.assistant.value
        )
    }
    return {cls.lead_instructor_id, *assistants}


def assert_class_teacher(db: Session, cls: Class, instructor_id: uuid.UUID | None) -> None:
    """A class session is taught by the class's lead or one of its assistants (422)."""
    if instructor_id is None or instructor_id not in teachers(db, cls):
        raise HTTPException(422, "A class session is taught by the class's lead or one of its assistant instructors")


def realign_teachers(db: Session, cls: Class, actor: CurrentUser) -> list:
    """After any change to a class: each of its unfinished sessions taught by someone who is
    no longer one of its teachers (a lead handed over, an assistant taken off, in one step
    or two) passes to the lead. Whoever left then holds no booking of the class, so no
    authority over its course or teams (app/class_setup/scope.py), no invites and no
    ``/schedule/mine`` entry. Returns the invites the change calls for. Does not commit."""
    return scheduler.reassign_instructor(db, actor, session_ids(db, cls), teachers(db, cls), cls.lead_instructor_id)


def assert_same_class(db: Session, evt: ScheduledEvent, class_id: str | None) -> None:
    """A session's class is fixed when it is booked: naming another (or one for a booking
    that is no class's session) is 422. Naming none keeps it."""
    if not class_id:
        return
    current = class_ids_for(db, [evt.id]).get(evt.id)
    if current is None or str(current) != str(class_id):
        raise HTTPException(422, "A booking's class is set when the session is booked and cannot change")


# ── Roll-call changes after approval (ADR 0014 §17) ─────────────────────────


def session_ids(db: Session, cls: Class) -> list[uuid.UUID]:
    return [eid for (eid,) in db.query(ClassSession.event_id).filter(ClassSession.class_id == cls.id).all()]


def approved_sessions(db: Session, cls: Class) -> list[ScheduledEvent]:
    """The class's approved sessions that still hold capacity and have not ended."""
    return scheduler.approved_upcoming(db, session_ids(db, cls))


def seat_demand(db: Session, cls: Class, evt: ScheduledEvent) -> Resources:
    """What one more Student adds to session ``evt``: their own range (the session's
    template) when it is individual; nothing when it is collective (one shared range)."""
    if mode_of(db, evt) != RangeMode.individual:
        return Resources()
    return per_seat(db, cls, evt.template_id)[0]


def _scaled(r: Resources, n: int) -> Resources:
    return Resources(r.vcpu * n, r.ram_mb * n, r.disk_gb * n)


def room_for_seats(db: Session, cls: Class, seats: int) -> list[str]:
    """Why ``seats`` more Students would not fit the class's approved sessions (none: they
    fit). When a seat costs anything, takes the scheduler's booking lock and reads the
    sessions again under it, so two additions (or an addition and an approval) cannot both
    take the last room. The caller commits or rolls back, which releases it."""
    ids = session_ids(db, cls)
    if not ids:
        return []

    def needs():
        out = [(evt, _scaled(seat_demand(db, cls, evt), seats)) for evt in scheduler.approved_upcoming(db, ids)]
        return [(evt, need) for evt, need in out if need != Resources()]

    if not needs() and not _has_individual(db, cls):
        return []  # a seat costs nothing: no reason to queue behind every booking in the platform
    # A seat costs something now, or will once an individual session is approved: take the
    # lock, so the roll call and the sizes taken from it change in one order (#299 review).
    scheduler.serialize_bookings(db)
    reasons: list[str] = []
    for evt, need in needs():  # read again under the lock
        reasons += scheduler.room_for(db, evt, need)
    return reasons


def take_seats(db: Session, cls: Class, seats: int) -> list[ScheduledEvent]:
    """The approved sessions after ``seats`` Students joined the roll call (call it once they
    are on it, after ``room_for_seats`` said they fit). Returns the sessions, whose invites the
    new Students now get. Does not commit."""
    return _reseat(db, cls, seats, 1)


def free_seats(db: Session, cls: Class, seats: int) -> list[ScheduledEvent]:
    """The approved sessions after ``seats`` Students left the roll call. Always allowed.
    Returns the sessions, whose invites are withdrawn from those Students. Does not commit."""
    return _reseat(db, cls, seats, -1)


def _reseat(db: Session, cls: Class, seats: int, sign: int) -> list[ScheduledEvent]:
    """An individual session's size is recomputed, never stepped: one seat's size times the
    Students on the roll call now, under the booking lock, so no addition, removal or
    forfeit can leave it drifting from the roll call. Any other session moves by its seat
    cost (nothing, for a collective one). Its SEQUENCE goes up: a Student who left and came
    back may hold an older CANCEL of it.

    **Lock order.** Two orders meet here:

    - remove and forfeit write the roll-call row first (a guarded status update), then
      take the booking lock (``serialize_bookings``, here);
    - add, reinstate and approve-an-addition take the booking lock first
      (``room_for_seats``), then write the row.

    These cannot deadlock on the same entry, because the status guards keep their rows apart:
    - remove and forfeit only update an entry that is *on* the roll call: invited, signed in,
      or ready (forfeit: invited only);
    - add writes a *new* or *removed* entry;
    - reinstate writes a *forfeited* one, and approve writes a *pending* one, each claimed by
      that status.

    A transaction that holds the row lock of an entry in one state never waits on the
    booking lock held by a transaction that needs that same row in the other state: the
    second finds the status changed and refuses (409) or skips. Every path also takes the
    booking lock at most once, and nothing waits on it while it holds another roll-call
    row it locked."""
    db.flush()  # the roll-call change itself, before it is counted
    sessions = approved_sessions(db, cls)
    if any(mode_of(db, e) == RangeMode.individual for e in sessions):
        scheduler.serialize_bookings(db)
    n = None
    for evt in sessions:
        if mode_of(db, evt) == RangeMode.individual:
            n = active_seats(db, cls) if n is None else n
            seat, vms = per_seat(db, cls, evt.template_id)
            scheduler.set_size(db, evt, _scaled(seat, n), vms * n)
        else:
            scheduler.resize(db, evt, _scaled(seat_demand(db, cls, evt), seats), sign)
        scheduler.resequence(evt)
    db.flush()
    return sessions


def _has_individual(db: Session, cls: Class) -> bool:
    """Whether the class has an individual session awaiting approval or approved."""
    from ..scheduler.lifecycle import HOLDING
    from ..scheduler.models import EventState

    return (
        db.query(ClassSession.id)
        .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
        .filter(
            ClassSession.class_id == cls.id,
            ClassSession.range_mode == RangeMode.individual.value,
            ScheduledEvent.state.in_((*HOLDING, EventState.pending_approval)),
        )
        .first()
        is not None
    )


def on_exercise(db: Session, evt: ScheduledEvent) -> None:
    """The clock created ``evt``'s exercise: a collective session's teams join it."""
    from . import teams

    teams.on_exercise(db, evt)


# ── For the scheduler's capacity view and the rosters ────────────────────────


def roll_call_user_ids(db: Session, event_ids: list[uuid.UUID], as_of: datetime | None = None) -> set[uuid.UUID]:
    """Who attends these bookings as class sessions: the accounts on their classes' roll
    calls (ADR 0014: the roll call replaces course enrolment for exercise and telemetry
    rosters). Now, or ``as_of`` a moment (an after-action report): on the roll call by then
    and not taken off before it. Additions awaiting an approver never count."""
    if not event_ids:
        return set()
    # tenant-safe: entries of the classes these bookings belong to, in the class's own tenant.
    q = (
        db.query(RollCallEntry.user_id, RollCallEntry.status, RollCallEntry.created_at, RollCallEntry.removed_at)
        .join(ClassSession, ClassSession.class_id == RollCallEntry.class_id)
        .filter(
            ClassSession.event_id.in_(event_ids),
            RollCallEntry.tenant_id == ClassSession.tenant_id,
            RollCallEntry.user_id.isnot(None),
            # Off the roll call (pending, forfeited) never counts; a removed entry counts
            # ``as_of`` a moment before it was removed (below).
            RollCallEntry.status.notin_([s for s in OFF_ROLL_CALL if s != RollCallStatus.removed.value]),
        )
    )
    out: set[uuid.UUID] = set()
    for uid, status, created, removed in q.all():
        if as_of is None:
            if status != RollCallStatus.removed.value:
                out.add(uid)
            continue
        if created is not None and _aware(created) > _aware(as_of):
            continue
        if status == RollCallStatus.removed.value and (removed is None or _aware(removed) <= _aware(as_of)):
            continue
        out.add(uid)
    return out


def _aware(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _uid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None
