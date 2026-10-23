"""A class's ranges: built at the lead, kept all week, torn down at the end (ADR 0014 §9-10; #282).

What a class owns (``ClassRange``):

- for an **individual** session, one range per Student on its roll call (a *seat*), from the
  session's range template;
- for a **collective** session, one *shared* range per template, which the session books
  (``scheduler.service.attach_range``), so its exercise, Live Class and telemetry find it as
  they find any booking's range.

**How they are built.** The scheduler's clock calls ``tick`` first, every minute. Each
approved class session whose provisioning lead has been reached gets its class's ranges:
any that do not exist yet are created (``range_lifecycle.create_for_booking``) and built
(``range_lifecycle.provision_for_booking``). That is the scheduler's own path: a recorded
range operation, the worker's ``provision_range`` task, the S1 VLAN allocation, S2 template
pinning and S3 credentials. Nothing here talks to a hypervisor (ADR 0001). Lab sessions
(``lab_sessions``) render a release's lab profile instead and lease pooled port groups; a
class seat is built from an ordinary range template, so it takes the scheduler's path.

- **Once per class.** A range is keyed by (class, template, seat), so the next session that
  needs it finds it standing and builds nothing.
- **Fan-out.** Every seat is its own range, operation and commit: a 15-Student class is 15
  ``provision_range`` tasks queued at once, which the worker's own concurrency runs as it
  can; up to ``CLASS_RANGE_BUILDS_PER_TICK`` new ranges per tick, the rest the next minute.
  The approval already held their capacity (``sessions``).
- **One seat failing** (refused, an exception, or a build the worker fails) never stops the
  others: it is recorded on its row and reported on the class (``status``). A build that
  could not be started is tried again each tick; one the worker failed waits for the lead
  to retry it (``retry``).

**How they are torn down**, always by ``range_lifecycle.destroy_for_booking`` (a range
operation; the worker destroys only what S5 recorded at clone time):

=================================  ==================================  =====================
When                               Which                               Maintenance window
=================================  ==================================  =====================
the class's last day is over       all of them (``class_ended``)       never blocks it: lease
                                                                       expiry (``booking_ended``)
its Student taken off the roll     that seat (``student_removed``)     waits (a normal
call                                                                   destroy)
no session uses it any more        that kind and template's ranges     waits
(cancelled, rejected, mode         (``no_session_needs_it``)
switched; drafts still count)
the class is cancelled or closed   all of them (``class_<status>``)    waits, like a
                                                                       cancelled booking's
=================================  ==================================  =====================

Kept until the class's end, not its last booked session, so booking day by day does not wipe
the Students' work (ADR 0014 §10). Capacity holds them over that span (``holds.py``).

A range still being built cannot be destroyed yet; the next tick tries again. Every step is
audited against the class.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import range_lifecycle
from ..models import Range, RangeState, Template
from ..scheduler import service as scheduler
from ..scheduler.capacity import TEARDOWN_GRACE
from ..scheduler.models import EventState, ScheduledEvent
from . import service
from .models import (
    OFF_ROLL_CALL,
    SHARED_SEAT,
    Class,
    ClassRange,
    ClassRangeKind,
    ClassSession,
    ClassStatus,
    RangeMode,
    RollCallEntry,
)

logger = logging.getLogger("truenorth.api.classes.ranges")

BUILDS_PER_TICK = int(os.getenv("CLASS_RANGE_BUILDS_PER_TICK", "100"))

# Why a build could not be started (``last_error``): an exception is a failed seat; a
# refusal (a maintenance window, an operation in flight) is waiting, tried again each tick.
FAILED_PREFIX = "Build could not be started: "
WAITING_PREFIX = "Waiting: "

# Teardown reasons, as recorded on the row and in the audit log.
CLASS_ENDED = "class_ended"  # the class's last day is over: lease expiry
STUDENT_REMOVED = "student_removed"
NO_SESSION = "no_session_needs_it"  # its sessions were cancelled, rejected or switched mode


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ── Reads ────────────────────────────────────────────────────────────────────


def is_class_range(db: Session, range_id: uuid.UUID | None) -> bool:
    """Whether a class owns this range now (built for it and not torn down)."""
    if range_id is None:
        return False
    # tenant-safe: keyed by a range the caller already resolved in its tenant.
    return (
        db.query(ClassRange.id)
        .filter(ClassRange.range_id == range_id, ClassRange.torn_down_at.is_(None))
        .first()
        is not None
    )


def _of_class(db: Session, cls: Class, range_id: uuid.UUID) -> bool:
    """Whether ``range_id`` is one of THIS class's ranges (another class's is never ours)."""
    return (
        db.query(ClassRange.id)
        .filter(ClassRange.class_id == cls.id, ClassRange.tenant_id == cls.tenant_id, ClassRange.range_id == range_id)
        .first()
        is not None
    )


def observes_only(db: Session, user, rng: Range) -> bool:
    """Whether the caller's part in a class that owns ``rng`` is observer (not its lead, an
    assistant or an admin): ADR 0014 §19, observers never see range login details."""
    from . import access
    from .schemas import ClassRole

    class_ids = {
        cid for (cid,) in db.query(ClassRange.class_id).filter(
            ClassRange.range_id == rng.id, ClassRange.tenant_id == rng.tenant_id)
    }
    for cid in class_ids:
        cls = db.get(Class, cid)  # tenant-safe: a class of the range's own tenant (filtered above)
        if cls is not None and access.role_in(db, cls, user) == ClassRole.observer:
            return True
    return False


def rows_of(db: Session, cls: Class) -> list[ClassRange]:
    return (
        db.query(ClassRange)
        .filter(ClassRange.class_id == cls.id, ClassRange.tenant_id == cls.tenant_id)
        .order_by(ClassRange.kind, ClassRange.created_at)
        .all()
    )


def status(row: ClassRange, rng: Range | None) -> str:
    """pending (not built yet) | building | ready | failed | tearing_down | torn_down."""
    if row.torn_down_at is not None:
        return "torn_down"
    if rng is None or rng.state == RangeState.created:
        return "failed" if (row.last_error or "").startswith(FAILED_PREFIX) else "pending"
    if rng.state == RangeState.provisioning:
        return "building"
    if rng.state == RangeState.failed:
        return "failed"
    if rng.state == RangeState.destroying:
        return "tearing_down"
    if rng.state == RangeState.destroyed:
        return "torn_down"
    return "ready"


def error_of(row: ClassRange, rng: Range | None) -> str | None:
    if rng is not None and rng.state == RangeState.failed:
        return rng.error_message or "The build failed"
    return row.last_error


def student_participation(db: Session, user_id: uuid.UUID, rng: Range, now: datetime | None = None) -> dict | None:
    """How a Student takes part in a class's range, for its credentials and telemetry (ADR 0013
    §3: Students see only their machines): ``{"class_seat_id"}`` for their own seat while they
    are on the roll call; ``{"class_id"}`` for their class's shared range while a collective
    session on it is under way and they are on the roll call. None otherwise."""
    # tenant-safe: rows of the range itself, in the range's tenant.
    rows = (
        db.query(ClassRange)
        .filter(ClassRange.range_id == rng.id, ClassRange.tenant_id == rng.tenant_id, ClassRange.torn_down_at.is_(None))
        .all()
    )
    for row in rows:
        cls = db.get(Class, row.class_id)  # tenant-safe: the row's own class, checked next
        if cls is None or cls.tenant_id != rng.tenant_id or cls.status != ClassStatus.open.value:
            continue  # a cancelled or closed class hands out no credentials
        if row.kind == ClassRangeKind.seat.value:
            # tenant-safe: the seat's own entry, checked against the range's tenant below.
            entry = db.get(RollCallEntry, row.entry_id) if row.entry_id else None
            if (entry is not None and entry.user_id == user_id and entry.tenant_id == rng.tenant_id
                    and entry.status not in OFF_ROLL_CALL):
                return {"class_seat_id": str(row.id)}
            continue
        on = (
            db.query(RollCallEntry.id)
            .filter(
                RollCallEntry.class_id == row.class_id,
                RollCallEntry.tenant_id == rng.tenant_id,
                RollCallEntry.user_id == user_id,
                RollCallEntry.status.notin_(OFF_ROLL_CALL),
            )
            .first()
        )
        if on is None:
            continue
        now = now or _now()
        live = (
            db.query(ScheduledEvent.id)
            .join(ClassSession, ClassSession.event_id == ScheduledEvent.id)
            .filter(
                ClassSession.class_id == row.class_id,
                ScheduledEvent.range_id == rng.id,
                ScheduledEvent.state.in_((EventState.provisioning, EventState.active)),
                ScheduledEvent.start_time <= now,
                ScheduledEvent.end_time > now,
            )
            .first()
        )
        if live is not None:
            return {"class_id": str(row.class_id)}
    return None


# ── The clock ────────────────────────────────────────────────────────────────


def tick(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Build what is due, then tear down what is no longer needed. Commits per range, so one
    failing never undoes another. Called by the scheduler's clock before its own steps."""
    now = now or _now()
    out = {"built": 0, "torn_down": 0, "failed": 0}
    budget = [BUILDS_PER_TICK]
    for evt, link in _due(db, now):
        try:
            cls = db.get(Class, link.class_id)  # tenant-safe: the link's own class
            if cls is None or cls.tenant_id != evt.tenant_id or cls.status != ClassStatus.open.value:
                continue
            if link.range_mode == RangeMode.individual.value:
                template_id = evt.template_id
                for entry_id in [e.id for e in _seats(db, cls)]:
                    try:  # each seat on its own: one that breaks holds up none of the others
                        entry = db.get(RollCallEntry, entry_id)  # tenant-safe: one of the class's own seats
                        _ensure(db, cls, template_id, entry, now, out, budget)
                    except Exception:  # noqa: BLE001
                        db.rollback()
                        out["failed"] += 1
                        logger.exception("class range: seat %s could not be handled; retrying next tick", entry_id)
            elif evt.range_id is None or _of_class(db, cls, evt.range_id):
                row = _ensure(db, cls, evt.template_id, None, now, out, budget)
                if row is not None and row.range_id is not None and evt.range_id is None:
                    if scheduler.attach_range(db, evt.id, row.range_id):
                        service.audit(db, None, cls, "class_range_attached",
                                      f"session {evt.id} uses the class's shared range {row.range_id}")
                    db.commit()
        except Exception:  # noqa: BLE001 — one session's trouble never stops the clock
            db.rollback()
            logger.exception("class ranges: session %s could not be handled; retrying next tick", evt.id)
    needed: dict = {}  # class -> what its sessions still use, read once per class per tick
    for row in list(_live(db)):
        try:
            if _teardown_if_done(db, row, now, needed):
                out["torn_down"] += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("class range %s: teardown could not be handled; retrying next tick", row.id)
    return out


