"""Telling the instructor what the approver decided (ADR 0014 §16; issue #319).

When an approver approves, rejects or proposes another time for a class session, its
instructor hears about it: an in-app notification (the inbox, written in the decision's own
transaction, so it lands with it or not at all), and an email too when email is configured
(``SMTP_*``; the email is sent after the commit, once, and never retried into duplicates).

Who: the booking's instructor and, for a class session, the class's lead (the one who
changes its sessions, ADR 0014 §8), when they are active accounts of the booking's tenant.
The approver is never told of their own decision.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import User
from .capacity import window_label
from .models import ScheduledEvent

logger = logging.getLogger("truenorth.api.scheduler.approval_notices")

LINK = "/calendar/events"


@dataclass(frozen=True)
class Notice:
    to: str
    subject: str
    body: str


def _recipients(db: Session, evt: ScheduledEvent) -> list[User]:
    from ..classes import sessions

    ids = {evt.instructor_id}
    cls = sessions.class_of(db, evt)
    if cls is not None:
        ids.add(cls.lead_instructor_id)
    ids.discard(None)
    if not ids:
        return []
    return (
        db.query(User)
        .filter(User.id.in_(ids), User.tenant_id == evt.tenant_id, User.is_active.is_(True), User.deleted_at.is_(None))
        .order_by(User.email)
        .all()
    )


def _what(decision: str, evt: ScheduledEvent, reason: str | None, proposed: tuple | None) -> tuple[str, str]:
    when = window_label(evt.start_time, evt.end_time)
    if decision == "approved":
        return (f"Session approved: {evt.name}",
                f"Your session {evt.name} ({when}) was approved. Its invites have gone out.")
    if decision == "rejected":
        return (f"Session rejected: {evt.name}",
                f"Your session {evt.name} ({when}) was rejected: {reason or 'no reason given'}. "
                "Book it again at another time, or talk to the approver.")
    start, end = proposed or (evt.start_time, evt.end_time)
    return (f"Another time proposed: {evt.name}",
            f"The approver proposed {window_label(start, end)} instead of {when} for your session {evt.name}. "
            "Accept or decline it on the Schedule or Calendar page.")


def notify(
    db: Session, evt: ScheduledEvent, actor: CurrentUser, decision: str, *,
    reason: str | None = None, proposed: tuple | None = None,
) -> list[Notice]:
    """Queue the in-app notifications (in the caller's transaction) and return the emails
    to send after the commit (none when email is not configured). Does not commit."""
    from ..notifications import inbox
    from ..notifications.smtp import smtp_settings

    people = _recipients(db, evt)
    if not people:
        return []
    title, message = _what(decision, evt, reason, proposed)
    from ..classes.service import _subject_safe

    title = _subject_safe(title)  # a booking's name goes into an email subject: no line breaks
    me = uuid.UUID(str(actor.id))
    inbox.notify(db, tenant_id=evt.tenant_id, user_ids=[u.id for u in people], actor_id=me, title=title,
                 message=message, link=LINK, kind=f"booking_{decision}")
    if not smtp_settings().configured:
        return []
    return [Notice(to=u.email, subject=title, body=message + "\n") for u in people if u.id != me and u.email]


async def send(notices: list[Notice]) -> None:
    """After the commit. At most once: a failure is logged."""
    if not notices:
        return
    from ..notifications import get_channel

    channel = get_channel("email")
    for n in notices:
        try:
            if not await channel.send(n.to, n.subject, n.body):
                logger.warning("Approval notice (%s) was not delivered", n.subject)
        except Exception:
            logger.exception("Approval notice (%s) failed", n.subject)
