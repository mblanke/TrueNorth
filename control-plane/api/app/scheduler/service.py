"""Scheduler logic, and the only way other sections reach scheduler data.

Capacity comes from a :class:`~.capacity.CapacityProvider` (CapacityService (ADR 0006)
once it lands); this module decides what a booking needs and what to do when it does
not fit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from .. import exercise_lifecycle, range_lifecycle
from ..auth import CurrentUser
from ..enrollment import active_course_ids
from ..models import AuditLog, Course, Exercise, Range, RangeState, Scenario, Template, User, UserRole
from ..range_topology import template_demand
from ..tenancy import get_owned, get_owned_or_global, tenant_uuid
from .capacity import (
    PROVISION_LEAD,
    TEARDOWN_GRACE,
    CapacityProvider,
    Committed,
    Resources,
    available,
    held_window,
    shortfalls,
    window_label,
)
from .lifecycle import HOLDING
from .models import EventState, OvercapacityPolicy, ScheduledEvent, SchedulerSetting
from .schemas import EventOut, as_utc

# States in which an event still holds its range, for range deletion. A draft counts:
# deleting the range would silently break a booking someone is still preparing.
RESERVING_STATES = (EventState.draft, *HOLDING)

POLICY_KEY = "overcapacity_policy"
DEFAULT_POLICY = OvercapacityPolicy.block


# -- Demand -------------------------------------------------------------------
@dataclass(frozen=True)
class Demand:
    resources: Resources
    vm_count: int
    source: str  # "template" or "typed"


def demand_for(
    db: Session, user: CurrentUser, template_id: str | None, typed: Resources, typed_vm_count: int = 0
) -> Demand:
    """What a booking needs: the template's VM specs when it names a template, else the
    typed totals (a booking for a range alone, until slice 3 derives those too)."""
    if not template_id:
        return Demand(typed, typed_vm_count, "typed")
    tmpl = (
        db.query(Template)
        .filter(
            Template.id == uuid.UUID(template_id),
            Template.deleted_at.is_(None),
            # Same visibility rule as GET /templates: own tenant's, or public.
            (Template.tenant_id == tenant_uuid(user)) | (Template.is_public == True),  # noqa: E712
        )
        .first()
    )
    if not tmpl:
        raise HTTPException(404, "Template not found")
    d = template_demand(tmpl.yaml)
    if d is None:
        raise HTTPException(422, "Template declares no VMs (no `nodes` or `assets`), so its size is unknown")
    return Demand(Resources(d.vcpu, d.ram_mb, d.disk_gb), d.vm_count, "template")


# -- Fit ----------------------------------------------------------------------
@dataclass(frozen=True)
class Assessment:
    supply: Resources
    committed: Committed
    free: Resources
    reasons: list[str] = field(default_factory=list)

    @property
    def fits(self) -> bool:
        return not self.reasons


def assess(
    db: Session,
    provider: CapacityProvider,
    start: datetime,
    end: datetime,
    need: Resources,
    exclude_id: uuid.UUID | None = None,
) -> Assessment:
    """Would a booking for [start, end) fit? Counts its provisioning lead and teardown grace."""
    held_start, held_end = held_window(start, end)
    supply = provider.supply()
    committed = provider.committed(db, held_start, held_end, exclude_id)
    free = available(supply, committed.resources)
    return Assessment(supply, committed, free, shortfalls(need, free, held_start, held_end))


def enforce(db: Session, assessment: Assessment) -> list[str]:
    """Apply the over-capacity policy. Raises 409 under `block`; under `warn` returns the
    warnings, which the caller puts in the response and audit-logs against the booking."""
    if assessment.fits:
        return []
    if get_policy(db) is OvercapacityPolicy.block:
        raise HTTPException(409, "; ".join(assessment.reasons))
    return assessment.reasons


# -- Conflicts ----------------------------------------------------------------
def serialize_bookings(db: Session) -> None:
    """Hold a transaction-scoped lock while checking and writing a booking, so two
    concurrent bookings cannot both pass the capacity and conflict checks. Postgres
    only; SQLite (tests) serialises writers anyway."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('truenorth.scheduler.bookings'))"))


def resolve_instructor(db: Session, user: CurrentUser, instructor_id: str | None) -> uuid.UUID | None:
    """The booking's instructor: the one named, else the caller when they are an instructor.
    A named instructor must be an active instructor or admin in the caller's tenant."""
    if not instructor_id:
        return user_uuid(user) if user.role == UserRole.instructor else None
    try:
        iid = uuid.UUID(instructor_id)
    except ValueError:
        raise HTTPException(422, "instructor_id is not a valid id") from None
    ok = (
        db.query(User.id)
        .filter(
            User.id == iid,
            User.tenant_id == tenant_uuid(user),
            User.role.in_([UserRole.instructor, UserRole.admin]),
            User.deleted_at.is_(None),
            User.is_active == True,  # noqa: E712
        )
        .first()
    )
    if not ok:
        raise HTTPException(422, "instructor_id must be an active instructor or admin in your tenant")
    return iid


