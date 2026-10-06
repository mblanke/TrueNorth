"""Emailed calendar invites (ADR 0004 §6, slice 6).

Through the existing SMTP channel, as iCalendar over email (RFC 6047): Outlook and other
mail clients show Accept and Decline.

- ``METHOD:REQUEST`` when a booking is scheduled, and again, with the same ``UID`` and
  a higher ``SEQUENCE``, when it moves;
- ``METHOD:CANCEL`` when it is cancelled, or to the old instructor when it changes hands.

Recipients are the booking's Instructor. Students are not invited until bookings know
their attendees (an open question in the ADR). Accept/Decline replies go to the
organizer mailbox (``SCHEDULER_ORGANIZER_EMAIL``, else ``SMTP_FROM``) and are not read
back. Sending happens after the response (FastAPI background task) and is best effort:
a failure is logged, the booking is unaffected.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import User
from . import ics
from .capacity import window_label
from .models import ScheduledEvent

logger = logging.getLogger("truenorth.api.scheduler.invites")


@dataclass(frozen=True)
class Invite:
    to: str
    subject: str
    body: str
    method: str  # REQUEST | CANCEL
    calendar: str


def organizer() -> str:
    return os.getenv("SCHEDULER_ORGANIZER_EMAIL") or os.getenv("SMTP_FROM") or "scheduler@truenorth.local"


def build(evt: ScheduledEvent, method: str, to: str) -> Invite:
    """One invite for one recipient, for the booking as it is now."""
    when = window_label(evt.start_time, evt.end_time)
    cancelled = method == "CANCEL"
    event = ics.IcsEvent(
        uid=ics.booking_uid(evt.id),
        sequence=evt.sequence or 0,
        start=evt.start_time,
        end=evt.end_time,
        summary=evt.name,
        description=evt.description or "",
        status="CANCELLED" if cancelled else "CONFIRMED",
        last_modified=evt.updated_at,
        organizer=organizer(),
        attendees=(to,),
    )
    verb = "Cancelled" if cancelled else ("Updated" if (evt.sequence or 0) > 0 else "Invitation")
    return Invite(
        to=to,
        subject=f"{verb}: {evt.name}, {when}",
        body=(
            f"'{evt.name}' has been cancelled ({when}).\n" if cancelled else f"You are teaching '{evt.name}' {when}.\n"
        ),
        method=method,
        calendar=ics.calendar([event], method=method, name="TrueNorth Range"),
    )


def instructor_email(db: Session, instructor_id: uuid.UUID | None) -> str | None:
    if not instructor_id:
        return None
    u = db.get(User, instructor_id)
    if u is None or not u.email or not u.is_active or u.deleted_at is not None:
        return None
    return u.email


def plan(
    db: Session, evt: ScheduledEvent, method: str, *, previous_instructor: uuid.UUID | None = None
) -> list[Invite]:
    """The invites a change to ``evt`` calls for. Built now, while the session is open."""
    out: list[Invite] = []
    to = instructor_email(db, evt.instructor_id)
    if to:
        out.append(build(evt, method, to))
    if previous_instructor and previous_instructor != evt.instructor_id:
        old = instructor_email(db, previous_instructor)
        if old:
            out.append(build(evt, "CANCEL", old))
    return out


async def send(invites: list[Invite]) -> None:
    if not invites:
        return
    from ..notifications import get_channel

    channel = get_channel("email")
    for inv in invites:
        try:
            ok = await channel.send(inv.to, inv.subject, inv.body, {"calendar": (inv.method, inv.calendar)})
            if not ok:
                logger.warning("Calendar invite (%s) was not delivered", inv.method)
        except Exception:
            logger.exception("Calendar invite (%s) failed", inv.method)
