"""Self-serve labs use only the capacity left after approved classes (ADR 0014 §18, #288).

A lab started through **Start lab** has no booking. Before its machines are built it asks
the scheduler's capacity provider (``scheduler.capacity``, ADR 0006) what is committed at
every 15-minute slice from now to the lab's maximum lifetime: approved bookings over their
held windows, approved classes over their whole span (``classes.holds``, judged at peak as
#299 does), and every range running now (other labs included). A booking awaiting approval
holds nothing (``scheduler.lifecycle.HOLDING``), so it never refuses a lab. If the lab does
not fit in every slice, it is refused with "Labs are busy, next free at HH:MM" in the site's
time zone (``SITE_TIMEZONE``, an IANA name; UTC by default).

The check and the lab's range are written in one transaction under the scheduler's booking
lock (``scheduler.service.serialize_bookings``), so two Students starting the last slot at
once, in any tenants, and a booking being approved at that moment, are decided one after
the other. Capacity is site-wide: the message carries a time only, never what holds it.

The next free time is a forecast: running labs are counted until they would close if
unused (their idle or maximum expiry, whichever is first); everything else as admission
counts it. A Student who keeps working keeps their lab, so the forecast can be early.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from ..scheduler import service as scheduler
from ..scheduler.capacity import ClusterCapacity, Committed, Resources, available
from .models import ENDING, RUNS_RESOURCES, LabSession

logger = logging.getLogger(__name__)
SLOT = timedelta(minutes=15)
BUSY = "Labs are busy"


def demand(profile: dict[str, Any]) -> Resources:
    """What one lab of this profile holds while it runs."""
    nodes = profile["nodes"]
    return Resources(
        sum(n["vcpu"] for n in nodes), sum(n["ram_mb"] for n in nodes), sum(n["disk_gb"] for n in nodes)
    )


def lifetime(profile: dict[str, Any]) -> timedelta:
    """How long a lab of this profile may run: the span it must fit."""
    return timedelta(minutes=profile["lifetime"]["max_minutes"])


def refusal(db: Session, need: Resources, length: timedelta, now: datetime) -> str | None:
    """Why a lab needing ``need`` for ``length`` from ``now`` cannot start, or None.

    Takes the scheduler's booking lock first; the caller writes the lab's range in the same
    transaction, so the next check (a lab or a booking) counts it."""
    scheduler.serialize_bookings(db)
    provider = scheduler.get_capacity_provider(db)
    supply = provider.supply()
    if _fits(need, supply, provider.committed_many(db, _slices(now, now + length))):
        return None
    return busy_message(next_free(db, need, length, now), now)


def next_free(db: Session, need: Resources, length: timedelta, now: datetime) -> datetime | None:
    """The first 15-minute mark after ``now`` from which a lab fits for ``length``, within the
    scheduler's search horizon (``SCHEDULER_NEXT_FREE_DAYS``), or None."""
    provider = ClusterCapacity(db, ends=_lab_ends(db, now))
    supply = provider.supply()
    first = _ceil(now + timedelta(seconds=1))
    span = int(length / SLOT) + (length % SLOT > timedelta(0))
    slices = _slices(first, first + timedelta(days=scheduler.NEXT_FREE_DAYS) + length)
    ok = [_fits(need, supply, [c]) for c in provider.committed_many(db, slices)]
    run = 0
    for i, fit in enumerate(ok):
        run = run + 1 if fit else 0
        if run >= span:
            return slices[i - span + 1][0]
    return None


def busy_message(at: datetime | None, now: datetime) -> str:
    """"Labs are busy, next free at HH:MM", in the site's time zone; the date too when it is
    not today there."""
    if at is None:
        return f"{BUSY}, and none is free in the next {scheduler.NEXT_FREE_DAYS} days"
    tz = site_timezone()
    local = at.astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return f"{BUSY}, next free at {local:%H:%M}"
    return f"{BUSY}, next free at {local:%H:%M} on {local:%a %d %b}"


def site_timezone() -> ZoneInfo:
    name = os.getenv("SITE_TIMEZONE", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("SITE_TIMEZONE=%r is not a time zone; using UTC", name)
        return ZoneInfo("UTC")


# ── internals ────────────────────────────────────────────────────────


def _fits(need: Resources, supply: Resources, committed: Sequence[Committed]) -> bool:
    for c in committed:
        free = available(supply, c.resources)
        if need.vcpu > free.vcpu or need.ram_mb > free.ram_mb or need.disk_gb > free.disk_gb:
            return False
    return True


def _lab_ends(db: Session, now: datetime) -> dict:
    """Running labs' ranges -> when each closes if nobody uses it (a forecast only)."""
    out = {}
    rows = db.query(LabSession.range_id, LabSession.state, LabSession.idle_expires_at, LabSession.max_expires_at)
    for range_id, state, idle, latest in rows.filter(
        LabSession.state.in_(RUNS_RESOURCES), LabSession.range_id.isnot(None)
    ):
        if state in ENDING:
            out[range_id] = now  # being torn down
            continue
        ends = [_utc(t) for t in (idle, latest) if t is not None]
        if ends:
            out[range_id] = min(ends)
    return out


def _slices(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out, t = [], _utc(start)
    end = _utc(end)
    while t < end:
        out.append((t, min(t + SLOT, end)))
        t += SLOT
    return out


def _ceil(t: datetime) -> datetime:
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    return epoch + -((epoch - _utc(t)) // SLOT) * SLOT


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)
