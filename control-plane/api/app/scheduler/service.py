"""Scheduler logic, and the only way other sections reach scheduler data.

Capacity comes from a :class:`~.capacity.CapacityProvider` (CapacityService (ADR 0006)
once it lands); this module decides what a booking needs and what to do when it does
not fit.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import case, func, or_, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, object_session

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
    get_capacity_provider,
    held_window,
    shortfalls,
    window_label,
)
from .lifecycle import HOLDING
from .models import BookingApproval, EventState, OvercapacityPolicy, ScheduledEvent, SchedulerSetting
from .schemas import ApprovalOut, EventOut, as_utc

# States in which an event still holds its range, for range deletion. A draft counts:
# deleting the range would silently break a booking someone is still preparing. So does
# one awaiting approval.
RESERVING_STATES = (EventState.draft, EventState.pending_approval, *HOLDING)

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


# -- Fit and the next free slot ----------------------------------------------
SLOT_STEP = timedelta(minutes=15)
NEXT_FREE_DAYS = int(os.getenv("SCHEDULER_NEXT_FREE_DAYS", "14"))


@dataclass(frozen=True)
class Fit:
    """Whether a booking fits a window: no double booking, and room in the cluster."""

    conflicts: list[str]
    assessment: Assessment

    @property
    def ok(self) -> bool:
        return not self.conflicts and self.assessment.fits

    @property
    def reasons(self) -> list[str]:
        return [*self.conflicts, *self.assessment.reasons]


def need_of(evt: ScheduledEvent) -> Resources:
    return Resources(evt.vcpu_total or 0, evt.ram_mb_total or 0, evt.disk_gb_total or 0)


def fit(db: Session, provider: CapacityProvider, evt: ScheduledEvent, start: datetime, end: datetime) -> Fit:
    """Would ``evt`` fit at [start, end)? The same checks a booking gets (``_admit`` in the
    router): conflicts of its range and instructor, and capacity over its held window."""
    clash = conflicts(
        db, start=start, end=end, range_id=evt.range_id, instructor_id=evt.instructor_id, exclude_id=evt.id
    )
    return Fit(clash, assess(db, provider, start, until_class_end(db, evt, end), need_of(evt), exclude_id=evt.id))


def until_class_end(db: Session, evt: ScheduledEvent, end: datetime) -> datetime:
    """How long a booking holds what it needs: to its end, or, for a class session that uses
    its class's ranges, to the class's end (they stay up between sessions; ``classes.holds``)."""
    from ..classes import holds

    last = holds.class_end(db, evt)
    return max(as_utc(end), last) if last is not None else end


def _ceil(t: datetime, step: timedelta) -> datetime:
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    n = -((epoch - t) // step)  # ceiling division
    return epoch + n * step


def next_free_slot(
    db: Session,
    provider: CapacityProvider,
    evt: ScheduledEvent,
    *,
    after: datetime | None = None,
    now: datetime | None = None,
    within: tuple[date, date] | None = None,
) -> tuple[tuple[datetime, datetime] | None, datetime]:
    """The first window of ``evt``'s length, from ``after`` (default: its own start) on a
    15-minute grid, that it fits: no conflict and room for its demand (``fit``), and no
    maintenance window over its build-to-end span. Never sooner than the provisioning lead
    from now, since the range must be built first. ``within``: the class's first and last
    day (UTC dates; there is no site time zone yet): a slot must lie inside them.

    Returns (slot or None, searched until). Bookings and running ranges are read once;
    the slot found is then checked again with ``fit`` so it never disagrees with what
    approving at that time would say."""
    now = now or datetime.now(UTC)
    start, end = as_utc(evt.start_time), as_utc(evt.end_time)
    length = end - start
    first = _ceil(max(as_utc(after) if after else start, now + PROVISION_LEAD), SLOT_STEP)
    horizon = first + timedelta(days=NEXT_FREE_DAYS)
    if within is not None:
        first = max(first, datetime.combine(within[0], time.min, tzinfo=UTC))
        horizon = min(horizon, datetime.combine(within[1] + timedelta(days=1), time.min, tzinfo=UTC) - length)
    candidates: list[datetime] = []
    t = first
    while t <= horizon:
        candidates.append(t)
        t += SLOT_STEP
    need = need_of(evt)
    supply = provider.supply()
    committed = provider.committed_many(db, [held_window(c, c + length) for c in candidates], evt.id)
    blocked = _blocked_spans(db, evt, first, horizon + length)
    windows = maintenance_spans(db, evt, first - PROVISION_LEAD, horizon + length)
    for c, com in zip(candidates, committed, strict=True):
        if shortfalls(need, available(supply, com.resources), c, c + length):
            continue
        if any(a < c + length and z > c for a, z in blocked):
            continue
        if any(a < c + length and z > c - PROVISION_LEAD for a, z in windows):
            continue
        if fit(db, provider, evt, c, c + length).ok:
            return (c, c + length), horizon
    return None, horizon


def maintenance_spans(db: Session, evt: ScheduledEvent, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Maintenance windows over [start, end) that would hold ``evt``'s range (ADR 0013 §7)."""
    from ..maintenance_windows import service as windows

    return windows.booking_spans(
        db, tenant_id=evt.tenant_id, range_id=evt.range_id, template_id=evt.template_id, start=start, end=end
    )


