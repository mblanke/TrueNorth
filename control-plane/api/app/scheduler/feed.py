"""Per-user calendar feed (ADR 0004 §6, "Feed security").

Calendar clients (Outlook "Subscribe from web", Apple Calendar, Thunderbird, Google)
cannot sign in to Keycloak, so the feed URL carries a long random per-user token:

- only its SHA-256 is stored; the token itself is shown once, when issued;
- regenerating replaces it and revoking deletes it, so an old URL stops working;
- the owner's role is checked on every fetch, so losing ``schedule:read`` (or the
  account) takes effect at the client's next refresh;
- the URL holds no personal data, and request logs redact it (``middleware.py``,
  nginx ``access_log off``);
- the feed holds only what its owner may see: their tenant's bookings.

Students hold no ``schedule:read``: their feed holds only the sessions of courses they
are enrolled in (decided 2026-10-06), with no capacity and no other people.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import User, UserRole
from ..rbac import Permission, user_has_permission
from . import ics
from .models import EventState, FeedToken, ScheduledEvent

S = EventState
# Cancelled bookings stay in the feed (as STATUS:CANCELLED) so clients remove them.
FEED_STATES = (S.scheduled, S.provisioning, S.active, S.completed, S.cancelled)
HISTORY = timedelta(days=30)
HORIZON = timedelta(days=365)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(db: Session, user: CurrentUser) -> str:
    """A new token for the caller, replacing any old one. Does not commit."""
    token = secrets.token_urlsafe(32)  # 256 bits
    uid = _uuid(user)
    row = db.get(FeedToken, uid)
    if row is None:
        db.add(FeedToken(user_id=uid, token_hash=_digest(token)))
    else:
        row.token_hash = _digest(token)
        row.created_at = datetime.now(UTC)
    return token


def revoke(db: Session, user: CurrentUser) -> bool:
    row = db.get(FeedToken, _uuid(user))
    if row is None:
        return False
    db.delete(row)
    return True


def issued_at(db: Session, user: CurrentUser) -> datetime | None:
    row = db.get(FeedToken, _uuid(user))
    return row.created_at if row else None


def owner_for(db: Session, token: str) -> User | None:
    """The user a token belongs to, if they may still read the schedule."""
    if not token or len(token) > 128:
        return None
    row = db.query(FeedToken).filter(FeedToken.token_hash == _digest(token)).first()
    if row is None:
        return None
    # tenant-safe: the token's own owner; render() then scopes the feed to owner.tenant_id.
    owner = db.get(User, row.user_id)
    if owner is None or not owner.is_active or owner.deleted_at is not None:
        return None
    if not (user_has_permission(owner, Permission.SCHEDULE_READ) or owner.role == UserRole.student):
        return None
    return owner


def render(db: Session, owner: User, now: datetime | None = None) -> str:
    """The owner's feed, from 30 days back to a year ahead: staff see their tenant's
    bookings; a Student sees only the sessions of courses they are enrolled in."""
    now = now or datetime.now(UTC)
    scope = [ScheduledEvent.tenant_id == owner.tenant_id]
    if not user_has_permission(owner, Permission.SCHEDULE_READ):
        from ..enrollment import active_course_ids

        scope.append(ScheduledEvent.course_id.in_(active_course_ids(db, owner.id) or [None]))
    events = (
        db.query(ScheduledEvent)
        .filter(
            *scope,
            ScheduledEvent.state.in_(FEED_STATES),
            ScheduledEvent.end_time >= now - HISTORY,
            ScheduledEvent.start_time <= now + HORIZON,
        )
        .order_by(ScheduledEvent.start_time)
        .limit(2000)
        .all()
    )
    return ics.calendar((to_ics(e) for e in events), method="PUBLISH", name="TrueNorth Range schedule", now=now)


def to_ics(e: ScheduledEvent) -> ics.IcsEvent:
    lines = [e.description] if e.description else []
    lines.append(f"State: {e.state.value}")
    if e.vm_count:
        lines.append(f"VMs: {e.vm_count}")
    return ics.IcsEvent(
        uid=ics.booking_uid(e.id),
        sequence=e.sequence or 0,
        start=e.start_time,
        end=e.end_time,
        summary=e.name,
        description="\n".join(lines),
        status="CANCELLED" if e.state == S.cancelled else "CONFIRMED",
        last_modified=e.updated_at,
    )


def feed_url(request: Request, token: str) -> str:
    """The subscription URL. ``SCHEDULER_FEED_BASE_URL`` sets the public origin: Outlook
    on the web fetches from Microsoft's cloud, so it must be reachable from there.

    Unset (development), the request's own ``Host`` is used. ``X-Forwarded-Host`` never
    is: nginx passes a client's through, so it would let a caller choose the host a
    bearer-token URL points at."""
    base = os.getenv("SCHEDULER_FEED_BASE_URL", "").rstrip("/")
    if not base:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host") or request.url.netloc
        base = f"{proto}://{host}"
    return f"{base}/api/v1/schedule/feed/{token}.ics"


def warn_if_unconfigured() -> bool:
    """Log a warning at startup when a deployment with auth on has no feed origin set.
    Returns whether it warned."""
    if os.getenv("SCHEDULER_FEED_BASE_URL", "").strip():
        return False
    if os.getenv("AUTH_DISABLED", "false").lower() == "true":
        return False  # development
    logging.getLogger("truenorth.scheduler").warning(
        "SCHEDULER_FEED_BASE_URL is not set: calendar-feed URLs will use each request's Host "
        "header. Set it to the public origin (https://range.example) in production."
    )
    return True


def webcal(url: str) -> str:
    """The one-click subscribe link: the same URL with the scheme swapped."""
    return "webcal://" + url.split("://", 1)[1] if "://" in url else url


def _uuid(user: CurrentUser) -> uuid.UUID:
    return user.id if isinstance(user.id, uuid.UUID) else uuid.UUID(str(user.id))
