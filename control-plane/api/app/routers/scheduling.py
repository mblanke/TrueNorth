"""TrueNorth Range - Event scheduling router with resource reservation.

Prevents over-commitment by tracking resource claims per time window.
The ``/schedule/check`` endpoint lets the UI forecast whether a given
deployment will fit within the cluster capacity at a given time.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import EventState, ScheduledEvent, Tenant

logger = logging.getLogger("truenorth.api.scheduling")

router = APIRouter(prefix="/schedule", tags=["scheduling"])

# -- Cluster capacity (configurable via env or pulled from Proxmox) ------
CLUSTER_VCPU = int(os.getenv("CLUSTER_TOTAL_VCPU", "128"))  # total vCPU across all nodes
CLUSTER_RAM_MB = int(os.getenv("CLUSTER_TOTAL_RAM_MB", "524288"))  # 512 GB
CLUSTER_DISK_GB = int(os.getenv("CLUSTER_TOTAL_DISK_GB", "10240"))  # 10 TB
CLUSTER_OVERHEAD_PCT = float(os.getenv("CLUSTER_OVERHEAD_PCT", "15"))  # % reserved for hypervisor


# -- Pydantic schemas ---------------------------------------------------
class EventIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    start_time: datetime
    end_time: datetime
    vm_count: int = Field(0, ge=0)
    vcpu_total: int = Field(0, ge=0)
    ram_mb_total: int = Field(0, ge=0)
    disk_gb_total: int = Field(0, ge=0)
    template_id: str | None = None
    range_id: str | None = None


class EventOut(BaseModel):
    id: str
    name: str
    description: str | None
    state: str
    tenant_id: str
    range_id: str | None
    template_id: str | None
    start_time: datetime
    end_time: datetime
    vm_count: int
    vcpu_total: int
    ram_mb_total: int
    disk_gb_total: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CapacityCheck(BaseModel):
    start_time: datetime
    end_time: datetime
    vcpu_needed: int = 0
    ram_mb_needed: int = 0
    disk_gb_needed: int = 0


class CapacityResult(BaseModel):
    fits: bool
    vcpu_available: int
    vcpu_committed: int
    vcpu_total: int
    ram_mb_available: int
    ram_mb_committed: int
    ram_mb_total: int
    disk_gb_available: int
    disk_gb_committed: int
    disk_gb_total: int
    overlapping_events: int
    message: str


# -- Helpers -------------------------------------------------------------
def _usable(total: float) -> int:
    return int(total * (1 - CLUSTER_OVERHEAD_PCT / 100))


def _committed_in_window(db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None):
    """Sum resources committed by overlapping events."""
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


def _to_out(e: ScheduledEvent) -> dict:
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

    vcpu_c, ram_c, disk_c, count = _committed_in_window(db, start, end)

    usable_cpu = _usable(CLUSTER_VCPU)
    usable_ram = _usable(CLUSTER_RAM_MB)
    usable_disk = _usable(CLUSTER_DISK_GB)

    return CapacityResult(
        fits=True,
        vcpu_available=max(0, usable_cpu - vcpu_c),
        vcpu_committed=vcpu_c,
        vcpu_total=usable_cpu,
        ram_mb_available=max(0, usable_ram - ram_c),
        ram_mb_committed=ram_c,
        ram_mb_total=usable_ram,
        disk_gb_available=max(0, usable_disk - disk_c),
        disk_gb_committed=disk_c,
        disk_gb_total=usable_disk,
        overlapping_events=count,
        message=f"{count} event(s) in window",
    )


@router.post("/check", response_model=CapacityResult, summary="Check if deployment fits")
def check_capacity(body: CapacityCheck, db: Session = Depends(get_db)):
    """Check whether a proposed deployment fits within the cluster at the given time."""
    vcpu_c, ram_c, disk_c, count = _committed_in_window(db, body.start_time, body.end_time)

    usable_cpu = _usable(CLUSTER_VCPU)
    usable_ram = _usable(CLUSTER_RAM_MB)
    usable_disk = _usable(CLUSTER_DISK_GB)

    avail_cpu = max(0, usable_cpu - vcpu_c)
    avail_ram = max(0, usable_ram - ram_c)
    avail_disk = max(0, usable_disk - disk_c)

    fits = body.vcpu_needed <= avail_cpu and body.ram_mb_needed <= avail_ram and body.disk_gb_needed <= avail_disk

    problems = []
    if body.vcpu_needed > avail_cpu:
        problems.append(f"vCPU: need {body.vcpu_needed}, only {avail_cpu} available")
    if body.ram_mb_needed > avail_ram:
        problems.append(f"RAM: need {body.ram_mb_needed}MB, only {avail_ram}MB available")
    if body.disk_gb_needed > avail_disk:
        problems.append(f"Disk: need {body.disk_gb_needed}GB, only {avail_disk}GB available")

    msg = "Resources available" if fits else "; ".join(problems)

    return CapacityResult(
        fits=fits,
        vcpu_available=avail_cpu,
        vcpu_committed=vcpu_c + (body.vcpu_needed if fits else 0),
        vcpu_total=usable_cpu,
        ram_mb_available=avail_ram,
        ram_mb_committed=ram_c + (body.ram_mb_needed if fits else 0),
        ram_mb_total=usable_ram,
        disk_gb_available=avail_disk,
        disk_gb_committed=disk_c + (body.disk_gb_needed if fits else 0),
        disk_gb_total=usable_disk,
        overlapping_events=count,
        message=msg,
    )


@router.get("/events", summary="List scheduled events")
def list_events(
    state: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List all scheduled events, optionally filtered by state."""
    q = db.query(ScheduledEvent).order_by(ScheduledEvent.start_time)
    if state:
        q = q.filter(ScheduledEvent.state == state)
    total = q.count()
    events = q.offset(offset).limit(limit).all()
    return {"items": [_to_out(e) for e in events], "total": total}


