"""Email status and a test message (#306).

=====================  =========================================================
Endpoint               Who
=====================  =========================================================
GET  /email/status     any signed-in user: whether email is configured. The platform
                       administrator also gets the relay (host, port, mode, sender,
                       account name). The password is never returned to anyone.
POST /email/test       the platform administrator: one message to their OWN address,
                       at most once a minute, audited (``email.test``) with its outcome.
=====================  =========================================================

The banner ("Email is not configured") and the Classes page's roll-call warning read the
status; neither decides anything, since mail is only ever sent by the API.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import AuditLog
from ..notifications.smtp import SMTPChannel, smtp_settings
from ..rbac import is_platform_admin, require_platform_admin
from ..tenancy import tenant_uuid
from .schemas import EmailRelayOut, EmailStatusOut, EmailTestOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email", tags=["email"])

TEST_ACTION = "email.test"
TEST_INTERVAL = timedelta(seconds=60)
TEST_TIMEOUT_S = 15.0
_PLATFORM_ADMIN = require_platform_admin()


def now() -> datetime:
    """The clock (tests move it)."""
    return datetime.now(UTC)


def _aware(ts: datetime) -> datetime:
    """SQLite hands back naive timestamps; they were written in UTC."""
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


@router.get("/status", response_model=EmailStatusOut, operation_id="getEmailStatus",
            summary="Whether the API sends email; the relay for the platform administrator only")
def get_status(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)) -> EmailStatusOut:
    cfg = smtp_settings()
    admin = is_platform_admin(user, db)
    relay = None
    if admin and cfg.host:
        relay = EmailRelayOut(
            host=cfg.host, port=cfg.port, security=cfg.security, from_address=cfg.from_addr,
            username=cfg.user, signs_in=bool(cfg.user and cfg.password),
        )
    return EmailStatusOut(configured=cfg.configured, relay=relay, can_send_test=admin and cfg.configured)


def _last_test(db: Session, user_id: uuid.UUID) -> datetime | None:
    ts = db.execute(
        select(AuditLog.timestamp)
        .where(AuditLog.action == TEST_ACTION, AuditLog.user_id == user_id)
        .order_by(AuditLog.timestamp.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _aware(ts) if ts else None


@router.post("/test", response_model=EmailTestOut, operation_id="sendTestEmail",
             summary="Send one test message to the platform administrator's own address")
async def send_test(db: Session = Depends(get_db), user: CurrentUser = Depends(_PLATFORM_ADMIN)) -> EmailTestOut:
    """The recipient is always the caller's own address: the body takes no recipient, so
    this cannot be used to mail anyone else. Refused while email is not configured (409),
    and more than once a minute per administrator (429). Every message sent is audited."""
    cfg = smtp_settings()
    if not cfg.configured:
        raise HTTPException(409, 'Email is not configured on this server (install/README.md, "Email (SMTP)").')
    to = (user.email or "").strip()
    if "@" not in to:
        raise HTTPException(409, "Your account has no email address to send the test to.")
    uid = uuid.UUID(str(user.id))
    at = now()
    last = _last_test(db, uid)
    if last and at - last < TEST_INTERVAL:
        wait = int((TEST_INTERVAL - (at - last)).total_seconds()) + 1
        raise HTTPException(429, f"A test email was sent less than a minute ago. Try again in {wait} s.",
                            headers={"Retry-After": str(wait)})
    # Recorded before sending, so a request after this commit is refused. Two that check at
    # the same instant can both send: bounded (platform administrator only, each audited).
    row = AuditLog(user_id=uid, tenant_id=tenant_uuid(user), action=TEST_ACTION, resource_type="email",
                   resource_id="smtp", detail=f"to {to} via {cfg.host}:{cfg.port}: sending", timestamp=at)
    db.add(row)
    db.commit()
    # One attempt with a short timeout: a relay that drops packets answers in seconds, not
    # the three minutes the background channel's retries would take.
    sent = await SMTPChannel(attempts=1, timeout=TEST_TIMEOUT_S).send(
        to,
        "TrueNorth test email",
        "This is a test message from TrueNorth Range, sent from Administration at your request.\n\n"
        "If you can read it, roll-call onboarding emails, reminders and calendar invites can reach you.",
    )
    detail = (f"The relay {cfg.host}:{cfg.port} accepted the message for {to}. Check that inbox."
              if sent else f"The relay {cfg.host}:{cfg.port} did not accept the message (the API log says why).")
    row.detail = f"to {to} via {cfg.host}:{cfg.port}: {'accepted' if sent else 'FAILED'}"
    db.commit()
    return EmailTestOut(sent=sent, to=to, detail=detail)