def _due(db: Session, now: datetime) -> list[tuple[ScheduledEvent, ClassSession]]:
    """Approved class sessions with a template whose provisioning lead has been reached and
    which have not ended, soonest first."""
    from .sessions import all_session_ids

    events = [e for e in scheduler.holding_sessions(db, all_session_ids(), now=now, lead=True) if e.template_id]
    if not events:
        return []
    # tenant-safe: the links of those bookings, in their own tenant.
    links = {s.event_id: s for s in db.query(ClassSession).filter(ClassSession.event_id.in_([e.id for e in events]))}
    return [(e, links[e.id]) for e in events if e.id in links and links[e.id].tenant_id == e.tenant_id]


def _seats(db: Session, cls: Class) -> list[RollCallEntry]:
    return (
        db.query(RollCallEntry)
        .filter(
            RollCallEntry.class_id == cls.id,
            RollCallEntry.tenant_id == cls.tenant_id,
            RollCallEntry.status.notin_(OFF_ROLL_CALL),
        )
        .order_by(RollCallEntry.created_at, RollCallEntry.id)
        .all()
    )


def _row(db: Session, cls: Class, template_id: uuid.UUID, entry: RollCallEntry | None) -> ClassRange:
    key = str(entry.id) if entry is not None else SHARED_SEAT
    q = db.query(ClassRange).filter(
        ClassRange.class_id == cls.id, ClassRange.template_id == template_id, ClassRange.seat_key == key
    )
    row = q.first()
    if row is not None:
        return row
    row = ClassRange(
        tenant_id=cls.tenant_id, class_id=cls.id, template_id=template_id,
        kind=(ClassRangeKind.seat if entry is not None else ClassRangeKind.shared).value,
        entry_id=entry.id if entry is not None else None, seat_key=key,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # another replica made it first
        row = q.one()
    return row


def _ensure(
    db: Session, cls: Class, template_id: uuid.UUID, entry: RollCallEntry | None, now: datetime,
    out: dict[str, int], budget: list[int],
) -> ClassRange | None:
    """The class's range for this seat (or its shared one) standing, or being built."""
    row = _row(db, cls, template_id, entry)
    rng = db.get(Range, row.range_id) if row.range_id else None  # tenant-safe: the row's own range
    standing = rng is not None and row.torn_down_at is None and rng.state not in (
        RangeState.destroying, RangeState.destroyed)
    if standing:
        if rng.state == RangeState.created:  # created, its build not started yet: start it
            _provision(db, cls, row, out)
        db.commit()
        return row
    if budget[0] <= 0:
        db.commit()
        return row
    budget[0] -= 1
    tmpl_name = (db.query(Template.name).filter(Template.id == template_id).scalar() or "range")[:60]
    label = f"seat {str(entry.id)[:8]}" if entry is not None else f"shared ({tmpl_name})"
    new = range_lifecycle.create_for_booking(
        db, tenant_id=cls.tenant_id, template_id=template_id, name=f"{cls.name} {label}"
    )
    old = row.range_id
    # tenant-safe: the class's own row, found by (class, template, seat) above.
    won = (
        db.query(ClassRange)
        .filter(ClassRange.id == row.id, ClassRange.range_id.is_(None) if old is None else ClassRange.range_id == old)
        .update(
            {
                ClassRange.range_id: new.id, ClassRange.builds: (row.builds or 0) + 1, ClassRange.built_at: now,
                ClassRange.torn_down_at: None, ClassRange.teardown_reason: None, ClassRange.last_error: None,
            },
            synchronize_session=False,
        )
    )
    if won != 1:
        db.rollback()  # another replica built it; its range is the one
        return None
    service.audit(db, None, cls, "class_range_created", f"{row.kind} {row.seat_key}: range {new.id}")
    db.commit()
    db.refresh(row)
    _provision(db, cls, row, out)
    return row


def _provision(db: Session, cls: Class, row: ClassRange, out: dict[str, int]) -> None:
    """Start the build of ``row``'s range. A refusal waits for the next tick; an exception is a
    failed seat, recorded and reported, never raised: the other seats go on."""
    range_id, row_id = row.range_id, row.id
    try:
        built, what = range_lifecycle.provision_for_booking(db, range_id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("class range %s: its build could not be started", row_id)
        _note(db, row_id, f"{FAILED_PREFIX}{exc}")
        out["failed"] += 1
        return
    rng = db.get(Range, range_id)  # tenant-safe: the row's own range
    if built or (rng is not None and rng.state != RangeState.created):
        _note(db, row_id, None)
        out["built"] += 1 if built else 0
    else:
        _note(db, row_id, f"{WAITING_PREFIX}{what}")


def _note(db: Session, row_id: uuid.UUID, error: str | None) -> None:
    """Record (or clear) why the row's last build or teardown could not go ahead. Commits."""
    message = error[:1000] if error else None
    # tenant-safe: a row this tick (or the lead's retry) already loaded from its class.
    db.query(ClassRange).filter(ClassRange.id == row_id).update({ClassRange.last_error: message},
                                                                synchronize_session=False)
    db.commit()


PAGE = 500  # rows per page of the teardown pass


def _live(db: Session):
    """Every class range that stands, a page at a time (keyset on id), so a large site is
    never cut off at one page."""
    last = None
    while True:
        q = db.query(ClassRange).filter(ClassRange.range_id.isnot(None), ClassRange.torn_down_at.is_(None))
        if last is not None:
            q = q.filter(ClassRange.id > last)
        page = q.order_by(ClassRange.id).limit(PAGE).all()
        yield from page
        if len(page) < PAGE:
            return
        last = page[-1].id


def _why_done(db: Session, cls: Class | None, row: ClassRange, now: datetime,
              needed: set[tuple[str, uuid.UUID]]) -> tuple[str, str | None] | None:
    """(reason, maintenance-window bypass) when ``row`` is no longer needed; None while it is.

    Only the class's end is lease expiry (ADR 0013 §7): its teardown runs inside a
    maintenance window too (``booking_ended``). Everything else is a normal destroy that a
    window defers, as a cancelled booking's is (``scheduler.service.release_range``): the
    class cancelled or closed, a Student taken off the roll call, no session left that uses
    the range (its sessions cancelled, rejected or switched to the other mode)."""
    from ..maintenance_windows.policy import BYPASS_BOOKING_ENDED

    if cls is None or cls.status != ClassStatus.open.value:
        return f"class_{cls.status if cls is not None else 'gone'}", None
    if row.kind == ClassRangeKind.seat.value:
        entry = db.get(RollCallEntry, row.entry_id) if row.entry_id else None  # tenant-safe: the row's own entry
        if entry is None or entry.status in OFF_ROLL_CALL:
            return STUDENT_REMOVED, None
    if (row.kind, row.template_id) not in needed:
        return NO_SESSION, None
    if now >= _ends(db, cls) + TEARDOWN_GRACE:
        return CLASS_ENDED, BYPASS_BOOKING_ENDED
    return None


def _needed(db: Session, class_id: uuid.UUID) -> set[tuple[str, uuid.UUID]]:
    """(kind, template) the class's sessions still use, in one query per class: a session of
    that mode and template that is not called off (drafts and sessions awaiting approval
    count: booking day by day must not wipe the Students' work; ADR 0014 §10)."""
    from .holds import kind_of

    rows = (
        db.query(ClassSession.range_mode, ScheduledEvent.template_id)
        .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
        .filter(
            ClassSession.class_id == class_id,
            ScheduledEvent.template_id.isnot(None),
            ScheduledEvent.state.notin_((EventState.cancelled, EventState.rejected)),
        )
        .distinct()
        .all()
    )
    return {(kind_of(mode), tid) for mode, tid in rows}


def _ends(db: Session, cls: Class) -> datetime:
    """The class's end: its last day, or its last session's end if that runs later."""
    from .holds import last_day_end

    last = (
        db.query(ScheduledEvent.end_time)
        .join(ClassSession, ClassSession.event_id == ScheduledEvent.id)
        .filter(ClassSession.class_id == cls.id,
                ScheduledEvent.state.notin_((EventState.cancelled, EventState.rejected)))
        .order_by(ScheduledEvent.end_time.desc())
        .first()
    )
    return max([last_day_end(cls.end_date), *([_aware(last[0])] if last else [])])


def _teardown_if_done(db: Session, row: ClassRange, now: datetime, needed: dict | None = None) -> bool:
    # What the row held when this pass read it: a commit below reloads the row, and a replica
    # may have rebuilt the seat onto a new range meanwhile.
    row_id, class_id, range_id, kind, seat_key = row.id, row.class_id, row.range_id, row.kind, row.seat_key
    cls = db.get(Class, class_id)  # tenant-safe: the row's own class
    needed = needed if needed is not None else {}
    if class_id not in needed:
        needed[class_id] = _needed(db, class_id)
    why = _why_done(db, cls, row, now, needed[class_id])
    if why is None:
        return False
    reason, bypass = why
    rng = db.get(Range, range_id)  # tenant-safe: the row's own range
    if rng is not None and rng.state == RangeState.created:
        settled, what = range_lifecycle.discard_unbuilt(db, rng.id)
    else:
        settled, what = range_lifecycle.destroy_for_booking(db, range_id, bypass)
    if not settled:
        _note(db, row_id, f"{WAITING_PREFIX}teardown: {what}")
        return False
    # tenant-safe: a row of the class's own, loaded by this tick. Only the range it tore down:
    # a replica that rebuilt the seat meanwhile keeps its new range standing.
    db.query(ClassRange).filter(
        ClassRange.id == row_id, ClassRange.range_id == range_id, ClassRange.torn_down_at.is_(None)
    ).update(
        # A status change (the range is gone), not a reference nulled on delete (ADR 0010).
        {ClassRange.last_error: None, ClassRange.torn_down_at: now, ClassRange.teardown_reason: reason},
        synchronize_session=False,
    )
    if cls is not None:
        how = f"teardown that always runs ({bypass})" if bypass else "normal teardown (a maintenance window defers it)"
        service.audit(db, None, cls, "class_range_teardown",
                      f"{kind} {seat_key}: range {range_id}: {reason}; {how}; {what}"[:2000])
    db.commit()
    return True


# ── The lead's retry ─────────────────────────────────────────────────────────


def retry(db: Session, cls: Class, row: ClassRange, actor) -> None:
    """Build a failed seat (or shared range) again. Its range must have failed, or its build
    never started. Audited. Commits (the build is a range operation)."""
    from fastapi import HTTPException

    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, "The class is not open; its ranges are not built again")
    if row.kind == ClassRangeKind.seat.value:
        entry = db.get(RollCallEntry, row.entry_id) if row.entry_id else None  # tenant-safe: the row's own entry
        if entry is None or entry.tenant_id != cls.tenant_id or entry.status in OFF_ROLL_CALL:
            raise HTTPException(409, "This Student is no longer on the roll call")
    rng = db.get(Range, row.range_id) if row.range_id else None  # tenant-safe: the row's own range
    if row.torn_down_at is not None or rng is None or status(row, rng) != "failed":
        raise HTTPException(409, "Only a range whose build failed can be retried")
    service.audit(db, actor.id, cls, "class_range_retry", f"{row.kind} {row.seat_key}: range {rng.id}")
    db.commit()
    built, what = range_lifecycle.provision_for_booking(db, rng.id)
    if not built:
        raise HTTPException(409, f"The build could not be started: {what}")
    _note(db, row.id, None)
