"""What lab sessions hold of the site's capacity, for the capacity section and the scheduler
(ADR 0006; ADR 0014 §18, #288). Reads only; imports nothing from the scheduler.

- ``resetting_loads``: a lab being reset holds its size through the destroy and the rebuild,
  while its range is not built, so nobody takes its room mid-reset.
- ``ends``: when each running lab's range is gone at the latest (its maximum lifetime), or,
  for a forecast, when it would close if unused. A booking checked for next week is not
  blocked by labs that will be long gone by then.
- ``drawing_on``: the labs that draw on a booking's hold (a booked class's own Students'
  labs), counted through the booking while it holds, never twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Range, RangeState
from .models import ENDING, RESETTING, RUNS_RESOURCES, LabSession

BUILT = (RangeState.provisioning, RangeState.ready, RangeState.running)
# A lab past its end that the sweep has not closed yet still has machines.
SWEEP_LAG = timedelta(minutes=30)


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)


def resetting_loads(db: Session) -> list[tuple[uuid.UUID, int, int, int]]:
    """(range id, vCPU, RAM MB, disk GB) of each lab being reset whose range is not built now
    (destroyed for a rebuild, or waiting to be rebuilt)."""
    rows = (
        db.query(LabSession.range_id, LabSession.vcpu, LabSession.ram_mb, LabSession.disk_gb)
        .join(Range, Range.id == LabSession.range_id)
        # A built (or stopped) range is counted by the capacity section already.
        .filter(LabSession.state == RESETTING, Range.state.notin_((*BUILT, RangeState.stopped)))
        .all()
    )
    return [(rid, vcpu or 0, ram or 0, disk or 0) for rid, vcpu, ram, disk in rows]


def ends(db: Session, now: datetime, *, forecast: bool = False) -> dict[uuid.UUID, datetime]:
    """Running labs' ranges -> when each is gone. By default its maximum lifetime (never later
    than that, whatever the Student does), not before the sweep can have closed it. With
    ``forecast``: when it would close if unused (idle or maximum, the first), and labs being
    torn down now."""
    out: dict[uuid.UUID, datetime] = {}
    rows = db.query(LabSession.range_id, LabSession.state, LabSession.idle_expires_at, LabSession.max_expires_at)
    for range_id, state, idle, latest in rows.filter(
        LabSession.state.in_(RUNS_RESOURCES), LabSession.range_id.isnot(None)
    ):
        if state in ENDING:
            if forecast:
                out[range_id] = now
            continue
        if not forecast:
            if latest is not None:
                out[range_id] = max(_utc(latest), now + SWEEP_LAG)
            continue
        found = [_utc(t) for t in (idle, latest) if t is not None]
        if found:
            out[range_id] = min(found)
    return out


def drawing_on(db: Session, event_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Booking id -> ranges of the labs that draw on its hold."""
    ids = list(event_ids)
    if not ids:
        return {}
    out: dict[uuid.UUID, set[uuid.UUID]] = {}
    for booking_id, range_id in db.query(LabSession.booking_id, LabSession.range_id).filter(
        LabSession.booking_id.in_(ids), LabSession.state.in_(RUNS_RESOURCES), LabSession.range_id.isnot(None)
    ):
        out.setdefault(booking_id, set()).add(range_id)
    return out


def drawn(db: Session, booking_id: uuid.UUID, exclude: uuid.UUID | None = None) -> tuple[int, int, int]:
    """What the labs drawing on a booking hold now: (vCPU, RAM MB, disk GB)."""
    q = db.query(LabSession.vcpu, LabSession.ram_mb, LabSession.disk_gb).filter(
        LabSession.booking_id == booking_id, LabSession.state.in_(RUNS_RESOURCES)
    )
    if exclude is not None:
        q = q.filter(LabSession.id != exclude)
    rows = q.all()
    return sum(r[0] or 0 for r in rows), sum(r[1] or 0 for r in rows), sum(r[2] or 0 for r in rows)