@router.post("/events", status_code=201, summary="Create a scheduled event")
def create_event(body: EventIn, db: Session = Depends(get_db)):
    """Create a new event with resource reservation.  Will reject if it
    would cause an over-commitment."""
    if body.end_time <= body.start_time:
        raise HTTPException(400, "end_time must be after start_time")

    # Check capacity
    vcpu_c, ram_c, disk_c, _ = _committed_in_window(db, body.start_time, body.end_time)
    usable_cpu = _usable(CLUSTER_VCPU)
    usable_ram = _usable(CLUSTER_RAM_MB)
    usable_disk = _usable(CLUSTER_DISK_GB)

    if body.vcpu_total > (usable_cpu - vcpu_c):
        raise HTTPException(409, f"Insufficient vCPU: need {body.vcpu_total}, available {usable_cpu - vcpu_c}")
    if body.ram_mb_total > (usable_ram - ram_c):
        raise HTTPException(409, f"Insufficient RAM: need {body.ram_mb_total}MB, available {usable_ram - ram_c}MB")
    if body.disk_gb_total > (usable_disk - disk_c):
        raise HTTPException(409, f"Insufficient Disk: need {body.disk_gb_total}GB, available {usable_disk - disk_c}GB")

    # Get default tenant
    tenant = db.query(Tenant).first()
    if not tenant:
        raise HTTPException(400, "No tenant configured")

    evt = ScheduledEvent(
        name=body.name,
        description=body.description,
        state=EventState.scheduled,
        tenant_id=tenant.id,
        range_id=uuid.UUID(body.range_id) if body.range_id else None,
        template_id=uuid.UUID(body.template_id) if body.template_id else None,
        start_time=body.start_time,
        end_time=body.end_time,
        vm_count=body.vm_count,
        vcpu_total=body.vcpu_total,
        ram_mb_total=body.ram_mb_total,
        disk_gb_total=body.disk_gb_total,
    )
    db.add(evt)
    db.commit()
    db.refresh(evt)
    return _to_out(evt)


@router.get("/events/{event_id}", summary="Get a scheduled event")
def get_event(event_id: str, db: Session = Depends(get_db)):
    evt = db.query(ScheduledEvent).filter(ScheduledEvent.id == uuid.UUID(event_id)).first()
    if not evt:
        raise HTTPException(404, "Event not found")
    return _to_out(evt)


