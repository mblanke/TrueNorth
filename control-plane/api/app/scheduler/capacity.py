"""The scheduler's view of cluster capacity (ADR 0004 §4; ADR 0006).

The scheduler only asks a :class:`CapacityProvider`; it never reads hypervisor tables or
computes host totals itself. :class:`ClusterCapacity` combines the capacity section's
answers (``app/capacity``: host supply under the overcommit policy, ranges running now)
with the scheduler's own bookings:

- committed = bookings holding the window, plus every running range that no booking of
  it covers in that window (a range started without a booking, or kept up between two
  sessions), from now on. A class session covers the class ranges it uses (its Students'
  ranges, or the class's shared range: ``classes.sessions.covered_ranges``), which its size
  already counts;
- ``supply_source`` says whether totals were discovered or fell back to env values.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from .lifecycle import HOLDING
from .models import ScheduledEvent

# A booking holds capacity from its provisioning lead before the start until its
# teardown grace after the end (ADR 0004, "Lead time").
PROVISION_LEAD = timedelta(minutes=int(os.getenv("SCHEDULER_PROVISION_LEAD_MIN", "30")))
TEARDOWN_GRACE = timedelta(minutes=int(os.getenv("SCHEDULER_TEARDOWN_GRACE_MIN", "15")))

# Events in these states hold capacity.
COMMITTING_STATES = HOLDING


@dataclass(frozen=True)
class Resources:
    vcpu: int = 0
    ram_mb: int = 0
    disk_gb: int = 0


@dataclass(frozen=True)
class Committed:
    resources: Resources
    events: int  # bookings
    ranges: int = 0  # running ranges no booking covers


class CapacityProvider(Protocol):
    """The seam the scheduler asks; :class:`ClusterCapacity` implements it (ADR 0006)."""

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

    def committed_series(self, db: Session, start: datetime, end: datetime, step: timedelta) -> list[Committed]:
        """``committed`` for each slot [start + i*step, start + (i+1)*step) up to ``end``,
        in one pass (the timeline and the scheduler's load bars)."""
        ...

    def committed_many(
        self, db: Session, windows: list[tuple[datetime, datetime]], exclude_id: uuid.UUID | None = None
    ) -> list[Committed]:
        """``committed`` for each window, exactly as it answers one, in one pass (the
        next free slot for a booking, ``service.next_free_slot``)."""
        ...


class ClusterCapacity:
    """Supply and running ranges from ``app/capacity``, bookings from the scheduler."""

    def __init__(self, db: Session) -> None:
        from .. import capacity

        self._capacity = capacity
        cluster = capacity.supply(db)
        self._supply = Resources(cluster.vcpu, cluster.ram_mb, cluster.disk_gb)
        self.supply_source = cluster.source
        self._running = capacity.running_ranges(db)

    def supply(self) -> Resources:
        return self._supply

    def committed(self, db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None) -> Committed:
        held = self._bookings(db, start, end, exclude_id)
        return self._sum(held, _utc(start), _utc(end), self._excluded_ranges(db, exclude_id))

    def committed_series(self, db: Session, start: datetime, end: datetime, step: timedelta) -> list[Committed]:
        held = self._bookings(db, start, end)
        out: list[Committed] = []
        t = _utc(start)
        while t < _utc(end):
            out.append(self._sum(held, t, t + step))
            t += step
        return out

    def committed_many(
        self, db: Session, windows: list[tuple[datetime, datetime]], exclude_id: uuid.UUID | None = None
    ) -> list[Committed]:
        if not windows:
            return []
        held = self._bookings(db, min(a for a, _ in windows), max(z for _, z in windows), exclude_id)
        skip = self._excluded_ranges(db, exclude_id)
        return [self._sum(held, _utc(a), _utc(z), skip) for a, z in windows]

    # -- internals ------------------------------------------------------------
    @staticmethod
    def _bookings(db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None):
        """Holding bookings whose held window [start - lead, end + grace] meets [start, end)."""
        q = db.query(ScheduledEvent).filter(
            ScheduledEvent.state.in_(COMMITTING_STATES),
            ScheduledEvent.start_time < end + PROVISION_LEAD,
            ScheduledEvent.end_time > start - TEARDOWN_GRACE,
        )
        if exclude_id:
            q = q.filter(ScheduledEvent.id != exclude_id)
        events = q.all()
        uses = _ranges_used(db, events)
        return [
            (_utc(e.start_time) - PROVISION_LEAD, _utc(e.end_time) + TEARDOWN_GRACE, e, uses.get(e.id, set()))
            for e in events
        ]

    @staticmethod
    def _excluded_ranges(db: Session, exclude_id: uuid.UUID | None) -> set[uuid.UUID]:
        """The ranges of the booking being re-checked: its own demand is what is asked."""
        if not exclude_id:
            return set()
        # tenant-safe: exclude_id is the id of a booking the router already loaded with get_owned.
        evt = db.get(ScheduledEvent, exclude_id)
        return _ranges_used(db, [evt]).get(evt.id, set()) if evt else set()

    def _sum(self, held, t0: datetime, t1: datetime, skip: set[uuid.UUID] | None = None) -> Committed:
        now = [(e, uses) for a, z, e, uses in held if a < t1 and z > t0]
        covered = set().union(*(uses for _, uses in now)) if now else set()
        vcpu = sum(e.vcpu_total for e, _ in now)
        ram = sum(e.ram_mb_total for e, _ in now)
        disk = sum(e.disk_gb_total for e, _ in now)
        ranges = 0
        if t1 > datetime.now(UTC):  # running ranges hold capacity from now on, not in the past
            for r in self._running:
                if r.range_id in covered or r.range_id in (skip or ()):
                    continue
                vcpu, ram, disk, ranges = vcpu + r.vcpu, ram + r.ram_mb, disk + r.disk_gb, ranges + 1
        return Committed(Resources(vcpu, ram, disk), len(now), ranges)


def _ranges_used(db: Session, events: list[ScheduledEvent]) -> dict[uuid.UUID, set[uuid.UUID]]:
    """booking id -> the ranges its size already counts: its own, and a class session's class ranges."""
    from ..classes import sessions

    out = sessions.covered_ranges(db, events)
    for e in events:
        if e.range_id:
            out.setdefault(e.id, set()).add(e.range_id)
    return out


def _utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; they are UTC (ADR 0004 §8)."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def get_capacity_provider(db: Session) -> CapacityProvider:
    return ClusterCapacity(db)


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
    window = window_label(start, end)
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


def window_label(start: datetime, end: datetime) -> str:
    start, end = (t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC) for t in (start, end))
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC"
    return f"{start:%Y-%m-%d %H:%M}–{end:%Y-%m-%d %H:%M} UTC"
