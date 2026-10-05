"""Scheduler logic, and the only way other sections reach scheduler data.

Capacity is still computed here from env totals. ADR 0004 slice 2 moves it behind
CapacityService (ADR 0005); until then these numbers are the same ones the old
``routers/scheduling.py`` produced.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from .models import EventState, ScheduledEvent
from .schemas import EventOut

# -- Cluster capacity (set via env; the vCenter REST API has no host capacity) --
CLUSTER_VCPU = int(os.getenv("CLUSTER_TOTAL_VCPU", "128"))  # total vCPU across all nodes
CLUSTER_RAM_MB = int(os.getenv("CLUSTER_TOTAL_RAM_MB", "524288"))  # 512 GB
CLUSTER_DISK_GB = int(os.getenv("CLUSTER_TOTAL_DISK_GB", "10240"))  # 10 TB
CLUSTER_OVERHEAD_PCT = float(os.getenv("CLUSTER_OVERHEAD_PCT", "15"))  # % reserved for hypervisor

# States in which an event still holds its range (and its capacity claim on the range).
RESERVING_STATES = (EventState.draft, EventState.scheduled, EventState.active)


def usable(total: float) -> int:
    return int(total * (1 - CLUSTER_OVERHEAD_PCT / 100))


def committed_in_window(db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None):
    """Sum resources committed by overlapping events.

    Deliberately not tenant-scoped: every tenant shares the cluster. Callers return
    totals only, never another tenant's events.
    """
    q = db.query(ScheduledEvent).filter(
        ScheduledEvent.state.in_([EventState.scheduled, EventState.active]),
        ScheduledEvent.start_time < end,
        ScheduledEvent.end_time > start,
    )
    if exclude_id:
        q = q.filter(ScheduledEvent.id != exclude_id)
    events = q.all()
    vcpu = sum(e.vcpu_total for e in events)
    ram = sum(e.ram_mb_total for e in events)
    disk = sum(e.disk_gb_total for e in events)
    return vcpu, ram, disk, len(events)


def to_out(e: ScheduledEvent) -> dict:
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
