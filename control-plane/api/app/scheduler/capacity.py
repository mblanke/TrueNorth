"""The scheduler's view of cluster capacity (ADR 0004 §4).

The scheduler only asks a :class:`CapacityProvider`; it never reads hypervisor tables or
computes host totals itself. ADR 0005's CapacityService is the intended provider: vSphere
host totals under an overcommit policy, and running ranges with no booking counted as
committed. Until it lands, :class:`EnvCapacity` serves the numbers the old
``routers/scheduling.py`` used (env totals, bookings only), and says so in
``supply_source``. Swapping providers is :func:`get_capacity_provider`.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from .models import EventState, ScheduledEvent

# A booking holds capacity from its provisioning lead before the start until its
# teardown grace after the end (ADR 0004, "Lead time").
PROVISION_LEAD = timedelta(minutes=int(os.getenv("SCHEDULER_PROVISION_LEAD_MIN", "30")))
TEARDOWN_GRACE = timedelta(minutes=int(os.getenv("SCHEDULER_TEARDOWN_GRACE_MIN", "15")))

# Events in these states hold capacity.
COMMITTING_STATES = (EventState.scheduled, EventState.active)


@dataclass(frozen=True)
class Resources:
    vcpu: int = 0
    ram_mb: int = 0
    disk_gb: int = 0


@dataclass(frozen=True)
class Committed:
    resources: Resources
    events: int


class CapacityProvider(Protocol):
    """The seam ADR 0005's CapacityService implements."""

    supply_source: str

    def supply(self) -> Resources:
        """Usable cluster capacity, after headroom and overcommit."""
        ...

    def committed(self, db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None) -> Committed:
        """Everything holding capacity at any point in [start, end).

        Cluster-wide, not tenant-scoped: tenants share the cluster. Callers return
        totals only, never another tenant's bookings.
        """
        ...


class EnvCapacity:
    """Interim provider: supply from ``CLUSTER_TOTAL_*`` env vars, commitments from bookings only."""

    supply_source = "env"

    def __init__(self) -> None:
        self.vcpu = int(os.getenv("CLUSTER_TOTAL_VCPU", "128"))
        self.ram_mb = int(os.getenv("CLUSTER_TOTAL_RAM_MB", "524288"))  # 512 GB
        self.disk_gb = int(os.getenv("CLUSTER_TOTAL_DISK_GB", "10240"))  # 10 TB
        self.overhead_pct = float(os.getenv("CLUSTER_OVERHEAD_PCT", "15"))  # reserved for the hypervisor

    def supply(self) -> Resources:
        keep = 1 - self.overhead_pct / 100
        return Resources(int(self.vcpu * keep), int(self.ram_mb * keep), int(self.disk_gb * keep))

    def committed(self, db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None) -> Committed:
        # An event holds [start - lead, end + grace], so widen the window by the same.
        q = db.query(ScheduledEvent).filter(
            ScheduledEvent.state.in_(COMMITTING_STATES),
            ScheduledEvent.start_time < end + PROVISION_LEAD,
            ScheduledEvent.end_time > start - TEARDOWN_GRACE,
        )
        if exclude_id:
            q = q.filter(ScheduledEvent.id != exclude_id)
        events = q.all()
        return Committed(
            Resources(
                sum(e.vcpu_total for e in events),
                sum(e.ram_mb_total for e in events),
                sum(e.disk_gb_total for e in events),
            ),
            len(events),
        )


def get_capacity_provider() -> CapacityProvider:
    return EnvCapacity()


def held_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """The span a booking for [start, end) holds capacity."""
    return start - PROVISION_LEAD, end + TEARDOWN_GRACE


def available(supply: Resources, committed: Resources) -> Resources:
    return Resources(
        max(0, supply.vcpu - committed.vcpu),
        max(0, supply.ram_mb - committed.ram_mb),
        max(0, supply.disk_gb - committed.disk_gb),
    )


def shortfalls(need: Resources, free: Resources, start: datetime, end: datetime) -> list[str]:
    """One reason per resource that does not fit, e.g. "RAM: need 96 GB, 40 GB free 13:00–16:00 UTC"."""
    window = _window_label(start, end)
    out = []
    if need.vcpu > free.vcpu:
        out.append(f"vCPU: need {need.vcpu}, {free.vcpu} free {window}")
    if need.ram_mb > free.ram_mb:
        out.append(f"RAM: need {_gb(need.ram_mb)} GB, {_gb(free.ram_mb)} GB free {window}")
    if need.disk_gb > free.disk_gb:
        out.append(f"Disk: need {need.disk_gb} GB, {free.disk_gb} GB free {window}")
    return out


def _gb(mb: int) -> str:
    return f"{mb / 1024:.0f}" if mb % 1024 == 0 or mb >= 10240 else f"{mb / 1024:.1f}"


def _window_label(start: datetime, end: datetime) -> str:
    start, end = (t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC) for t in (start, end))
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC"
    return f"{start:%Y-%m-%d %H:%M}–{end:%Y-%m-%d %H:%M} UTC"
