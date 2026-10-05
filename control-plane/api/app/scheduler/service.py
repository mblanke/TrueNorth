"""Scheduler logic, and the only way other sections reach scheduler data.

Capacity comes from a :class:`~.capacity.CapacityProvider` (ADR 0005's CapacityService
once it lands); this module decides what a booking needs and what to do when it does
not fit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, Template
from ..range_topology import template_demand
from ..tenancy import tenant_uuid
from .capacity import CapacityProvider, Committed, Resources, available, held_window, shortfalls
from .models import EventState, OvercapacityPolicy, ScheduledEvent, SchedulerSetting
from .schemas import EventOut

# States in which an event still holds its range (and its capacity claim on the range).
RESERVING_STATES = (EventState.draft, EventState.scheduled, EventState.active)

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
    row.updated_by = _user_uuid(user)
    audit(db, user, "update", POLICY_KEY, f"{before.value} -> {policy.value}")


# -- Audit --------------------------------------------------------------------
def audit(db: Session, user: CurrentUser, action: str, resource_id: str, detail: str = "") -> None:
    db.add(
        AuditLog(
            user_id=_user_uuid(user),
            tenant_id=tenant_uuid(user),
            action=action,
            resource_type="schedule",
            resource_id=resource_id,
            detail=detail,
        )
    )


def _user_uuid(user: CurrentUser) -> uuid.UUID | None:
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
        start_time=e.start_time,
        end_time=e.end_time,
        vm_count=e.vm_count,
        vcpu_total=e.vcpu_total,
        ram_mb_total=e.ram_mb_total,
        disk_gb_total=e.disk_gb_total,
        created_at=e.created_at,
        updated_at=e.updated_at,
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