@router.put("/events/{event_id}", summary="Update a scheduled event")
def update_event(event_id: str, body: EventIn, db: Session = Depends(get_db)):
    evt = db.query(ScheduledEvent).filter(ScheduledEvent.id == uuid.UUID(event_id)).first()
    if not evt:
        raise HTTPException(404, "Event not found")

    if body.end_time <= body.start_time:
        raise HTTPException(400, "end_time must be after start_time")

    # Check capacity (excluding this event)
    vcpu_c, ram_c, disk_c, _ = _committed_in_window(db, body.start_time, body.end_time, exclude_id=evt.id)
    usable_cpu = _usable(CLUSTER_VCPU)
    usable_ram = _usable(CLUSTER_RAM_MB)
    usable_disk = _usable(CLUSTER_DISK_GB)

    if body.vcpu_total > (usable_cpu - vcpu_c):
        raise HTTPException(409, "Insufficient vCPU for update")
    if body.ram_mb_total > (usable_ram - ram_c):
        raise HTTPException(409, "Insufficient RAM for update")
    if body.disk_gb_total > (usable_disk - disk_c):
        raise HTTPException(409, "Insufficient Disk for update")

    evt.name = body.name
    evt.description = body.description
    evt.start_time = body.start_time
    evt.end_time = body.end_time
    evt.vm_count = body.vm_count
    evt.vcpu_total = body.vcpu_total
    evt.ram_mb_total = body.ram_mb_total
    evt.disk_gb_total = body.disk_gb_total
    evt.range_id = uuid.UUID(body.range_id) if body.range_id else None
    evt.template_id = uuid.UUID(body.template_id) if body.template_id else None
    db.commit()
    db.refresh(evt)
    return _to_out(evt)


@router.delete("/events/{event_id}", status_code=204, response_class=Response, summary="Cancel/delete event")
def delete_event(event_id: str, db: Session = Depends(get_db)):
    evt = db.query(ScheduledEvent).filter(ScheduledEvent.id == uuid.UUID(event_id)).first()
    if not evt:
        raise HTTPException(404, "Event not found")
    db.delete(evt)
    db.commit()


@router.post("/events/{event_id}/activate", summary="Mark event as active")
def activate_event(event_id: str, db: Session = Depends(get_db)):
    evt = db.query(ScheduledEvent).filter(ScheduledEvent.id == uuid.UUID(event_id)).first()
    if not evt:
        raise HTTPException(404, "Event not found")
    evt.state = EventState.active
    db.commit()
    db.refresh(evt)
    return _to_out(evt)


@router.post("/events/{event_id}/complete", summary="Mark event as completed")
def complete_event(event_id: str, db: Session = Depends(get_db)):
    evt = db.query(ScheduledEvent).filter(ScheduledEvent.id == uuid.UUID(event_id)).first()
    if not evt:
        raise HTTPException(404, "Event not found")
    evt.state = EventState.completed
    db.commit()
    db.refresh(evt)
    return _to_out(evt)


@router.get("/timeline", summary="Resource timeline for capacity planning")
def resource_timeline(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
):
    """Return hourly resource commitment buckets for the next N days.
    Used to render the capacity timeline chart in the dashboard."""
    from datetime import timedelta

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    buckets = []
    for h in range(0, days * 24, 1):
        t_start = now + timedelta(hours=h)
        t_end = t_start + timedelta(hours=1)
        vcpu, ram, disk, count = _committed_in_window(db, t_start, t_end)
        buckets.append(
            {
                "time": t_start.isoformat(),
                "vcpu_committed": vcpu,
                "ram_mb_committed": ram,
                "disk_gb_committed": disk,
                "event_count": count,
            }
        )
    return {
        "buckets": buckets,
        "cluster_vcpu": _usable(CLUSTER_VCPU),
        "cluster_ram_mb": _usable(CLUSTER_RAM_MB),
        "cluster_disk_gb": _usable(CLUSTER_DISK_GB),
    }
