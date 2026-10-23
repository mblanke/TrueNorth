"""What a class's ranges hold in the cluster (ADR 0014 §10; #282 review item 1).

A class's ranges stay up from the first session that needs them to the class's end
(``ranges.py``), between sessions too. Capacity must reserve that whole span, or another
booking is approved into the evening between two sessions and the clock then builds into
an overcommitted cluster.

So a class holds capacity as **one span per (class, kind, template)**, not per session:

- *kind*: seats (individual sessions) or the shared range (collective sessions);
- *from* the provisioning lead of its first approved session of that kind and template;
- *to* the class's end (``class_end``: the end of its last day, or its last such session's
  end if that is later), plus the teardown grace;
- *size*: one seat's size times the Students on the roll call now (seats), or the
  template's size (shared).

The sessions inside a hold are not counted again (overlapping sessions count once), and
nor are the class ranges they build: running ranges the hold already reserves are not
"unbooked load" (``scheduler.capacity``). A collective session that books its own fixed
range, or one with no template, is an ordinary booking.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from ..models import Template
from ..range_topology import template_demand
from ..scheduler.capacity import PROVISION_LEAD, TEARDOWN_GRACE, Resources
from ..scheduler.lifecycle import HOLDING
from ..scheduler.models import EventState, ScheduledEvent
from .models import Class, ClassRange, ClassRangeKind, ClassSession, ClassStatus, RangeMode

# A session in these states has put (or had put) the class's ranges up: approved, or over.
NEEDS_RANGES = (*HOLDING, EventState.completed)


@dataclass
class Hold:
    key: tuple[uuid.UUID, str, uuid.UUID]  # (class, kind, template)
    start: datetime
    end: datetime
    resources: Resources
    ranges: set[uuid.UUID] = field(default_factory=set)
    events: set[uuid.UUID] = field(default_factory=set)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def last_day_end(end_date: date) -> datetime:
    """The end of a class's last day (UTC dates; there is no site time zone yet)."""
    return datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)


def kind_of(mode: str) -> str:
    return ClassRangeKind.seat.value if mode == RangeMode.individual.value else ClassRangeKind.shared.value


def holds(db: Session, start: datetime, end: datetime) -> list[Hold]:
    """Every class hold that meets [start, end). Cluster-wide, like the bookings it replaces."""
    from .sessions import active_seats

    start, end = _aware(start), _aware(end)
    since = (start - TEARDOWN_GRACE - timedelta(days=1)).date()
    # tenant-safe: the capacity view is cluster-wide; it returns totals only, never rows.
    rows = (
        db.query(ScheduledEvent, ClassSession, Class)
        .join(ClassSession, ClassSession.event_id == ScheduledEvent.id)
        .join(Class, Class.id == ClassSession.class_id)
        .filter(
            Class.status == ClassStatus.open.value,
            Class.tenant_id == ScheduledEvent.tenant_id,
            Class.end_date >= since,
            ScheduledEvent.state.in_(NEEDS_RANGES),
            ScheduledEvent.template_id.isnot(None),
        )
        .all()
    )
    groups: dict[tuple, list] = {}
    classes: dict[uuid.UUID, Class] = {}
    for evt, link, cls in rows:
        if not _uses_class_ranges(db, cls, link, evt):
            continue
        classes[cls.id] = cls
        groups.setdefault((cls.id, kind_of(link.range_mode), evt.template_id), []).append(evt)
    out: list[Hold] = []
    sizes: dict[uuid.UUID, Resources | None] = {}
    for key, events in groups.items():
        cls_id, kind, template_id = key
        cls = classes[cls_id]
        approved = [e for e in events if e.state in HOLDING]
        first = min(_aware(e.start_time) for e in (approved or events)) - PROVISION_LEAD
        last = max([last_day_end(cls.end_date), *(_aware(e.end_time) for e in events)]) + TEARDOWN_GRACE
        if not (first < end and last > start):
            continue
        if template_id not in sizes:
            sizes[template_id] = _template_size(db, template_id)
        one = sizes[template_id]
        if one is None:
            continue
        n = active_seats(db, cls) if kind == ClassRangeKind.seat.value else 1
        out.append(Hold(key, first, last, Resources(one.vcpu * n, one.ram_mb * n, one.disk_gb * n),
                        _live_ranges(db, cls, kind, template_id), {e.id for e in events}))
    return out


def hold_key(db: Session, evt: ScheduledEvent) -> tuple[uuid.UUID, str, uuid.UUID] | None:
    """The hold a class session belongs to (or will, once approved); None for any other booking."""
    link = db.query(ClassSession).filter(ClassSession.event_id == evt.id).first()
    if link is None or evt.template_id is None:
        return None
    cls = db.get(Class, link.class_id)  # tenant-safe: the link's own class, checked below
    if cls is None or cls.tenant_id != evt.tenant_id or not _uses_class_ranges(db, cls, link, evt):
        return None
    return cls.id, kind_of(link.range_mode), evt.template_id


def class_end(db: Session, evt: ScheduledEvent) -> datetime | None:
    """When the ranges a class session uses come down at the latest: its class's end. None
    for a booking that uses no class ranges."""
    key = hold_key(db, evt)
    if key is None:
        return None
    cls = db.get(Class, key[0])  # tenant-safe: the session's own class (hold_key checked it)
    return max(last_day_end(cls.end_date), _aware(evt.end_time))


def _uses_class_ranges(db: Session, cls: Class, link: ClassSession, evt: ScheduledEvent) -> bool:
    """Individual sessions always; collective ones unless they book a range of their own
    (a fixed range or an exercise's) rather than the class's shared one."""
    if link.range_mode == RangeMode.individual.value:
        return True
    if evt.range_id is None:
        return True
    return (
        db.query(ClassRange.id)
        .filter(ClassRange.class_id == cls.id, ClassRange.range_id == evt.range_id)
        .first()
        is not None
    )


def _template_size(db: Session, template_id: uuid.UUID) -> Resources | None:
    yaml = db.query(Template.yaml).filter(Template.id == template_id).scalar()
    d = template_demand(yaml) if yaml else None
    return Resources(d.vcpu, d.ram_mb, d.disk_gb) if d else None


def _live_ranges(db: Session, cls: Class, kind: str, template_id: uuid.UUID) -> set[uuid.UUID]:
    return {
        rid
        for (rid,) in db.query(ClassRange.range_id).filter(
            ClassRange.class_id == cls.id, ClassRange.tenant_id == cls.tenant_id, ClassRange.kind == kind,
            ClassRange.template_id == template_id, ClassRange.range_id.isnot(None), ClassRange.torn_down_at.is_(None),
        )
    }