def resolve_course(db: Session, user: CurrentUser, course_id: str | None) -> uuid.UUID | None:
    """The class: a course of the caller's tenant, or a shared catalogue course."""
    if not course_id:
        return None
    return get_owned_or_global(db, Course, course_id, user, not_found="Course not found").id


def resolve_scenario(db: Session, user: CurrentUser, scenario_id: str | None) -> uuid.UUID | None:
    """A scenario of the caller's tenant, or a public one (as GET /scenarios lists them)."""
    if not scenario_id:
        return None
    try:
        sid = uuid.UUID(scenario_id)
    except ValueError:
        raise HTTPException(404, "Scenario not found") from None
    found = (
        db.query(Scenario.id)
        .filter(Scenario.id == sid, (Scenario.tenant_id == tenant_uuid(user)) | (Scenario.is_public == True))  # noqa: E712
        .first()
    )
    if not found:
        raise HTTPException(404, "Scenario not found")
    return sid


def resolve_exercise(
    db: Session, user: CurrentUser, exercise_id: str | None, range_id: uuid.UUID | None
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """A linked exercise brings its range. Returns (exercise_id, range_id); 422 when the
    booking names a different range than the exercise runs on."""
    if not exercise_id:
        return None, range_id
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if range_id and range_id != ex.range_id:
        raise HTTPException(422, "The exercise runs on a different range than the one booked")
    return ex.id, ex.range_id


def resolve_range(db: Session, user: CurrentUser, range_id: str | None) -> uuid.UUID | None:
    """A booked range must be the caller's tenant's (404 otherwise, as for any range),
    must still be buildable (a destroyed range cannot be provisioned again), and must not
    be a lab session's (only its session drives it)."""
    if not range_id:
        return None
    rng = get_owned(db, Range, range_id, user, not_found="Range not found")
    if rng.state in (RangeState.destroying, RangeState.destroyed):
        raise HTTPException(409, f"Range is {rng.state.value} and cannot be built again; book another range")
    if range_lifecycle.is_lab_range(db, rng.id):
        # The clock would build and tear it down under a student's session.
        raise HTTPException(409, "This range belongs to a student's lab session; book another range")
    return rng.id


def conflicts(
    db: Session,
    *,
    start: datetime,
    end: datetime,
    range_id: uuid.UUID | None,
    instructor_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> list[str]:
    """Double bookings (ADR 0004, "Conflicts").

    - A range is held from its provisioning lead to its teardown grace, so two bookings
      of one range need that much room between them.
    - An instructor is held for the session itself.
    Both are within one tenant: ranges and instructors belong to one.
    """
    out: list[str] = []

    def others(*crit):
        q = db.query(ScheduledEvent).filter(ScheduledEvent.state.in_(HOLDING), *crit)
        if exclude_id:
            q = q.filter(ScheduledEvent.id != exclude_id)
        return q.order_by(ScheduledEvent.start_time).all()

    if range_id:
        reach = PROVISION_LEAD + TEARDOWN_GRACE
        for e in others(
            ScheduledEvent.range_id == range_id,
            ScheduledEvent.start_time < end + reach,
            ScheduledEvent.end_time > start - reach,
        ):
            out.append(f"Range is already booked for '{e.name}' {window_label(e.start_time, e.end_time)}")
    if instructor_id:
        for e in others(
            ScheduledEvent.instructor_id == instructor_id,
            ScheduledEvent.start_time < end,
            ScheduledEvent.end_time > start,
        ):
            out.append(f"Instructor is already teaching '{e.name}' {window_label(e.start_time, e.end_time)}")
    return out


# -- Policy -------------------------------------------------------------------
def get_policy(db: Session) -> OvercapacityPolicy:
    row = db.get(SchedulerSetting, POLICY_KEY)
    try:
        return OvercapacityPolicy(row.value) if row else DEFAULT_POLICY
    except ValueError:
        return DEFAULT_POLICY


def set_policy(db: Session, user: CurrentUser, policy: OvercapacityPolicy) -> None:
    """Admin only (the router checks schedule:admin). Audit-logged. Does not commit."""
    before = get_policy(db)
    row = db.get(SchedulerSetting, POLICY_KEY)
    if row is None:
        row = SchedulerSetting(key=POLICY_KEY, value=policy.value)
        db.add(row)
    row.value = policy.value
    row.updated_by = user_uuid(user)
    audit(db, user, "update", POLICY_KEY, f"{before.value} -> {policy.value}")


# -- Audit --------------------------------------------------------------------
def audit(db: Session, user: CurrentUser, action: str, resource_id: str, detail: str = "") -> None:
    db.add(
        AuditLog(
            user_id=user_uuid(user),
            tenant_id=tenant_uuid(user),
            action=action,
            resource_type="schedule",
            resource_id=resource_id,
            detail=detail,
        )
    )


def my_sessions(db: Session, user: CurrentUser) -> list[ScheduledEvent]:
    """Upcoming (and today's) sessions that are yours: ones you teach, and for a Student
    the sessions of courses you are enrolled in. Drafts and cancellations are left out."""
    me = user_uuid(user)
    mine = [ScheduledEvent.instructor_id == me]
    if user.role == UserRole.student and me:
        courses = active_course_ids(db, me)
        if courses:
            mine.append(ScheduledEvent.course_id.in_(courses))
    return (
        db.query(ScheduledEvent)
        .filter(
            ScheduledEvent.tenant_id == tenant_uuid(user),
            ScheduledEvent.state.in_(HOLDING),
            ScheduledEvent.end_time > datetime.now(UTC) - timedelta(hours=12),
            or_(*mine),
        )
        .order_by(ScheduledEvent.start_time)
        .limit(50)
        .all()
    )


def release_range(db: Session, evt: ScheduledEvent) -> str | None:
    """Tear down the range this booking built, if it still holds one.

    ``auto_provisioned`` means "this booking owns a live build". It is cleared once the
    teardown is settled (dispatched, or the range is already going or gone); while the
    range is still being built it stays set, and the clock retries.
    """
    if not (evt.auto_provisioned and evt.range_id):
        return None
    # Another live booking of the same range (the next class in a series) takes the range
    # over instead: ranges cannot be built again once destroyed.
    heir = (
        db.query(ScheduledEvent)
        .filter(
            ScheduledEvent.range_id == evt.range_id,
            ScheduledEvent.id != evt.id,
            ScheduledEvent.state.in_(HOLDING),
        )
        .order_by(ScheduledEvent.start_time)
        .first()
    )
    if heir is not None:
        heir.auto_provisioned = True
        evt.auto_provisioned = False
        return f"range kept up for '{heir.name}' ({heir.id}), which now owns it"
    settled, what = range_lifecycle.destroy_for_booking(db, evt.range_id)
    if settled:
        evt.auto_provisioned = False
    return what


def withdraw_exercise(db: Session, evt: ScheduledEvent) -> str | None:
    """Cancel the exercise this booking created, if it never started. Does not commit."""
    if evt.auto_exercise and evt.exercise_id and exercise_lifecycle.withdraw_if_unstarted(db, evt.exercise_id):
        return "unstarted exercise cancelled"
    return None


def audit_system(db: Session, evt: ScheduledEvent, action: str, detail: str = "") -> None:
    """An entry for something the clock did: no user, the booking's tenant."""
    db.add(
        AuditLog(
            user_id=None,
            tenant_id=evt.tenant_id,
            action=action,
            resource_type="schedule",
            resource_id=str(evt.id),
            detail=f"[clock] {detail}" if detail else "[clock]",
        )
    )


def user_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None


def to_out(e: ScheduledEvent, warnings: list[str] | None = None) -> dict:
    return EventOut(
        id=str(e.id),
        name=e.name,
        description=e.description,
        state=e.state.value if e.state else "draft",
        tenant_id=str(e.tenant_id),
        range_id=str(e.range_id) if e.range_id else None,
        template_id=str(e.template_id) if e.template_id else None,
        instructor_id=str(e.instructor_id) if e.instructor_id else None,
        created_by=str(e.created_by) if e.created_by else None,
        course_id=str(e.course_id) if e.course_id else None,
        scenario_id=str(e.scenario_id) if e.scenario_id else None,
        exercise_id=str(e.exercise_id) if e.exercise_id else None,
        start_time=as_utc(e.start_time),
        end_time=as_utc(e.end_time),
        vm_count=e.vm_count,
        vcpu_total=e.vcpu_total,
        ram_mb_total=e.ram_mb_total,
        disk_gb_total=e.disk_gb_total,
        created_at=as_utc(e.created_at),
        updated_at=as_utc(e.updated_at),
        warnings=warnings or [],
    ).model_dump()


# -- For other sections -------------------------------------------------------
def reserving_event_count(db: Session, range_id: uuid.UUID) -> int:
    """How many events still hold this range. The caller has already tenant-checked it."""
    return (
        db.query(ScheduledEvent.id)
        .filter(ScheduledEvent.range_id == range_id, ScheduledEvent.state.in_(RESERVING_STATES))
        .count()
    )


def detach_range(db: Session, range_id: uuid.UUID) -> None:
    """Unlink a range being deleted. Finished events are history: they keep their row,
    without the range. Does not commit; the caller's transaction does."""
    db.query(ScheduledEvent).filter(ScheduledEvent.range_id == range_id).update(
        {ScheduledEvent.range_id: None}, synchronize_session=False
    )