def _blocked_spans(db: Session, evt: ScheduledEvent, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Spans a start must not fall in for ``evt``'s session to overlap: its range's other
    bookings (widened by lead and grace) and its instructor's (``conflicts``)."""
    reach = PROVISION_LEAD + TEARDOWN_GRACE
    out: list[tuple[datetime, datetime]] = []

    def others(*crit):
        return (
            db.query(ScheduledEvent)
            .filter(ScheduledEvent.state.in_(HOLDING), ScheduledEvent.id != evt.id, *crit)
            .all()
        )

    if evt.range_id:
        for o in others(
            ScheduledEvent.range_id == evt.range_id,
            ScheduledEvent.start_time < end + reach,
            ScheduledEvent.end_time > start - reach,
        ):
            out.append((as_utc(o.start_time) - reach, as_utc(o.end_time) + reach))
    if evt.instructor_id:
        for o in others(
            ScheduledEvent.instructor_id == evt.instructor_id,
            ScheduledEvent.start_time < end,
            ScheduledEvent.end_time > start,
        ):
            out.append((as_utc(o.start_time), as_utc(o.end_time)))
    return out


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


def resolve_range(
    db: Session, user: CurrentUser, range_id: str | None, keep: uuid.UUID | None = None
) -> uuid.UUID | None:
    """A booked range must be the caller's tenant's (404 otherwise, as for any range),
    must still be buildable (a destroyed range cannot be provisioned again), and must not
    be a lab session's (only its session drives it) or a class's (only its class does;
    ``keep``: the one a class session already uses)."""
    if not range_id:
        return None
    rng = get_owned(db, Range, range_id, user, not_found="Range not found")
    if rng.state in (RangeState.destroying, RangeState.destroyed):
        raise HTTPException(409, f"Range is {rng.state.value} and cannot be built again; book another range")
    if range_lifecycle.is_lab_range(db, rng.id):
        # The clock would build and tear it down under a student's session.
        raise HTTPException(409, "This range belongs to a student's lab session; book another range")
    if rng.id != keep and class_owns_range(db, rng.id):
        # A class builds, keeps and tears down its own ranges (classes/ranges.py).
        raise HTTPException(409, "This range belongs to a class's Students; book a session of that class instead")
    return rng.id


def class_owns_range(db: Session, range_id: uuid.UUID) -> bool:
    from ..classes import ranges as class_ranges

    return class_ranges.is_class_range(db, range_id)


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


def student_sessions(db: Session, user_id: uuid.UUID):
    """A Student's own sessions (a filter): a class session when they are on its roll call
    (ADR 0014: the roll call replaces enrolment), any other booking of a course they are
    enrolled in."""
    from ..classes import sessions

    courses = active_course_ids(db, user_id)
    plain = ScheduledEvent.course_id.in_(courses or [None]) & ScheduledEvent.id.notin_(sessions.all_session_ids())
    return or_(plain, ScheduledEvent.id.in_(sessions.session_ids_on_roll_call(user_id)))


def my_sessions(db: Session, user: CurrentUser) -> list[ScheduledEvent]:
    """Upcoming (and today's) sessions that are yours: ones you teach, and for a Student
    their own (``student_sessions``). Drafts, sessions awaiting approval and cancellations
    are left out."""
    me = user_uuid(user)
    mine = [ScheduledEvent.instructor_id == me]
    if user.role == UserRole.student and me:
        mine.append(student_sessions(db, me))
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

    A booking that ended (``completed``) is the range's lease expiring: its teardown runs
    inside a maintenance window too (ADR 0013 §7). A cancelled one's is a normal destroy:
    a window defers it, and the clock retries once the window is over.

    ``auto_provisioned`` means "this booking owns a live build". It is cleared once the
    teardown is settled (dispatched, or the range is already going or gone); while the
    range is still being built it stays set, and the clock retries.
    """
    if not (evt.auto_provisioned and evt.range_id):
        return None
    if class_owns_range(db, evt.range_id):
        # A class's range outlives its sessions (ADR 0014 §10): the class tears it down after
        # its end, or when it is cancelled (classes/ranges.py).
        evt.auto_provisioned = False
        return "range kept by its class until the class ends"
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
    from ..maintenance_windows.policy import BYPASS_BOOKING_ENDED

    bypass = BYPASS_BOOKING_ENDED if evt.state == EventState.completed else None
    settled, what = range_lifecycle.destroy_for_booking(db, evt.range_id, bypass)
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


def maintenance_warnings(db: Session, evt: ScheduledEvent) -> list[str]:
    """Maintenance windows over the booking's build-to-end span (app/maintenance_windows)."""
    from ..maintenance_windows import service as windows

    return windows.booking_warnings(
        db, tenant_id=evt.tenant_id, range_id=evt.range_id, template_id=evt.template_id,
        build_from=as_utc(evt.start_time) - PROVISION_LEAD, end=as_utc(evt.end_time),
    )


def approval_of(db: Session, event_id: uuid.UUID) -> BookingApproval | None:
    """The booking's approval row: it needs an approver (a class session) when there is one."""
    # tenant-safe: keyed by a booking the caller already loaded tenant-scoped.
    return db.get(BookingApproval, event_id)


def needs_approval(db: Session, evt: ScheduledEvent) -> bool:
    return approval_of(db, evt.id) is not None


def _approval_out(a: BookingApproval | None) -> ApprovalOut | None:
    if a is None:
        return None

    def sid(v):
        return str(v) if v else None

    def t(v):
        return as_utc(v) if v else None

    return ApprovalOut(
        requested_by=sid(a.requested_by), requested_at=t(a.requested_at),
        proposed_start_time=t(a.proposed_start), proposed_end_time=t(a.proposed_end),
        proposed_by=sid(a.proposed_by), proposed_at=t(a.proposed_at),
        decided_by=sid(a.decided_by), decided_at=t(a.decided_at), reason=a.reason,
    )


def outs(db: Session, events: list[ScheduledEvent]) -> list[dict]:
    """Bookings as the API returns them, with their class and approval, in two queries."""
    ids = [e.id for e in events]
    if not ids:
        return []
    from ..classes import sessions

    # tenant-safe: rows keyed by bookings the caller already loaded tenant-scoped.
    approvals = {a.event_id: a for a in db.query(BookingApproval).filter(BookingApproval.event_id.in_(ids))}
    links = sessions.links_for(db, ids)
    return [to_out(e, approval=approvals.get(e.id), link=links.get(e.id)) for e in events]


def out(db: Session, e: ScheduledEvent, warnings: list[str] | None = None, maintenance: list[str] | None = None) -> dict:
    from ..classes import sessions

    return to_out(e, warnings, maintenance, approval=approval_of(db, e.id), link=sessions.links_for(db, [e.id]).get(e.id))


def to_out(
    e: ScheduledEvent,
    warnings: list[str] | None = None,
    maintenance: list[str] | None = None,
    *,
    approval: BookingApproval | None = None,
    link: tuple[uuid.UUID, str] | None = None,
) -> dict:
    class_id, range_mode = link or (None, None)
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
        require_certified_range=bool(e.require_certified_range),
        start_time=as_utc(e.start_time),
        end_time=as_utc(e.end_time),
        vm_count=e.vm_count,
        vcpu_total=e.vcpu_total,
        ram_mb_total=e.ram_mb_total,
        disk_gb_total=e.disk_gb_total,
        created_at=as_utc(e.created_at),
        updated_at=as_utc(e.updated_at),
        warnings=warnings or [],
        maintenance_warnings=maintenance or [],
        class_id=str(class_id) if class_id else None,
        range_mode=range_mode,
        approval=_approval_out(approval),
    ).model_dump()


