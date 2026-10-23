"""A class's sessions: its scheduler bookings (ADR 0014 §8, §14-17; issue #281).

- Only the class's lead (or an admin of its tenant) books a session for it, and only while
  the class is open (``class_for_booking``). The booking then waits for an approver.
- Once approved, a session's invites and reminders go to the class's roll call
  (``roll_call_for_booking``), not to its course's enrolment.
- Roll-call changes after approval (§17): taking a Student off is always allowed; adding one
  is automatic while every approved session still has room for one more seat
  (``room_for_seats``); an addition that does not fit waits for an approver.

**The seat seam (#282).** What one more Student adds to a session is ``seat_demand``. Today
every session is one range for the whole class, sized by its template, so a seat adds
nothing and additions always fit. #282 builds per-Student ranges for *individual* sessions:
it makes ``seat_demand`` return the per-Student range's size for those, and sets a session's
size from its roll call when it is approved. Nothing else here changes.

The scheduler is reached only through ``scheduler.service`` (ADR 0004 §1).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..scheduler import service as scheduler
from ..scheduler.capacity import Resources
from ..scheduler.models import ScheduledEvent
from . import access
from .models import Class, ClassSession, ClassStatus


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
        raise HTTPException(409, "The class is cancelled; it takes no new sessions")
    return cls


def link(db: Session, cls: Class, evt: ScheduledEvent, actor: CurrentUser) -> ClassSession:
    """Make ``evt`` a session of ``cls``. Both are the same tenant's. Does not commit."""
    if evt.tenant_id != cls.tenant_id:  # never: the caller checked both against its tenant
        raise HTTPException(404, "Class not found")
    row = ClassSession(class_id=cls.id, event_id=evt.id, tenant_id=cls.tenant_id, created_by=_uid(actor))
    db.add(row)
    return row


def class_ids_for(db: Session, event_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """booking id -> class id, for those of ``event_ids`` that are class sessions."""
    if not event_ids:
        return {}
    # tenant-safe: keyed by bookings the caller already loaded tenant-scoped.
    rows = db.query(ClassSession.event_id, ClassSession.class_id).filter(ClassSession.event_id.in_(event_ids)).all()
    return dict(rows)


def class_of(db: Session, evt: ScheduledEvent) -> Class | None:
    """The class ``evt`` is a session of, in the booking's own tenant; None when it is none."""
    return (
        db.query(Class)
        .join(ClassSession, ClassSession.class_id == Class.id)
        .filter(ClassSession.event_id == evt.id, Class.tenant_id == evt.tenant_id)
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
    """What one more Student adds to session ``evt``. Nothing yet: a session is one range
    for the whole class. #282 returns the per-Student range's size for individual sessions."""
    return Resources()


def _scaled(r: Resources, n: int) -> Resources:
    return Resources(r.vcpu * n, r.ram_mb * n, r.disk_gb * n)


def room_for_seats(db: Session, cls: Class, seats: int) -> list[str]:
    """Why ``seats`` more Students would not fit the class's approved sessions (none: they
    fit). Takes the scheduler's booking lock first when any seat costs something, so two
    additions cannot both take the last room. The caller commits or rolls back."""
    sessions = approved_sessions(db, cls)
    needs = [(evt, _scaled(seat_demand(db, cls, evt), seats)) for evt in sessions]
    needs = [(evt, need) for evt, need in needs if need != Resources()]
    if not needs:
        return []
    scheduler.serialize_bookings(db)
    reasons: list[str] = []
    for evt, need in needs:
        reasons += scheduler.room_for(db, evt, need)
    return reasons


def take_seats(db: Session, cls: Class, seats: int) -> list[ScheduledEvent]:
    """Grow each approved session by ``seats`` seats (after ``room_for_seats`` said they fit).
    Returns the sessions, whose invites the new Students now get. Does not commit."""
    sessions = approved_sessions(db, cls)
    for evt in sessions:
        scheduler.resize(db, evt, _scaled(seat_demand(db, cls, evt), seats), 1)
    db.flush()
    return sessions


def free_seats(db: Session, cls: Class, seats: int) -> list[ScheduledEvent]:
    """Shrink each approved session by ``seats`` seats (Students taken off). Always allowed.
    Returns the sessions, whose invites are withdrawn from those Students. Does not commit."""
    sessions = approved_sessions(db, cls)
    for evt in sessions:
        scheduler.resize(db, evt, _scaled(seat_demand(db, cls, evt), seats), -1)
    return sessions


def _uid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None
