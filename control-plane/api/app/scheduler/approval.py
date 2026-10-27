"""Approving class sessions (ADR 0014 §14-16; issue #281).

Every class session is booked ``pending_approval``. Until an approver acts, nothing happens:
it holds no capacity, range or instructor (``lifecycle.HOLDING`` leaves it out), the clock
never builds it, and nobody is emailed or invited. Approvers hold ``schedule:approve``
(admins and range ops) and act on their own tenant's bookings only.

- **Approve** checks the session again, now, under the booking lock (``serialize_bookings``):
  no double booking, and room in the cluster. A double booking is always 409. Room follows
  the over-capacity policy, as every booking does: ``block`` (the default) refuses with 409,
  ``warn`` approves with warnings that are audit-logged. A 409 carries the reasons and the
  next free slot. Only then does the session become ``scheduled`` and its invites go out, to
  its instructor and its class's roll call.
- **Reject** needs a reason, which the instructor sees. Final.
- **Propose** suggests another time, checked as approve would check it. The booking stays
  pending. Its instructor **accepts** (checked again under the lock; the session moves there
  and is approved, on the approver's word) or **declines** (it stays pending at its own
  time, for the approver to decide).

Every decision is audit-logged against the booking. Plain bookings, with no class, stay
self-service and never come here (ADR 0014 §14 is about class bookings).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from . import lifecycle, service
from .capacity import PROVISION_LEAD, get_capacity_provider, window_label
from .models import BookingApproval, EventState, OvercapacityPolicy, ScheduledEvent
from .schemas import FitOut, SlotOut, as_utc

S = EventState


@dataclass(frozen=True)
class Checked:
    fit: service.Fit
    out: FitOut


def check(
    db: Session,
    evt: ScheduledEvent,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
    provider=None,
    *,
    strict: bool = False,
) -> Checked:
    """Would ``evt`` fit at [start, end), and if not, where next? From ``start`` on. Pass
    ``provider`` to share one capacity read across many checks (the queue).

    ``strict`` (a time an approver proposes): it must also lie within the class's dates and
    clear of maintenance windows. The next free slot always does both."""
    provider = provider or get_capacity_provider(db)
    f = service.fit(db, provider, evt, start, end)
    within = _class_dates(db, evt)
    if strict:
        f = service.Fit([*f.conflicts, *_off_limits(db, evt, start, end, within)], f.assessment)
    if f.ok:
        nxt, until = (as_utc(start), as_utc(end)), as_utc(start)
    else:
        probe = at_time(evt, start, end)
        nxt, until = service.next_free_slot(db, provider, probe, after=start, now=now, within=within)
    slot = SlotOut(start_time=nxt[0], end_time=nxt[1]) if nxt else None
    return Checked(f, FitOut(fits=f.ok, reasons=f.reasons, next_free=slot, searched_until=until))


def _off_limits(db: Session, evt: ScheduledEvent, start: datetime, end: datetime, within) -> list[str]:
    out = []
    if within is not None and not (within[0] <= as_utc(start).date() and as_utc(end).date() <= within[1]):
        out.append(f"Outside the class's dates ({within[0]} to {within[1]}, UTC)")
    if service.maintenance_spans(db, evt, as_utc(start) - PROVISION_LEAD, as_utc(end)):
        out.append("A maintenance window covers it: its range would not be built")
    return out


def _class_dates(db: Session, evt: ScheduledEvent):
    """A class session's class's first and last day, or None for a plain booking."""
    from ..classes import sessions

    cls = sessions.class_of(db, evt)
    return (cls.start_date, cls.end_date) if cls is not None else None


def at_time(evt: ScheduledEvent, start: datetime, end: datetime) -> ScheduledEvent:
    """A detached copy of ``evt`` at another time, to search from (never added to the session)."""
    return ScheduledEvent(
        id=evt.id, name=evt.name, tenant_id=evt.tenant_id, range_id=evt.range_id, instructor_id=evt.instructor_id,
        start_time=start, end_time=end, vcpu_total=evt.vcpu_total, ram_mb_total=evt.ram_mb_total,
        disk_gb_total=evt.disk_gb_total,
    )


def _refuse(message: str, checked: Checked) -> HTTPException:
    nxt = checked.out.next_free
    return HTTPException(
        409,
        {
            "message": message,
            "reasons": checked.out.reasons,
            "next_free": nxt.model_dump(mode="json") if nxt else None,
        },
    )


def _approval(db: Session, evt: ScheduledEvent) -> BookingApproval:
    a = service.approval_of(db, evt.id)
    if a is None:
        raise HTTPException(409, "This booking needs no approval: it is no class's session")
    return a


def _now() -> datetime:
    return datetime.now(UTC)


def _admit(
    db: Session, evt: ScheduledEvent, start: datetime, end: datetime, what: str, strict: bool = False
) -> list[str]:
    """Check [start, end) for ``evt`` as approving it there: 409 (with the next free slot)
    on a double booking, or on no room under ``block``; under ``warn`` the warnings."""
    if as_utc(end) <= _now():
        raise HTTPException(409, f"The session {window_label(start, end)} is already over")
    if evt.range_id:
        # A destroyed range cannot be built again; one of a Student's lab is not ours.
        from .. import range_lifecycle
        from ..models import Range, RangeState

        # tenant-safe: a booking's range, checked against its tenant when booked (resolve_range).
        rng = db.get(Range, evt.range_id)
        if rng is None or rng.state in (RangeState.destroying, RangeState.destroyed):
            raise HTTPException(409, "The booked range can no longer be built; the instructor must book another")
        if range_lifecycle.is_lab_range(db, rng.id):
            raise HTTPException(409, "The booked range belongs to a student's lab session")
    checked = check(db, evt, start, end, strict=strict)
    if checked.fit.conflicts:
        raise _refuse(f"{what}: the time is not free", checked)
    if not checked.fit.assessment.fits:
        if service.get_policy(db) is OvercapacityPolicy.block:
            raise _refuse(f"{what}: it does not fit the cluster", checked)
        return checked.fit.assessment.reasons
    return []


def _resend(evt: ScheduledEvent) -> None:
    """A session approved again after its invites were withdrawn (moved after approval)
    was cancelled in calendars at its current SEQUENCE: the new REQUEST must be newer, or a
    calendar keeps it cancelled (RFC 5546 §2.1.5). A first approval was never sent: 0 stays."""
    if evt.sequence:
        service.resequence(evt)


def _require_pending(evt: ScheduledEvent) -> None:
    if evt.state != S.pending_approval:
        raise HTTPException(409, f"The booking is {evt.state.value}, not awaiting approval")


def _size(db: Session, evt: ScheduledEvent) -> None:
    """An individual session is approved for the Students on its roll call now (#282)."""
    from ..classes import sessions

    sessions.size_for_approval(db, evt)


def _class_open(db: Session, evt: ScheduledEvent) -> None:
    from ..classes import sessions

    cls = sessions.class_of(db, evt)
    if cls is not None and cls.status != "open":
        raise HTTPException(409, f"The class is {cls.status}; its sessions are not approved or moved")


def approve(db: Session, user: CurrentUser, evt: ScheduledEvent) -> tuple[bool, list[str]]:
    """pending_approval -> scheduled. Returns (moved, over-capacity warnings). Approving one
    already approved is a no-op. The caller holds ``serialize_bookings`` and commits."""
    a = _approval(db, evt)
    if evt.state == S.scheduled:
        return False, []
    _require_pending(evt)
    _class_open(db, evt)
    _size(db, evt)
    warnings = _admit(db, evt, evt.start_time, evt.end_time, "Cannot approve")
    lifecycle.transition(db, evt, S.scheduled)
    _resend(evt)
    a.decided_by, a.decided_at, a.reason = service.user_uuid(user), _now(), None
    a.proposed_start = a.proposed_end = a.proposed_by = a.proposed_at = None
    service.audit(db, user, "approve", str(evt.id), "pending_approval -> scheduled")
    if warnings:
        service.audit(db, user, "overcapacity_warning", str(evt.id), "; ".join(warnings))
    return True, warnings


def reject(db: Session, user: CurrentUser, evt: ScheduledEvent, reason: str) -> bool:
    """pending_approval -> rejected, with the reason the instructor sees. Final. The caller commits."""
    a = _approval(db, evt)
    if evt.state == S.rejected:
        return False
    _require_pending(evt)
    lifecycle.transition(db, evt, S.rejected)
    a.decided_by, a.decided_at, a.reason = service.user_uuid(user), _now(), reason
    a.proposed_start = a.proposed_end = a.proposed_by = a.proposed_at = None
    service.audit(db, user, "reject", str(evt.id), f"pending_approval -> rejected: {reason}"[:2000])
    return True


def propose(db: Session, user: CurrentUser, evt: ScheduledEvent, start: datetime, end: datetime) -> list[str]:
    """Suggest [start, end) instead. It must fit now, as approving there would check (409
    with the next free slot otherwise). The booking stays pending; nothing is held. The
    caller holds ``serialize_bookings`` and commits."""
    a = _approval(db, evt)
    _require_pending(evt)
    _class_open(db, evt)
    if end <= start:
        raise HTTPException(400, "end_time must be after start_time")
    if as_utc(start) <= _now():
        raise HTTPException(422, "Propose a time in the future")
    warnings = _admit(db, evt, start, end, "Cannot propose that time", strict=True)
    a.proposed_start, a.proposed_end = as_utc(start), as_utc(end)
    a.proposed_by, a.proposed_at = service.user_uuid(user), _now()
    service.audit(db, user, "propose", str(evt.id), f"proposed {window_label(start, end)}")
    return warnings


def accept(db: Session, user: CurrentUser, evt: ScheduledEvent) -> list[str]:
    """The instructor takes the proposed time: checked again now (409 with the next free
    slot when it no longer fits), the session moves there and is approved, on the word of
    the approver who proposed it. The caller holds ``serialize_bookings`` and commits."""
    a = _approval(db, evt)
    _require_pending(evt)
    if a.proposed_start is None or a.proposed_end is None:
        raise HTTPException(409, "No time has been proposed")
    _class_open(db, evt)
    lifecycle.lock_editable(db, evt)
    _size(db, evt)
    start, end = as_utc(a.proposed_start), as_utc(a.proposed_end)
    probe = at_time(evt, start, end)
    warnings = _admit(db, probe, start, end, "The proposed time no longer fits")
    before = window_label(evt.start_time, evt.end_time)
    evt.start_time, evt.end_time, evt.reminded_at = start, end, None
    lifecycle.transition(db, evt, S.scheduled)
    _resend(evt)
    a.decided_by, a.decided_at, a.reason = a.proposed_by, _now(), None
    a.proposed_start = a.proposed_end = a.proposed_by = a.proposed_at = None
    service.audit(
        db, user, "accept_proposal", str(evt.id),
        f"moved {before} -> {window_label(start, end)}; approved as proposed by {a.decided_by}",
    )
    if warnings:
        service.audit(db, user, "overcapacity_warning", str(evt.id), "; ".join(warnings))
    return warnings


def decline(db: Session, user: CurrentUser, evt: ScheduledEvent) -> None:
    """The instructor keeps their own time: the proposal goes, the booking stays pending for
    the approver to approve, reject or propose again. The caller commits."""
    a = _approval(db, evt)
    _require_pending(evt)
    if a.proposed_start is None:
        raise HTTPException(409, "No time has been proposed")
    was = window_label(a.proposed_start, a.proposed_end)
    a.proposed_start = a.proposed_end = a.proposed_by = a.proposed_at = None
    service.audit(db, user, "decline_proposal", str(evt.id), f"declined {was}")


def request(db: Session, user: CurrentUser, evt: ScheduledEvent) -> BookingApproval:
    """A new class session: it needs an approver. Does not commit."""
    a = BookingApproval(
        event_id=evt.id, tenant_id=evt.tenant_id, requested_by=service.user_uuid(user), requested_at=_now()
    )
    db.add(a)
    return a


def back_to_approval(db: Session, user: CurrentUser, evt: ScheduledEvent, why: str) -> None:
    """An approved session the instructor moved or resized needs approving again: it stops
    holding anything until then. Does not commit."""
    a = _approval(db, evt)
    lifecycle.transition(db, evt, S.pending_approval)
    a.decided_by = a.decided_at = None
    a.requested_by, a.requested_at = service.user_uuid(user), _now()
    service.audit(db, user, "approval_requested", str(evt.id), f"scheduled -> pending_approval: {why}")


def withdraw_proposal(db: Session, evt: ScheduledEvent) -> bool:
    """The instructor moved a pending session themselves: an open proposal is moot."""
    a = service.approval_of(db, evt.id)
    if a is None or a.proposed_start is None:
        return False
    a.proposed_start = a.proposed_end = a.proposed_by = a.proposed_at = None
    return True


def queue(db: Session, user: CurrentUser, limit: int = 50) -> list[ScheduledEvent]:
    """The caller's tenant's sessions awaiting approval, soonest first."""
    from ..tenancy import tenant_uuid

    return (
        db.query(ScheduledEvent)
        .join(BookingApproval, BookingApproval.event_id == ScheduledEvent.id)
        .filter(
            ScheduledEvent.tenant_id == tenant_uuid(user),
            BookingApproval.tenant_id == tenant_uuid(user),
            ScheduledEvent.state == S.pending_approval,
        )
        .order_by(ScheduledEvent.start_time)
        .limit(limit)
        .all()
    )