# -- For other sections -------------------------------------------------------
# What a booking can point at that its owner may delete. Every one of these foreign
# keys is nullable: a booking in a reserving state blocks the delete (409 at the
# caller), a finished one keeps its row without the link.
_LINKS = {
    "range": ScheduledEvent.range_id,
    "template": ScheduledEvent.template_id,
    "course": ScheduledEvent.course_id,
    "scenario": ScheduledEvent.scenario_id,
}


def reserving_events_for(db: Session, link: str, parent_id: uuid.UUID) -> int:
    """How many draft/scheduled/provisioning/active events still use this range,
    template, course or scenario. The caller has already tenant-checked the parent."""
    col = _LINKS[link]
    return db.query(ScheduledEvent.id).filter(col == parent_id, ScheduledEvent.state.in_(RESERVING_STATES)).count()


def events_for(db: Session, link: str, parent_id: uuid.UUID) -> int:
    """How many events, in any state, point at this range, template, course or scenario."""
    return db.query(ScheduledEvent.id).filter(_LINKS[link] == parent_id).count()


def detach(db: Session, link: str, parent_id: uuid.UUID) -> None:
    """Unlink a range, template, course or scenario being deleted. Finished events are
    history: they keep their row, without the link. Does not commit."""
    col = _LINKS[link]
    db.query(ScheduledEvent).filter(col == parent_id).update({col: None}, synchronize_session=False)


