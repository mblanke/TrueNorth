"""Email for ticket notifications (issue #378).

A ticket notification is written to the inbox in the request's transaction (``inbox.notify``);
this module decides which of those recipients are also **emailed**, builds the messages, and
sends them after the commit through the same channel the scheduler's notices use. Email is
optional: with no ``SMTP_*`` configured nothing here does anything.

* **No ticket content leaves by email.** A message names the ticket (``TN-12``), what
  happened ("new reply"), and links to it. No subject, details, reply text, internal-note
  text or person's name: the text of a ticket can describe a range, or something worse, and
  mail is stored and forwarded by systems the platform does not control.
* **Personal events only.** Assigned to you, a reply, a status change, an internal note
  (staff). The "new ticket" broadcast to every staff member is in-app only.
* **Each person can turn it off** (``notification_preferences.email_tickets``, default on).
* **Throttled.** At most one email per ticket per person per five minutes, counted from the
  emails actually sent (the notification row is marked ``emailed``); a bell-only
  notification does not count, so an assignment is never swallowed by an earlier broadcast.
  A busy thread sends one email, not ten.
* **After the commit, at most once**: callers hand the notices to ``BackgroundTasks``; one
  attempt per message (the shared channel's retry loop is switched off, because a retry after
  a timeout that came after the relay accepted the message is a duplicate), a failure is
  logged, and never fails the request. A restart between the commit and the send drops the
  email; the bell row is already there.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import User
from .models import NotificationPreference, UserNotification
from .smtp import SMTPChannel, smtp_settings

logger = logging.getLogger("truenorth.api.notifications.ticket_email")

# What is emailed. ticket_new (every staff member, once per ticket) stays in the bell.
EMAIL_KINDS = frozenset({"ticket_assigned", "ticket_reply", "ticket_status", "ticket_note"})
THROTTLE = timedelta(minutes=5)
EMAILED_FLAG = "emailed"  # in a notification's data_json: this one was also sent by email
EMAILED_KEY = "tn_ticket_emailed"  # (user, link) pairs already handled in this transaction


@dataclass(frozen=True)
class Notice:
    user_id: uuid.UUID
    to: str
    subject: str
    body: str


def web_base_url() -> str:
    """The public address of the web app, for the link in the email; '' when not configured.

    ``WEB_BASE_URL``, else the installer's ``LTI_WEB_BASE_URL`` (https://<domain>)."""
    for name in ("WEB_BASE_URL", "LTI_WEB_BASE_URL"):
        value = os.getenv(name, "").strip().rstrip("/")
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _recently_emailed(db: Session, tenant_id: uuid.UUID, user_ids: set[uuid.UUID], link: str) -> set[uuid.UUID]:
    """Those already emailed about this ticket within the throttle window."""
    if not user_ids:
        return set()
    rows = (
        db.query(UserNotification.user_id)
        .filter(
            UserNotification.tenant_id == tenant_id,
            UserNotification.user_id.in_(user_ids),
            UserNotification.created_at >= datetime.now(UTC) - THROTTLE,
            UserNotification.data_json.like(f'%"link": "{_like(link)}"%', escape="\\"),
            UserNotification.data_json.like(f'%"{EMAILED_FLAG}": true%'),
        )
        .distinct()
        .all()
    )
    return {r.user_id for r in rows}


def prepare(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    recipient_ids: Iterable[uuid.UUID | None],
    actor_id: uuid.UUID | None,
    title: str,
    kind: str,
    link: str,
) -> list[Notice]:
    """The emails to send once this transaction commits. Writes nothing.

    Call it **before** ``inbox.notify`` for the same event, so the notification being
    written does not count as "already told"."""
    if kind not in EMAIL_KINDS or not smtp_settings().configured:
        return []
    handled: set[tuple[uuid.UUID, str]] = db.info.setdefault(EMAILED_KEY, set())
    ids = {r for r in recipient_ids if r and r != actor_id and (r, link) not in handled}
    if not ids:
        return []
    people = (
        db.query(User)
        .filter(User.id.in_(ids), User.tenant_id == tenant_id, User.is_active.is_(True), User.deleted_at.is_(None))
        .all()
    )
    people = [u for u in people if u.email]
    if not people:
        return []
    off = {
        r.user_id
        for r in db.query(NotificationPreference.user_id)
        .filter(
            NotificationPreference.tenant_id == tenant_id,
            NotificationPreference.user_id.in_([u.id for u in people]),
            NotificationPreference.email_tickets.is_(False),
        )
        .all()
    }
    told = _recently_emailed(db, tenant_id, {u.id for u in people}, link)
    base = web_base_url()
    notices = []
    for u in sorted(people, key=lambda u: u.email):
        if u.id in off or u.id in told:
            continue
        handled.add((u.id, link))
        notices.append(Notice(user_id=u.id, to=u.email, subject=f"TrueNorth: {title}", body=_body(title, base, link)))
    return notices


def _body(title: str, base: str, link: str) -> str:
    where = f"Open it: {base}{link}" if base else "Open TrueNorth and look under Support."
    return (
        f"{title}\n\n{where}\n\n"
        "This email carries no ticket text. You can turn these emails off from the bell in TrueNorth "
        "(Email me about tickets).\n"
    )


def _channel() -> SMTPChannel:
    return SMTPChannel(attempts=1, timeout=30.0)


async def send(notices: list[Notice]) -> None:
    """After the commit. At most once: a failure is logged, never retried, never raised."""
    if not notices:
        return
    channel = _channel()
    for n in notices:
        try:
            if not await channel.send(n.to, n.subject, n.body):
                logger.warning("Ticket email (%s) was not delivered", n.subject)
        except Exception:  # noqa: BLE001 — mail is best effort; the notification is in the bell
            logger.exception("Ticket email (%s) failed", n.subject)
