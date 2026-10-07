"""TrueNorth Range - Event scheduling router with resource reservation.

Prevents over-commitment by tracking resource claims per time window.
The ``/schedule/check`` endpoint lets the UI forecast whether a given
deployment will fit within the cluster capacity at a given time.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..rbac import Permission, is_platform_admin, require_permission, require_platform_admin
from ..tenancy import get_owned, tenant_uuid
from . import calendar_backends, clock, feed, invites, lifecycle, service
from .capacity import PROVISION_LEAD, TEARDOWN_GRACE, Resources, available, get_capacity_provider
from .models import EventState, ScheduledEvent
from .schemas import (
    CapacityCheck,
    CapacityResult,
    EventIn,
    EventListOut,
    EventOut,
    FeedTokenIssued,
    FeedTokenStatus,
    MySessionOut,
    PolicyIn,
    PolicyOut,
    TimelineOut,
    UtcDateTime,
)
from .service import to_out as _to_out

logger = logging.getLogger("truenorth.api.scheduling")

# Reading needs schedule:read, which every role except student holds (ADR 0004): Students
# never see other bookings, capacity or the timeline. Writes add schedule:write on top.
# This router once shipped with no authentication at all, and then with only
# exercise:read, which Students hold; the gate stays at router level so a new endpoint
# cannot ship ungated.
router = APIRouter(
    prefix="/schedule",
    tags=["scheduling"],
    dependencies=[Depends(require_permission(Permission.SCHEDULE_READ))],
)
_WRITE = [Depends(require_permission(Permission.SCHEDULE_WRITE))]


# -- Endpoints -----------------------------------------------------------


@router.get("/capacity", response_model=CapacityResult, summary="Current cluster capacity")
def get_capacity(
    start_time: datetime = Query(None, description="Window start (default: now)"),
    end_time: datetime = Query(None, description="Window end (default: +8h)"),
    db: Session = Depends(get_db),
):
    """Return current usable capacity minus all scheduled/active event reservations."""
    from datetime import timedelta

    now = datetime.now(UTC)
    start = start_time or now
    end = end_time or (now + timedelta(hours=8))

    provider = get_capacity_provider(db)
    supply = provider.supply()
    committed = provider.committed(db, start, end)
    free = available(supply, committed.resources)
    c = committed.resources

    return CapacityResult(
        fits=True,
        vcpu_available=free.vcpu,
        vcpu_committed=c.vcpu,
        vcpu_total=supply.vcpu,
        ram_mb_available=free.ram_mb,
        ram_mb_committed=c.ram_mb,
        ram_mb_total=supply.ram_mb,
        disk_gb_available=free.disk_gb,
        disk_gb_committed=c.disk_gb,
        disk_gb_total=supply.disk_gb,
        overlapping_events=committed.events,
        message=f"{committed.events} event(s) in window",
        policy=service.get_policy(db).value,
        supply_source=provider.supply_source,
    )


@router.post("/check", response_model=CapacityResult, summary="Check if deployment fits")
def check_capacity(body: CapacityCheck, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Check whether a proposed deployment fits within the cluster at the given time.

    With `template_id`, the need is that template's VM specs. The window includes the
    provisioning lead and teardown grace, exactly as booking it would."""
    if body.end_time <= body.start_time:
        raise HTTPException(400, "end_time must be after start_time")
    typed = Resources(body.vcpu_needed, body.ram_mb_needed, body.disk_gb_needed)
    demand = service.demand_for(db, user, body.template_id, typed)
    need = demand.resources
    provider = get_capacity_provider(db)
    a = service.assess(db, provider, body.start_time, body.end_time, need)
    c = a.committed.resources

    return CapacityResult(
        fits=a.fits,
        vcpu_available=a.free.vcpu,
        vcpu_committed=c.vcpu + (need.vcpu if a.fits else 0),
        vcpu_total=a.supply.vcpu,
        ram_mb_available=a.free.ram_mb,
        ram_mb_committed=c.ram_mb + (need.ram_mb if a.fits else 0),
        ram_mb_total=a.supply.ram_mb,
        disk_gb_available=a.free.disk_gb,
        disk_gb_committed=c.disk_gb + (need.disk_gb if a.fits else 0),
        disk_gb_total=a.supply.disk_gb,
        overlapping_events=a.committed.events,
        message="Resources available" if a.fits else "; ".join(a.reasons),
        vm_count_needed=demand.vm_count,
        vcpu_needed=need.vcpu,
        ram_mb_needed=need.ram_mb,
        disk_gb_needed=need.disk_gb,
        reasons=a.reasons,
        policy=service.get_policy(db).value,
        supply_source=provider.supply_source,
    )