def reserving_event_count(db: Session, range_id: uuid.UUID) -> int:
    """How many events still hold this range. The caller has already tenant-checked it."""
    return reserving_events_for(db, "range", range_id)


def detach_range(db: Session, range_id: uuid.UUID) -> None:
    """Unlink a range being deleted (see :func:`detach`)."""
    detach(db, "range", range_id)


# -- For the classes section (ADR 0014 §17: roll-call changes after approval) ----------
def approved_upcoming(db: Session, event_ids: list[uuid.UUID], now: datetime | None = None) -> list[ScheduledEvent]:
    """Of these bookings, the approved ones that still hold capacity and have not ended."""
    if not event_ids:
        return []
    now = now or datetime.now(UTC)
    # tenant-safe: ids of one class's sessions, all in the class's tenant (sessions.link).
    return (
        db.query(ScheduledEvent)
        .filter(ScheduledEvent.id.in_(event_ids), ScheduledEvent.state.in_(HOLDING), ScheduledEvent.end_time > now)
        .order_by(ScheduledEvent.start_time)
        .all()
    )


@dataclass(frozen=True)
class ApprovedWindow:
    """An approved booking's times, and when an approver let it through (None: no approval
    row, or no decision time recorded)."""

    start: datetime
    end: datetime
    approved_at: datetime | None


def approved_windows(db: Session, event_ids: list[uuid.UUID]) -> list[ApprovedWindow]:
    """These bookings that an approver let through, earliest first: approved and still
    holding, or already over. Those awaiting approval, rejected or cancelled never count
    (ADR 0014 §4: a class's seat-forfeit clock)."""
    if not event_ids:
        return []
    # tenant-safe: ids of one class's sessions, all in the class's tenant (sessions.link).
    rows = (
        db.query(ScheduledEvent.start_time, ScheduledEvent.end_time, BookingApproval.decided_at)
        .outerjoin(BookingApproval, BookingApproval.event_id == ScheduledEvent.id)
        .filter(ScheduledEvent.id.in_(event_ids), ScheduledEvent.state.in_((*HOLDING, EventState.completed)))
        .order_by(ScheduledEvent.start_time, ScheduledEvent.id)
        .all()
    )
    return [ApprovedWindow(as_utc(s), as_utc(e), as_utc(d) if d is not None else None) for s, e, d in rows]


