"""The scheduler's view of cluster capacity (ADR 0004 §4; ADR 0006).

The scheduler only asks a :class:`CapacityProvider`; it never reads hypervisor tables or
computes host totals itself. :class:`ClusterCapacity` combines the capacity section's
answers (``app/capacity``: host supply under the overcommit policy, ranges running now)
with the scheduler's own bookings:

- committed = bookings holding the window, plus every running range that no booking of
  it covers in that window (a range started without a booking, or kept up between two
  sessions), from now on;
- a class's ranges are held as one span per class, kind and template, from its first
  approved session's lead to the class's end (``classes.holds``): the sessions inside it and
  the ranges it builds are counted once, through it;
- ``supply_source`` says whether totals were discovered or fell back to env values.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from .lifecycle import HOLDING
from .models import ScheduledEvent

# A booking holds capacity from its provisioning lead before the start until its
# teardown grace after the end (ADR 0004, "Lead time").
PROVISION_LEAD = timedelta(minutes=int(os.getenv("SCHEDULER_PROVISION_LEAD_MIN", "30")))
TEARDOWN_GRACE = timedelta(minutes=int(os.getenv("SCHEDULER_TEARDOWN_GRACE_MIN", "15")))

logger = logging.getLogger("truenorth.api.scheduler.capacity")


def site_timezone() -> ZoneInfo:
    """The site's time zone (``SITE_TIMEZONE``, IANA; installer ``tn_site_timezone``), for
    times shown in words such as "next free at 14:00". UTC when unset or unknown."""
    name = os.getenv("SITE_TIMEZONE", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("SITE_TIMEZONE=%r is not a time zone; using UTC", name)
        return ZoneInfo("UTC")

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

    def __init__(self, db: Session, *, ends: Mapping[uuid.UUID, datetime] | None = None) -> None:
        """Running ranges known to end hold capacity only until then: a lab session's range
        until its maximum lifetime (``lab_sessions.holdings.ends``), so a site full of labs
        today does not block next week's class. Every other running range holds it from now
        on. ``ends`` replaces that map; only a forecast passes one
        (``lab_sessions.capacity_gate.next_free``: labs closing when unused)."""
        from .. import capacity
        from ..lab_sessions import holdings

        self._capacity = capacity
        cluster = capacity.supply(db)
        self._supply = Resources(cluster.vcpu, cluster.ram_mb, cluster.disk_gb)
        self.supply_source = cluster.source
        self._running = capacity.running_ranges(db)
        if ends is None:
            ends = holdings.ends(db, datetime.now(UTC))
        self._ends = {k: _utc(v) for k, v in ends.items()}

    def supply(self) -> Resources:
        return self._supply

    def committed(self, db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None) -> Committed:
        held, skip = self._held(db, start, end, exclude_id)
        return self._sum(held, _utc(start), _utc(end), skip)

    def committed_series(self, db: Session, start: datetime, end: datetime, step: timedelta) -> list[Committed]:
        held, _ = self._held(db, start, end)
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
        held, skip = self._held(db, min(a for a, _ in windows), max(z for _, z in windows), exclude_id)
        return [self._sum(held, _utc(a), _utc(z), skip) for a, z in windows]

    # -- internals ------------------------------------------------------------
    @staticmethod
    def _held(
        db: Session, start: datetime, end: datetime, exclude_id: uuid.UUID | None = None
    ) -> tuple[list[tuple[datetime, datetime, Resources, set[uuid.UUID]]], set[uuid.UUID]]:
        """(what holds capacity in [start, end), the ranges of the booking being re-checked).

        Each item is (from, to, size, ranges it already counts): a holding booking over its
        held window [start - lead, end + grace], or a class's hold over its span
        (``classes.holds``), which stands in for the class sessions inside it. A booking
        re-checked (``exclude_id``) is left out, with its class's hold: its own demand is
        what is asked."""
        from ..classes import holds as class_holds
        from ..lab_sessions import holdings

        excluded = db.get(ScheduledEvent, exclude_id) if exclude_id else None  # tenant-safe: loaded by get_owned
        key = class_holds.hold_key(db, excluded) if excluded is not None else None
        out = []
        absorbed: set[uuid.UUID] = set()
        skip: set[uuid.UUID] = {excluded.range_id} - {None} if excluded is not None else set()
        if excluded is not None:  # its Students' labs are part of the demand being asked
            skip |= holdings.drawing_on(db, [excluded.id]).get(excluded.id, (set(), None))[0]
        for h in class_holds.holds(db, start - TEARDOWN_GRACE, end + PROVISION_LEAD):
            absorbed |= h.events
            if h.key == key:
                skip |= h.ranges
                continue
            out.append((h.start, h.end, h.resources, h.ranges))
        q = db.query(ScheduledEvent).filter(
            ScheduledEvent.state.in_(COMMITTING_STATES),
            ScheduledEvent.start_time < end + PROVISION_LEAD,
            ScheduledEvent.end_time > start - TEARDOWN_GRACE,
        )
        if exclude_id:
            q = q.filter(ScheduledEvent.id != exclude_id)
        events = [e for e in q.all() if e.id not in absorbed]
        # A class session's own Students' labs draw on its hold: counted through it, not again,
        # and the booking counts as the larger of its hold and what they hold (a session
        # shrunk under its running labs does not hide them).
        labs = holdings.drawing_on(db, [e.id for e in events])
        for e in events:
            ranges, (vcpu, ram, disk) = labs.get(e.id, (set(), (0, 0, 0)))
            need = Resources(max(e.vcpu_total or 0, vcpu), max(e.ram_mb_total or 0, ram), max(e.disk_gb_total or 0, disk))
            out.append((_utc(e.start_time) - PROVISION_LEAD, _utc(e.end_time) + TEARDOWN_GRACE, need,
                        ({e.range_id} - {None}) | ranges))
        return out, skip

    def _sum(self, held, t0: datetime, t1: datetime, skip: set[uuid.UUID] | None = None) -> Committed:
        now = [(need, uses) for a, z, need, uses in held if a < t1 and z > t0]
        covered = set().union(*(uses for _, uses in now)) if now else set()
        vcpu = sum(n.vcpu for n, _ in now)
        ram = sum(n.ram_mb for n, _ in now)
        disk = sum(n.disk_gb for n, _ in now)
        ranges = 0
        if t1 > datetime.now(UTC):  # running ranges hold capacity from now on, not in the past
            for r in self._running:
                if r.range_id in covered or r.range_id in (skip or ()):
                    continue
                if r.range_id in self._ends and self._ends[r.range_id] <= t0:
                    continue
                vcpu, ram, disk, ranges = vcpu + r.vcpu, ram + r.ram_mb, disk + r.disk_gb, ranges + 1
        return Committed(Resources(vcpu, ram, disk), len(now), ranges)


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