@router.get("/policy", response_model=PolicyOut, summary="Over-capacity policy")
def get_policy(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """`block`: a booking that does not fit is refused for everyone. `warn`: it is
    created, with warnings in the response, and the warning is audit-logged."""
    return PolicyOut(overcapacity=service.get_policy(db), can_change=is_platform_admin(user))


@router.put(
    "/policy",
    response_model=PolicyOut,
    summary="Set the over-capacity policy (platform admin)",
    dependencies=[Depends(require_permission(Permission.SCHEDULE_ADMIN)), Depends(require_platform_admin())],
)
def put_policy(body: PolicyIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Platform-wide, because every tenant books against the same cluster. Audit-logged."""
    service.set_policy(db, user, body.overcapacity)
    db.commit()
    return PolicyOut(overcapacity=service.get_policy(db), can_change=True)


@router.get("/events", response_model=EventListOut, summary="List scheduled events")
def list_events(
    state: str | None = Query(None),
    start: UtcDateTime | None = Query(None, description="Only events ending after this"),
    end: UtcDateTime | None = Query(None, description="Only events starting before this"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List the caller's tenant's scheduled events, optionally filtered by state and by a
    window they overlap (the calendar's week)."""
    q = (
        db.query(ScheduledEvent)
        .filter(ScheduledEvent.tenant_id == tenant_uuid(user))
        .order_by(ScheduledEvent.start_time)
    )
    if state:
        q = q.filter(ScheduledEvent.state == state)
    if start:
        q = q.filter(ScheduledEvent.end_time > start)
    if end:
        q = q.filter(ScheduledEvent.start_time < end)
    total = q.count()
    events = q.offset(offset).limit(limit).all()
    return {"items": [_to_out(e) for e in events], "total": total}


@router.post(
    "/events", status_code=201, response_model=EventOut, summary="Create a scheduled event", dependencies=_WRITE
)
def create_event(
    body: EventIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Book a session. Refused with 409 when its range or instructor is already booked
    then. One that does not fit the cluster is refused with 409 (policy `block`) or
    created with warnings (policy `warn`). A draft holds nothing and is checked when it
    is scheduled."""
    _check_times(body)
    service.serialize_bookings(db)
    range_id = service.resolve_range(db, user, body.range_id)
    instructor_id = service.resolve_instructor(db, user, body.instructor_id)
    course_id = service.resolve_course(db, user, body.course_id)
    scenario_id = service.resolve_scenario(db, user, body.scenario_id)
    exercise_id, range_id = service.resolve_exercise(db, user, body.exercise_id, range_id)
    if range_id:
        service.resolve_range(db, user, str(range_id))  # an exercise's range must still be buildable
    typed = Resources(body.vcpu_total, body.ram_mb_total, body.disk_gb_total)
    demand = service.demand_for(db, user, body.template_id, typed, body.vm_count)
    warnings = [] if body.draft else _admit(db, body, demand.resources, range_id, instructor_id)

    evt = ScheduledEvent(
        name=body.name,
        description=body.description,
        state=EventState.draft if body.draft else EventState.scheduled,
        tenant_id=tenant_uuid(user),
        range_id=range_id,
        template_id=uuid.UUID(body.template_id) if body.template_id else None,
        instructor_id=instructor_id,
        course_id=course_id,
        scenario_id=scenario_id,
        exercise_id=exercise_id,
        created_by=service.user_uuid(user),
        start_time=body.start_time,
        end_time=body.end_time,
        vm_count=demand.vm_count,
        vcpu_total=demand.resources.vcpu,
        ram_mb_total=demand.resources.ram_mb,
        disk_gb_total=demand.resources.disk_gb,
    )
    db.add(evt)
    db.flush()
    service.audit(db, user, "create", str(evt.id), evt.state.value)
    _audit_warnings(db, user, evt, warnings)
    db.commit()
    db.refresh(evt)
    if evt.state == EventState.scheduled:
        _announce(background, db, evt, "REQUEST")
    return _to_out(evt, warnings)


def _announce(
    background: BackgroundTasks,
    db: Session,
    evt: ScheduledEvent,
    method: str,
    previous_instructor: uuid.UUID | None = None,
    previous_course: uuid.UUID | None = None,
) -> None:
    """After the response: email the invite (slice 6) and sync any external calendar
    (CALENDAR_BACKEND, slice 7). Both are built now, while the session is open."""
    background.add_task(
        invites.send,
        invites.plan(db, evt, method, previous_instructor=previous_instructor, previous_course=previous_course),
    )
    background.add_task(calendar_backends.push, invites.neutral_event(db, evt), method == "CANCEL")


def _same_instant(a: datetime, b: datetime) -> bool:
    return a.replace(tzinfo=a.tzinfo or UTC) == b.replace(tzinfo=b.tzinfo or UTC)


def _check_times(body) -> None:
    if body.end_time <= body.start_time:
        raise HTTPException(400, "end_time must be after start_time")


def _admit(
    db: Session,
    window,
    need: Resources,
    range_id: uuid.UUID | None,
    instructor_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> list[str]:
    """Conflicts are always refused; capacity follows the over-capacity policy."""
    clash = service.conflicts(
        db,
        start=window.start_time,
        end=window.end_time,
        range_id=range_id,
        instructor_id=instructor_id,
        exclude_id=exclude_id,
    )
    if clash:
        raise HTTPException(409, "; ".join(clash))
    a = service.assess(db, get_capacity_provider(db), window.start_time, window.end_time, need, exclude_id)
    return service.enforce(db, a)


def _audit_warnings(db: Session, user: CurrentUser, evt: ScheduledEvent, warnings: list[str]) -> None:
    if warnings:
        service.audit(db, user, "overcapacity_warning", str(evt.id), "; ".join(warnings))


def _owned(db: Session, event_id: str, user: CurrentUser) -> ScheduledEvent:
    return get_owned(db, ScheduledEvent, lifecycle.event_id(event_id), user, not_found="Event not found")


@router.get("/events/{event_id}", response_model=EventOut, summary="Get a scheduled event")
def get_event(event_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return _to_out(_owned(db, event_id, user))


@router.put("/events/{event_id}", response_model=EventOut, summary="Update a scheduled event", dependencies=_WRITE)
def update_event(
    event_id: str,
    body: EventIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Reschedule or resize a draft or scheduled booking (409 once it is being built)."""
    _check_times(body)
    service.serialize_bookings(db)
    evt = _owned(db, event_id, user)
    lifecycle.lock_editable(db, evt)
    range_id = service.resolve_range(db, user, body.range_id)
    scenario_id = service.resolve_scenario(db, user, body.scenario_id)
    exercise_id, range_id = service.resolve_exercise(db, user, body.exercise_id, range_id)
    instructor_id = (
        service.resolve_instructor(db, user, body.instructor_id) if body.instructor_id else evt.instructor_id
    )
    typed = Resources(body.vcpu_total, body.ram_mb_total, body.disk_gb_total)
    demand = service.demand_for(db, user, body.template_id, typed, body.vm_count)
    warnings = (
        []
        if evt.state == EventState.draft
        else _admit(db, body, demand.resources, range_id, instructor_id, exclude_id=evt.id)
    )

    previous_instructor, previous_course = evt.instructor_id, evt.course_id
    course_id = service.resolve_course(db, user, body.course_id)
    if not _same_instant(evt.start_time, body.start_time):
        evt.reminded_at = None  # moved: remind again for the new time
    evt.name = body.name
    evt.description = body.description
    evt.start_time = body.start_time
    evt.end_time = body.end_time
    evt.vm_count = demand.vm_count
    evt.vcpu_total = demand.resources.vcpu
    evt.ram_mb_total = demand.resources.ram_mb
    evt.disk_gb_total = demand.resources.disk_gb
    evt.range_id = range_id
    evt.template_id = uuid.UUID(body.template_id) if body.template_id else None
    evt.instructor_id = instructor_id
    evt.course_id = course_id
    evt.scenario_id = scenario_id
    evt.exercise_id = exercise_id
    if evt.state != EventState.draft:  # a draft was never sent to a calendar
        evt.sequence = (evt.sequence or 0) + 1  # calendars replace their copy
    service.audit(db, user, "update", str(evt.id))
    _audit_warnings(db, user, evt, warnings)
    db.commit()
    db.refresh(evt)
    if evt.state == EventState.scheduled:  # an updated invite: same UID, higher SEQUENCE
        _announce(background, db, evt, "REQUEST", previous_instructor, previous_course)
    return _to_out(evt, warnings)


@router.post("/events/{event_id}/schedule", response_model=EventOut, summary="Schedule a draft", dependencies=_WRITE)
def schedule_event(
    event_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """draft -> scheduled, checked for conflicts and capacity exactly as a new booking."""
    service.serialize_bookings(db)
    evt = _owned(db, event_id, user)
    warnings: list[str] = []
    if evt.state == EventState.draft:
        service.resolve_range(db, user, str(evt.range_id) if evt.range_id else None)  # still buildable?
        need = Resources(evt.vcpu_total, evt.ram_mb_total, evt.disk_gb_total)
        warnings = _admit(db, evt, need, evt.range_id, evt.instructor_id, exclude_id=evt.id)
    return _move(db, user, evt, EventState.scheduled, warnings, background)


@router.post(
    "/events/{event_id}/activate", response_model=EventOut, summary="Mark event as active", dependencies=_WRITE
)
def activate_event(event_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """scheduled or provisioning -> active."""
    return _move(db, user, _owned(db, event_id, user), EventState.active)


@router.post(
    "/events/{event_id}/complete", response_model=EventOut, summary="Mark event as completed", dependencies=_WRITE
)
def complete_event(event_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """active -> completed."""
    return _move(db, user, _owned(db, event_id, user), EventState.completed)


@router.post("/events/{event_id}/cancel", response_model=EventOut, summary="Cancel an event", dependencies=_WRITE)
def cancel_event(
    event_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Any state before completed -> cancelled. The row stays as history."""
    return _move(db, user, _owned(db, event_id, user), EventState.cancelled, background=background)


@router.delete(
    "/events/{event_id}", status_code=204, response_class=Response, summary="Cancel an event", dependencies=_WRITE
)
def delete_event(
    event_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Same as POST .../cancel. Events are no longer hard-deleted: a cancelled booking
    is history, like a completed one."""
    _move(db, user, _owned(db, event_id, user), EventState.cancelled, background=background)


def _move(
    db: Session,
    user: CurrentUser,
    evt: ScheduledEvent,
    to: EventState,
    warnings: list[str] | None = None,
    background: BackgroundTasks | None = None,
) -> dict:
    before = evt.state.value
    moved = lifecycle.transition(db, evt, to)
    if moved:
        detail = f"{before} -> {to.value}"
        # Cancelling after the clock built the range: take down what we built.
        if to == EventState.cancelled:
            evt.sequence = (evt.sequence or 0) + 1
            for what in (service.release_range(db, evt), service.withdraw_exercise(db, evt)):
                if what:
                    detail += f"; {what}"
        service.audit(db, user, "transition", str(evt.id), detail)
        _audit_warnings(db, user, evt, warnings or [])
        db.commit()
    db.refresh(evt)
    if moved and background is not None:
        # Invite when a booking becomes real; withdraw it when one the Instructor was
        # invited to is called off. Drafts were never sent.
        if to == EventState.scheduled:
            _announce(background, db, evt, "REQUEST")
        elif to == EventState.cancelled and before != EventState.draft.value:
            _announce(background, db, evt, "CANCEL")
    return _to_out(evt, warnings)


@router.post(
    "/tick",
    summary="Run the scheduler clock now (platform admin)",
    dependencies=[Depends(require_permission(Permission.SCHEDULE_ADMIN)), Depends(require_platform_admin())],
)
async def run_tick(db: Session = Depends(get_db)):
    """One clock pass, as the background clock runs every minute: provision at the lead,
    activate at the start, complete and tear down after the grace, send reminders."""
    result = await asyncio.to_thread(clock.tick, db)  # sync DB and broker calls: off the event loop
    await clock.send_reminders(result.reminders)
    return result.summary()


@router.get("/timeline", response_model=TimelineOut, summary="Resource timeline for capacity planning")
def resource_timeline(
    days: int = Query(7, ge=1, le=90),
    start: UtcDateTime | None = Query(None, description="First slot (default: the current hour)"),
    resolution_minutes: int = Query(60, description="Slot length: 15, 30 or 60"),
    db: Session = Depends(get_db),
):
    """Committed capacity per slot, build and teardown time included, so back-to-back
    sessions are not counted as concurrent at a fine enough resolution. Feeds the
    scheduler's load bars and the Range Ops heatmap."""
    from datetime import timedelta

    if resolution_minutes not in (15, 30, 60):
        raise HTTPException(422, "resolution_minutes must be 15, 30 or 60")
    if days * 24 * 60 // resolution_minutes > 2880:
        raise HTTPException(422, "Too many slots: shorten `days` or use a coarser resolution")
    provider = get_capacity_provider(db)
    first = start or datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    step = timedelta(minutes=resolution_minutes)
    series = provider.committed_series(db, first, first + timedelta(days=days), step)
    buckets = [
        {
            "time": (first + i * step).isoformat(),
            "vcpu_committed": c.resources.vcpu,
            "ram_mb_committed": c.resources.ram_mb,
            "disk_gb_committed": c.resources.disk_gb,
            "event_count": c.events,
        }
        for i, c in enumerate(series)
    ]
    supply = provider.supply()
    return {
        "buckets": buckets,
        "cluster_vcpu": supply.vcpu,
        "cluster_ram_mb": supply.ram_mb,
        "cluster_disk_gb": supply.disk_gb,
        "supply_source": provider.supply_source,
        "resolution_minutes": resolution_minutes,
        "lead_minutes": int(PROVISION_LEAD.total_seconds() // 60),
        "grace_minutes": int(TEARDOWN_GRACE.total_seconds() // 60),
    }


# -- Your own: sessions and calendar feed (every signed-in user, Students included) --
# Students hold no schedule:read (ADR 0004): they never see the calendar, capacity or
# anyone else. They do get their own sessions, here and in their feed.
me_router = APIRouter(prefix="/schedule", tags=["scheduling"], dependencies=[Depends(get_current_user)])


@me_router.get("/mine", response_model=list[MySessionOut], summary="Your upcoming sessions")
def my_sessions(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Sessions you teach, and for Students the sessions of courses they are enrolled in."""
    return [
        MySessionOut(**{k: v for k, v in _to_out(e).items() if k in MySessionOut.model_fields})
        for e in service.my_sessions(db, user)
    ]


@me_router.get("/feed-token", response_model=FeedTokenStatus, summary="Whether you have a calendar feed")
def feed_token_status(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    issued = feed.issued_at(db, user)
    return FeedTokenStatus(active=issued is not None, issued_at=issued)


@me_router.post("/feed-token", response_model=FeedTokenIssued, summary="Create or regenerate your calendar feed URL")
def feed_token_issue(request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Returns the subscription URL once. Regenerating stops the previous URL working."""
    token = feed.issue(db, user)
    service.audit(db, user, "feed_token_issued", str(user.id))
    db.commit()
    url = feed.feed_url(request, token)
    return FeedTokenIssued(url=url, webcal_url=feed.webcal(url), issued_at=feed.issued_at(db, user))


@me_router.delete("/feed-token", status_code=204, response_class=Response, summary="Revoke your calendar feed URL")
def feed_token_revoke(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    if feed.revoke(db, user):
        service.audit(db, user, "feed_token_revoked", str(user.id))
        db.commit()


# Calendar clients cannot sign in: this route is authenticated by the token in its path
# and is listed in tests/api/test_auth_coverage_guard.py PUBLIC_PATHS for that reason.
feed_router = APIRouter(prefix="/schedule", tags=["scheduling"])


@feed_router.get(
    "/feed/{token}.ics",
    summary="Calendar feed (iCalendar)",
    response_class=Response,
    responses={200: {"content": {"text/calendar": {}}}, 404: {"description": "Unknown or revoked feed"}},
)
def calendar_feed(token: str, db: Session = Depends(get_db)):
    owner = feed.owner_for(db, token)
    if owner is None:
        # One answer for unknown, revoked and no-longer-permitted: nothing to probe.
        raise HTTPException(404, "Not found")
    return Response(
        content=feed.render(db, owner),
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, max-age=300", "Content-Disposition": 'inline; filename="truenorth.ics"'},
    )
