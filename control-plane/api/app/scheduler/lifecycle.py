"""Booking lifecycle (ADR 0004, "Lifecycle").

    draft -> scheduled -> provisioning -> active -> completed
    cancelled from any state before completed

Every move is a guarded UPDATE (``... WHERE state IN (allowed sources)``), so two
requests racing to move the same booking cannot both succeed, and a retry cannot move
it twice. Asking for the state a booking is already in is a no-op, so retries by the
worker (slice 4) or a double-click are harmless.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import EventState, ScheduledEvent

S = EventState

# target state -> the states it may be reached from
SOURCES: dict[EventState, frozenset[EventState]] = {
    S.scheduled: frozenset({S.draft}),
    S.provisioning: frozenset({S.scheduled}),
    # Activating straight from scheduled covers a range that was already up.
    S.active: frozenset({S.scheduled, S.provisioning}),
    S.completed: frozenset({S.active}),
    S.cancelled: frozenset({S.draft, S.scheduled, S.provisioning, S.active}),
}

# Only a booking that has not started being built can be rescheduled or resized.
EDITABLE = frozenset({S.draft, S.scheduled})

# States that hold capacity, the range and the instructor.
HOLDING = (S.scheduled, S.provisioning, S.active)


def transition(db: Session, evt: ScheduledEvent, to: EventState) -> bool:
    """Move ``evt`` to ``to``. Returns False when it was already there (no-op).

    Raises 409 when ``to`` is not reachable from the booking's current state, or when
    another request moved it first. Does not commit.
    """
    if evt.state == to:
        return False
    allowed = SOURCES[to]
    if evt.state not in allowed:
        raise HTTPException(409, f"Event is {evt.state.value}; it cannot become {to.value}")
    moved = (
        db.query(ScheduledEvent)
        .filter(ScheduledEvent.id == evt.id, ScheduledEvent.state.in_(allowed))
        .update({ScheduledEvent.state: to}, synchronize_session=False)
    )
    if moved != 1:
        db.rollback()
        raise HTTPException(409, "Event changed state while this request ran; reload and retry")
    db.expire(evt, ["state"])
    return True


def lock_editable(db: Session, evt: ScheduledEvent) -> None:
    """Refuse unless the booking is editable *now*, and hold its row until the caller
    commits. A no-op guarded UPDATE: on Postgres it row-locks the booking, so the clock's
    transition waits for the edit (and then builds the range the edit chose), or the edit
    waits for the clock and then finds the booking no longer editable. 409 either way
    rather than an edit landing on a booking that is already being built."""
    if evt.state not in EDITABLE:
        raise HTTPException(409, f"Event is {evt.state.value}; only draft or scheduled events can be changed")
    locked = (
        db.query(ScheduledEvent)
        .filter(ScheduledEvent.id == evt.id, ScheduledEvent.state.in_(EDITABLE))
        .update({ScheduledEvent.state: ScheduledEvent.state}, synchronize_session=False)
    )
    if locked != 1:
        db.rollback()
        raise HTTPException(409, "Event changed state while this request ran; reload and retry")


def event_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(404, "Event not found") from None
