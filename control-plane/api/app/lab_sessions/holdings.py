"""What lab sessions hold of the site's capacity, for the capacity section and the scheduler
(ADR 0006; ADR 0014 §18, #288). Reads only; imports nothing from the scheduler.

- ``resetting_loads``: a lab being reset holds its size through the destroy and the rebuild,
  while its range is not built, so nobody takes its room mid-reset.
- ``ends``: when each running lab's range is gone at the latest (its maximum lifetime), or,
  for a forecast, when it would close if unused. A booking checked for next week is not
  blocked by labs that will be long gone by then.
- ``drawing_on``: the labs that draw on a booking's hold (a class session's own Students'
  labs, while they are on its roll call), counted through the booking while it holds, never
  twice; the booking counts as the larger of its hold and what they hold.
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


def _drawing(db: Session):
    """Labs drawing on a booking's hold now: live, linked to the booking, and their Student
    still on the roll call of the class the booking is a session of. A Student taken off the
    roll call stops drawing at once (their lab is self-serve from then on); nothing is
    written to the lab for it."""
    from ..classes.models import OFF_ROLL_CALL, ClassSession, RollCallEntry

    on_roll_call = (
        db.query(RollCallEntry.id)
        .join(ClassSession, ClassSession.class_id == RollCallEntry.class_id)
        .filter(
            ClassSession.event_id == LabSession.booking_id,
            RollCallEntry.tenant_id == LabSession.tenant_id,
            RollCallEntry.user_id == LabSession.user_id,
            RollCallEntry.status.notin_(OFF_ROLL_CALL),
        )
        .exists()
    )
    return db.query(LabSession).filter(
        LabSession.booking_id.isnot(None), LabSession.state.in_(RUNS_RESOURCES), on_roll_call
    )


def drawing_on(db: Session, event_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[set[uuid.UUID], tuple[int, int, int]]]:
    """Booking id -> (ranges of the labs drawing on its hold, what they hold: vCPU, RAM MB,
    disk GB)."""
    ids = list(event_ids)
    if not ids:
        return {}
    out: dict[uuid.UUID, tuple[set[uuid.UUID], tuple[int, int, int]]] = {}
    rows = _drawing(db).filter(LabSession.booking_id.in_(ids)).with_entities(
        LabSession.booking_id, LabSession.range_id, LabSession.vcpu, LabSession.ram_mb, LabSession.disk_gb
    )
    for booking_id, range_id, vcpu, ram, disk in rows:
        ranges, (v, r, d) = out.get(booking_id, (set(), (0, 0, 0)))
        if range_id is not None:
            ranges.add(range_id)
        out[booking_id] = (ranges, (v + (vcpu or 0), r + (ram or 0), d + (disk or 0)))
    return out


def is_drawing(db: Session, session_id: uuid.UUID) -> bool:
    """Whether this lab draws on its booking's hold now (live, Student on the roll call)."""
    return db.query(_drawing(db).filter(LabSession.id == session_id).exists()).scalar()


def drawn(db: Session, booking_id: uuid.UUID, exclude: uuid.UUID | None = None) -> tuple[int, int, int]:
    """What the labs drawing on a booking hold now: (vCPU, RAM MB, disk GB)."""
    q = _drawing(db).filter(LabSession.booking_id == booking_id)
    if exclude is not None:
        q = q.filter(LabSession.id != exclude)
    rows = q.with_entities(LabSession.vcpu, LabSession.ram_mb, LabSession.disk_gb).all()
    return sum(r[0] or 0 for r in rows), sum(r[1] or 0 for r in rows), sum(r[2] or 0 for r in rows)
