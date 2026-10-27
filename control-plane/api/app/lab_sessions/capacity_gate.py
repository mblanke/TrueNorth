"""Self-serve labs use only the capacity left after approved classes (ADR 0014 §18, #288).

**A booked class's own labs.** A Student on the roster of an approved booking of the lab's
course that holds now (a class session: its roll call; another booking: an assigned
enrolment, as Live Class #199 counts it), with no range or template of its own (its demand
was sized for its Students' labs), draws on that booking's hold (``own_booking``): it is
not gated, and the booking counts its range (``holdings.drawing_on``), never twice. Labs
beyond what the booking holds are self-serve.

**A self-serve lab** (``fits``) must fit in every 15-minute slice from now to its maximum
lifetime, after approved bookings over their held windows, approved classes over their whole
span (``classes.holds``, judged at peak as #299 does) and every range running now (labs until
their maximum lifetime; a lab being reset at its size). A booking awaiting approval holds
nothing. When it does not fit the Student sees "Labs are busy, next free at HH:MM" in the
site's time zone (``SITE_TIMEZONE``, IANA, UTC by default). The message carries a time only.

**Locks.** The decision is taken under the scheduler's booking lock (``lock``), which a
launch takes before any row lock, the order the classes section uses (roll-call changes);
the lab's range is written in the same transaction. The next free time is a forecast and is
never computed under the lock: a refused launch rolls back first (``explain``), and a queued
lab's page reads it outside the sweep (``queued_message``), at most every
``FORECAST_SECONDS`` per lab. A queued lab refused in the last ``FORECAST_SECONDS`` is not
re-decided either.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from ..scheduler import service as scheduler
from ..scheduler.capacity import (
    PROVISION_LEAD,
    TEARDOWN_GRACE,
    CapacityProvider,
    ClusterCapacity,
    Committed,
    Resources,
    available,
)
from ..scheduler.lifecycle import HOLDING
from ..scheduler.models import ScheduledEvent
from . import holdings
from .models import LabSession

logger = logging.getLogger(__name__)
SLOT = timedelta(minutes=15)
BUSY = "Labs are busy"
FORECAST_SECONDS = 30
_refused: dict[uuid.UUID, datetime] = {}  # queued lab -> when it was last refused (this process)
_forecasts: dict[uuid.UUID, tuple[datetime, str]] = {}  # queued lab -> (when, message)


def demand(profile: dict[str, Any]) -> Resources:
    """What one lab of this profile holds while it runs."""
    nodes = profile["nodes"]
    return Resources(
        sum(n["vcpu"] for n in nodes), sum(n["ram_mb"] for n in nodes), sum(n["disk_gb"] for n in nodes)
    )


def lifetime(profile: dict[str, Any]) -> timedelta:
    """How long a lab of this profile may run: the span it must fit."""
    return timedelta(minutes=profile["lifetime"]["max_minutes"])


def lock(db: Session) -> None:
    """The scheduler's booking lock, for this transaction. Taken before the Student's and the
    tenant's rows, as the classes section takes it before writing roll-call rows."""
    scheduler.serialize_bookings(db)


# ── the decision (under the lock) ─────────────────────────────────────


def own_booking(
    db: Session, session: LabSession, course_id: uuid.UUID, need: Resources, now: datetime
) -> ScheduledEvent | None:
    """The approved booking holding now whose hold this Student's lab draws on, or None."""
    from sqlalchemy import and_, or_

    from ..classes import sessions as class_sessions
    from ..enrollment import rostered_course_ids

    plain = and_(
        ScheduledEvent.id.notin_(class_sessions.all_session_ids()),
        ScheduledEvent.course_id.in_(rostered_course_ids(db, session.user_id) or [None]),
    )
    rows = (
        db.query(ScheduledEvent)
        .filter(
            ScheduledEvent.tenant_id == session.tenant_id,
            ScheduledEvent.course_id == course_id,
            ScheduledEvent.state.in_(HOLDING),
            ScheduledEvent.range_id.is_(None),
            ScheduledEvent.template_id.is_(None),
            ScheduledEvent.start_time <= now + PROVISION_LEAD,
            ScheduledEvent.end_time > now - TEARDOWN_GRACE,
            or_(plain, ScheduledEvent.id.in_(class_sessions.session_ids_on_roll_call(session.user_id))),
        )
        .order_by(ScheduledEvent.start_time)
        .all()
    )
    for evt in rows:
        if _utc(evt.end_time) + TEARDOWN_GRACE <= now:
            continue
        vcpu, ram, disk = holdings.drawn(db, evt.id, exclude=session.id)
        if (vcpu + need.vcpu <= (evt.vcpu_total or 0) and ram + need.ram_mb <= (evt.ram_mb_total or 0)
                and disk + need.disk_gb <= (evt.disk_gb_total or 0)):
            return evt
    return None


def fits(db: Session, need: Resources, length: timedelta, now: datetime) -> bool:
    """Whether a self-serve lab needing ``need`` for ``length`` from ``now`` fits. Call under
    ``lock``; the caller writes the lab's range in the same transaction."""
    provider = scheduler.get_capacity_provider(db)
    return _fits(need, provider.supply(), provider.committed_many(db, _slices(now, now + length)))


def recently_refused(session_id: uuid.UUID, now: datetime) -> bool:
    at = _refused.get(session_id)
    return at is not None and now - at < timedelta(seconds=FORECAST_SECONDS)


def note_refused(session_id: uuid.UUID, now: datetime) -> None:
    if len(_refused) > 10000:
        _refused.clear()
    _refused[session_id] = now


# ── the message (outside the lock) ────────────────────────────────────


def explain(db: Session, need: Resources, length: timedelta, now: datetime) -> str:
    """"Labs are busy, next free at HH:MM". Call with no lock held (after the refused
    launch rolled back)."""
    return busy_message(next_free(db, need, length, now), now)


def queued_message(db: Session, session: LabSession, need: Resources, length: timedelta, now: datetime) -> str:
    """The message for a lab waiting in the queue for capacity, at most every
    ``FORECAST_SECONDS`` per lab (its page polls every few seconds)."""
    hit = _forecasts.get(session.id)
    if hit is not None and now - hit[0] < timedelta(seconds=FORECAST_SECONDS):
        return hit[1]
    message = explain(db, need, length, now)
    if len(_forecasts) > 10000:
        _forecasts.clear()
    _forecasts[session.id] = (now, message)
    return message


def next_free(
    db: Session, need: Resources, length: timedelta, now: datetime, provider: CapacityProvider | None = None
) -> datetime | None:
    """The first 15-minute mark after ``now`` from which a lab fits for ``length``, within the
    scheduler's search horizon (``SCHEDULER_NEXT_FREE_DAYS``), or None. Running labs count
    until they would close if unused: a forecast."""
    provider = provider or ClusterCapacity(db, ends=holdings.ends(db, now, forecast=True))
    supply = provider.supply()
    first = _ceil(now + timedelta(seconds=1))
    span = int(length / SLOT) + (length % SLOT > timedelta(0))
    slices = _slices(first, first + timedelta(days=scheduler.NEXT_FREE_DAYS) + length)
    run = 0
    for i, c in enumerate(provider.committed_many(db, slices)):
        run = run + 1 if _fits(need, supply, [c]) else 0
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
