"""Approving class sessions over HTTP (ADR 0014 §14-16; issue #281). The rules are in
:mod:`.approval`.

===============================================  =========================================
Endpoint                                         Who
===============================================  =========================================
GET  /schedule/approvals                         schedule:approve: the tenant's queue
GET  /schedule/events/{id}/next-free             schedule:read: fits now? next free slot
POST /schedule/events/{id}/approve               schedule:approve
POST /schedule/events/{id}/reject                schedule:approve, with a reason
POST /schedule/events/{id}/propose               schedule:approve, with a time
POST /schedule/events/{id}/proposal/accept       the session's instructor or an admin
POST /schedule/events/{id}/proposal/decline      the session's instructor or an admin
===============================================  =========================================

Every booking is the caller's tenant's (404 otherwise). The router-level gate is
``schedule:read``, as for the rest of ``/schedule``: Students never get this far.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..classes import sessions
from ..db import get_db
from ..rbac import Permission, require_permission
from . import approval, service
from .router import _announce, _changeable, _owned
from .schemas import ApprovalItemOut, ApprovalQueueOut, EventOut, FitOut, ProposeIn, RejectIn, UtcDateTime, as_utc

router = APIRouter(
    prefix="/schedule",
    tags=["scheduling"],
    dependencies=[Depends(require_permission(Permission.SCHEDULE_READ))],
)
_APPROVE = [Depends(require_permission(Permission.SCHEDULE_APPROVE))]
_WRITE = [Depends(require_permission(Permission.SCHEDULE_WRITE))]


@router.get(
    "/approvals", response_model=ApprovalQueueOut, summary="Class sessions awaiting approval", dependencies=_APPROVE
)
def approval_queue(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """The caller's tenant's class sessions awaiting approval, soonest first, each with
    whether it fits now and the next free slot when it does not (ADR 0014 §16)."""
    events = approval.queue(db, user)
    outs = service.outs(db, events)
    class_ids = {o["class_id"] for o in outs if o["class_id"]}
    names = sessions.class_names(db, {uuid.UUID(c) for c in class_ids}, events[0].tenant_id) if events else {}
    items = []
    for evt, o in zip(events, outs, strict=True):
        checked = approval.check(db, evt, evt.start_time, evt.end_time)
        name = names.get(uuid.UUID(o["class_id"])) if o["class_id"] else None
        items.append(ApprovalItemOut(event=EventOut(**o), class_name=name, fit=checked.out))
    return ApprovalQueueOut(items=items)


@router.get("/events/{event_id}/next-free", response_model=FitOut, summary="Does a booking fit; the next free slot")
def next_free(
    event_id: str,
    after: UtcDateTime | None = Query(None, description="Search from here (default: the booking's own start)"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Whether the booking fits where it is, and the first slot of its length, on a
    15-minute grid from ``after``, where it would: no double booking and room for its
    demand, its build lead and teardown grace included. Searches 14 days
    (``SCHEDULER_NEXT_FREE_DAYS``)."""
    evt = _owned(db, event_id, user)
    if after is None:
        return approval.check(db, evt, evt.start_time, evt.end_time).out
    probe = approval.at_time(evt, after, after + (as_utc(evt.end_time) - as_utc(evt.start_time)))
    return approval.check(db, probe, probe.start_time, probe.end_time).out


@router.post("/events/{event_id}/approve", response_model=EventOut, summary="Approve a class session", dependencies=_APPROVE)
def approve_event(
    event_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """pending_approval -> scheduled, checked again now under the booking lock. 409 with
    ``{message, reasons, next_free}`` when it is double-booked or (policy ``block``) does not
    fit. Only now are the invites sent: to its instructor and its class's roll call."""
    service.serialize_bookings(db)
    evt = _owned(db, event_id, user)
    moved, warnings = approval.approve(db, user, evt)
    db.commit()
    db.refresh(evt)
    if moved:
        _announce(background, db, evt, "REQUEST")
    return service.out(db, evt, warnings, service.maintenance_warnings(db, evt))


@router.post("/events/{event_id}/reject", response_model=EventOut, summary="Reject a class session", dependencies=_APPROVE)
def reject_event(
    event_id: str, body: RejectIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    """pending_approval -> rejected, with a reason the instructor sees. Nothing was sent, so
    nothing is withdrawn."""
    evt = _owned(db, event_id, user)
    approval.reject(db, user, evt, body.reason)
    db.commit()
    db.refresh(evt)
    return service.out(db, evt)


@router.post(
    "/events/{event_id}/propose", response_model=EventOut, summary="Propose another time for a class session",
    dependencies=_APPROVE,
)
def propose_time(
    event_id: str, body: ProposeIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    """The time must fit now (409 with the next free slot otherwise). The session stays
    pending, holding nothing, until its instructor accepts or declines."""
    service.serialize_bookings(db)
    evt = _owned(db, event_id, user)
    warnings = approval.propose(db, user, evt, body.start_time, body.end_time)
    db.commit()
    db.refresh(evt)
    return service.out(db, evt, warnings)


@router.post(
    "/events/{event_id}/proposal/accept", response_model=EventOut, summary="Accept the proposed time",
    dependencies=_WRITE,
)
def accept_proposal(
    event_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """The session's instructor (or an admin) takes the approver's time. Checked again under
    the booking lock (409 with the next free slot when it no longer fits); it then moves and
    is approved, and its invites go out."""
    service.serialize_bookings(db)
    evt = _changeable(db, event_id, user)
    warnings = approval.accept(db, user, evt)
    db.commit()
    db.refresh(evt)
    _announce(background, db, evt, "REQUEST")
    return service.out(db, evt, warnings, service.maintenance_warnings(db, evt))


@router.post(
    "/events/{event_id}/proposal/decline", response_model=EventOut, summary="Decline the proposed time",
    dependencies=_WRITE,
)
def decline_proposal(event_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """The session stays pending at its own time, for the approver to decide."""
    evt = _changeable(db, event_id, user)
    approval.decline(db, user, evt)
    db.commit()
    db.refresh(evt)
    return service.out(db, evt)
