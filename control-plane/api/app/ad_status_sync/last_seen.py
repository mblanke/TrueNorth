"""When TrueNorth last saw an account, and the break-glass sign-in alert (#290).

**Why TrueNorth's own record, not Keycloak's.** The 90-day rule asks whether a local
account is still used. Keycloak could answer through its LOGIN events, but they are kept
exactly 90 days (``eventsExpiration`` 7776000, install/roles/tn_keycloak/files/mfa-spec.json),
so an account last used 89 days ago and one never used look alike at the edge; reading them
needs the ``view-events`` role the API's client does not hold; and an SSO session refresh is
not a LOGIN event at all. Keycloak sessions show only who is signed in now. What matters is
use of TrueNorth, and every authenticated request already passes ``auth.get_signed_in_user``:
it calls ``note`` once the account checks pass (not for a disabled account or a refused
second factor), which writes ``user_last_seen`` at most once an hour per account and process.

An account with no row has not been seen since this was installed: the sync counts its last
use from the later of its creation and the first sync run (``service.last_use``), so nobody
is disabled for the time before TrueNorth kept the record.

**Break-glass.** The local recovery administrator (``break_glass_usernames``) must never be
locked out, so it is never disabled; its use is announced instead, at most once an hour per
process: the Prometheus counter ``truenorth_break_glass_sign_ins_total`` (alert
``BreakGlassAccountUsed``, critical, delivered by Alertmanager when alert email is on), a
``break_glass_sign_in`` audit row (the AD status sync panel's "last used") and a WARNING log.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditLog, User
from .models import UserLastSeen

logger = logging.getLogger("truenorth.api.ad_status_sync.last_seen")

RESOLUTION = timedelta(hours=1)
_written: dict[str, datetime] = {}  # user id -> when this process last wrote it


def break_glass_usernames() -> set[str]:
    """``AD_STATUS_SYNC_BREAK_GLASS`` (comma-separated Keycloak usernames), lower case. The
    installer sets it to the bootstrap administrator's UPN only where that is a local recovery
    account beside AD (``tn_ldap_enabled`` and ``tn_keycloak_local_admin``); otherwise it is
    empty: no break-glass, no 90-day rule, no alert. No fallback: guessing would alert on an
    ordinary administrator's every sign-in."""
    raw = os.getenv("AD_STATUS_SYNC_BREAK_GLASS") or ""
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def is_break_glass_identity(identity) -> bool:
    """A token from the break-glass account: its username is configured as break-glass and it
    carries no AD claim (a directory account of the same name is not the local one)."""
    name = (getattr(identity, "preferred_username", "") or "").lower()
    return bool(name) and name in break_glass_usernames() and not getattr(identity, "ad_object_guid", None)


def touch(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> None:
    """Record ``user_id`` as seen at ``now``. Does not commit."""
    now = now or datetime.now(UTC)
    n = (
        db.query(UserLastSeen)
        .filter(UserLastSeen.user_id == user_id)
        .update({UserLastSeen.last_seen_at: now}, synchronize_session=False)
    )
    if n:
        return
    try:
        with db.begin_nested():
            db.add(UserLastSeen(user_id=user_id, last_seen_at=now))
    except IntegrityError:  # another request inserted it first: it is as recent as ours
        pass


def note(db: Session, user: User, identity) -> None:
    """Called by ``auth.get_signed_in_user`` for an account that passed its checks. Never
    raises: a failure to record use must not refuse the request."""
    now = datetime.now(UTC)
    key = str(user.id)
    last = _written.get(key)
    if last is not None and now - last < RESOLUTION:
        return
    try:
        touch(db, user.id, now)
        if is_break_glass_identity(identity):
            db.add(AuditLog(
                user_id=user.id, tenant_id=user.tenant_id, action="break_glass_sign_in", resource_type="user",
                resource_id=key, detail="the local break-glass account was used (never disabled by the AD status sync)",
            ))
            logger.warning("Break-glass account in use: user %s", user.id)
        db.commit()
        if is_break_glass_identity(identity):
            from ..metrics import break_glass_sign_ins_total

            break_glass_sign_ins_total.inc()  # Prometheus alert BreakGlassAccountUsed
        _written[key] = now
    except Exception:  # noqa: BLE001 -- recorded on the next request instead
        db.rollback()
        logger.warning("Could not record last use of user %s", user.id, exc_info=True)