def room_for(db: Session, evt: ScheduledEvent, extra: Resources) -> list[str]:
    """Why ``extra`` more demand on the already-held ``evt`` would not fit (none: it fits).
    The booking itself is counted at its current size, so this asks for the growth only.
    The caller holds ``serialize_bookings``."""
    if extra == Resources():
        return []
    end = until_class_end(db, evt, as_utc(evt.end_time))
    a = assess(db, get_capacity_provider(db), max(as_utc(evt.start_time), datetime.now(UTC)), end, extra)
    return [f"'{evt.name}': {r}" for r in a.reasons]


def _floored(column, change: int):
    """``column + change``, never below zero, computed by the database."""
    total = func.coalesce(column, 0) + change
    return case((total < 0, 0), else_=total)


def resize(db: Session, evt: ScheduledEvent, delta: Resources, sign: int, vms: int = 0) -> None:
    """Grow (sign 1) or shrink (sign -1) a held booking's demand, never below zero. Does not commit.

    An increment in SQL, not a read-modify-write: the forfeit clock (on every replica) and a
    roll-call change can resize the same session at once, and each change must land."""
    if delta == Resources() and not vms:
        return
    # tenant-safe: a booking the caller already loaded tenant-scoped (a class's own session).
    db.query(ScheduledEvent).filter(ScheduledEvent.id == evt.id).update(
        {
            ScheduledEvent.vcpu_total: _floored(ScheduledEvent.vcpu_total, sign * delta.vcpu),
            ScheduledEvent.ram_mb_total: _floored(ScheduledEvent.ram_mb_total, sign * delta.ram_mb),
            ScheduledEvent.disk_gb_total: _floored(ScheduledEvent.disk_gb_total, sign * delta.disk_gb),
            ScheduledEvent.vm_count: _floored(ScheduledEvent.vm_count, sign * vms),
        },
        synchronize_session=False,
    )
    db.refresh(evt, ["vcpu_total", "ram_mb_total", "disk_gb_total", "vm_count"])


def set_size(db: Session, evt: ScheduledEvent, size: Resources, vms: int) -> None:
    """Set a booking's demand outright: an individual class session sized from its roll call.
    Callers hold ``serialize_bookings``, so the roll call they counted cannot change before
    they commit. Does not commit."""
    evt.vcpu_total, evt.ram_mb_total, evt.disk_gb_total, evt.vm_count = size.vcpu, size.ram_mb, size.disk_gb, vms


def holding_sessions(db: Session, event_ids, *, now: datetime, lead: bool) -> list[ScheduledEvent]:
    """Of ``event_ids`` (a subquery of class sessions), the approved ones that have not ended
    and, with ``lead``, whose provisioning lead has been reached: whose ranges must be up."""
    # tenant-safe: the class clock's system-wide pass; each session is handled in its own tenant.
    q = db.query(ScheduledEvent).filter(
        ScheduledEvent.id.in_(event_ids), ScheduledEvent.state.in_(HOLDING), ScheduledEvent.end_time > now
    )
    if lead:
        q = q.filter(ScheduledEvent.start_time <= now + PROVISION_LEAD)
    return q.order_by(ScheduledEvent.start_time).limit(500).all()


def attach_range(db: Session, event_id: uuid.UUID, range_id: uuid.UUID) -> bool:
    """Point a collective class session that has no range yet at its class's shared range.
    A guarded update: an edit or another replica that set one first wins. Does not commit."""
    # tenant-safe: a class session's booking and its class's own range, both the class's tenant's.
    return (
        db.query(ScheduledEvent)
        .filter(ScheduledEvent.id == event_id, ScheduledEvent.range_id.is_(None), ScheduledEvent.state.in_(HOLDING))
        .update({ScheduledEvent.range_id: range_id}, synchronize_session=False)
    ) == 1


def reassign_instructor(
    db: Session, user: CurrentUser, event_ids: list[uuid.UUID], keep: set[uuid.UUID], new: uuid.UUID
) -> list:
    """Pass these bookings that have not finished, and whose instructor is not in ``keep``
    (a class's teachers), to ``new`` (its lead). Audited. Returns the invites, for sessions
    already sent: the new instructor's REQUEST and the old one's CANCEL.

    The new instructor must be free then (``conflicts``). When they are not, an approved
    session that has not started goes back to awaiting approval: it stops holding anything,
    its invites are withdrawn, and an approver decides again. One already under way stays
    with them, and the clash is audited. Does not commit."""
    from . import approval, invites, lifecycle

    if not event_ids:
        return []
    live = (EventState.draft, EventState.pending_approval, *HOLDING)
    out: list = []
    # tenant-safe: ids of one class's sessions, all in the class's tenant (sessions.link).
    rows = (
        db.query(ScheduledEvent)
        .filter(
            ScheduledEvent.id.in_(event_ids),
            ScheduledEvent.instructor_id.isnot(None),
            ScheduledEvent.instructor_id.notin_(keep),
            ScheduledEvent.state.in_(live),
        )
        .order_by(ScheduledEvent.start_time)
        .all()
    )
    for evt in rows:
        old = evt.instructor_id
        evt.instructor_id = new
        db.flush()
        audit(db, user, "update", str(evt.id), f"instructor {old} -> {new}: {old} no longer teaches the class")
        clash = (
            conflicts(db, start=evt.start_time, end=evt.end_time, range_id=None, instructor_id=new, exclude_id=evt.id)
            if evt.state in HOLDING
            else []
        )
        if clash and evt.state == EventState.scheduled and approval_of(db, evt.id) is not None:
            resequence(evt)  # the CANCEL must be newer than the REQUEST they hold
            out += invites.plan(db, evt, "CANCEL", previous_instructor=old)
            approval.back_to_approval(db, user, evt, "; ".join(clash))
            continue
        if clash:
            audit(db, user, "instructor_conflict", str(evt.id), "; ".join(clash))
        if evt.state not in lifecycle.UNSENT:
            resequence(evt)
            out += invites.plan(db, evt, "REQUEST", previous_instructor=old)
    return out


def resequence(evt: ScheduledEvent) -> None:
    """Raise iCalendar SEQUENCE before a per-attendee REQUEST or CANCEL: a Student taken off
    and put back must get a REQUEST newer than their CANCEL (RFC 5546 §2.1.5). Does not commit.

    An increment in SQL for a stored booking, so two transactions raising it at once (the
    forfeit clock and a roll-call change) give two numbers, never the same one twice."""
    db = object_session(evt)
    state = sa_inspect(evt)
    if db is None or state.transient or state.pending:
        evt.sequence = (evt.sequence or 0) + 1
        return
    # tenant-safe: a booking the caller already loaded tenant-scoped.
    db.query(ScheduledEvent).filter(ScheduledEvent.id == evt.id).update(
        {ScheduledEvent.sequence: func.coalesce(ScheduledEvent.sequence, 0) + 1}, synchronize_session=False
    )
    db.refresh(evt, ["sequence"])


def attendee_invites(db: Session, events: list[ScheduledEvent], emails: list[str], method: str) -> list:
    """Invites (REQUEST) or withdrawals (CANCEL) of these sessions for Students joining or
    leaving the roll call after approval. Built now, sent by the caller after commit."""
    from . import invites

    return [invites.build(evt, method, to, attending=True) for evt in events for to in emails]


def cancel_unstarted(db: Session, user: CurrentUser, event_ids: list[uuid.UUID]) -> list:
    """Cancel these bookings that have not started being built (draft, awaiting approval,
    scheduled): a cancelled class's sessions. Returns the CANCEL invites for those already
    sent, for the caller to send after commit. Sessions under way are left to their
    instructor. Does not commit."""
    from . import invites, lifecycle

    if not event_ids:
        return []
    out: list = []
    unstarted = (EventState.draft, EventState.pending_approval, EventState.scheduled)
    # tenant-safe: ids of one class's sessions, all in the class's tenant (sessions.link).
    for evt in db.query(ScheduledEvent).filter(ScheduledEvent.id.in_(event_ids), ScheduledEvent.state.in_(unstarted)):
        before = evt.state
        if not lifecycle.transition(db, evt, EventState.cancelled):
            continue
        resequence(evt)
        detail = f"{before.value} -> cancelled: its class was cancelled"
        if what := release_range(db, evt):
            detail += f"; {what}"
        audit(db, user, "transition", str(evt.id), detail)
        if before not in lifecycle.UNSENT:
            db.refresh(evt, ["state"])
            out += invites.plan(db, evt, "CANCEL")
    return out


async def send_invites(items: list) -> None:
    from . import invites

    await invites.send(items)
