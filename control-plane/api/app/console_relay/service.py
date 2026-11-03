"""Console grants: issue one with a console, claim it when the browser's socket opens (#366).

A grant is permission to open one VM's console for one user for a few minutes, once at a
time. It is not the authentication of the socket (the signed-in user is, ``ws_auth``); it
names the VM and the audience, and being unguessable it cannot be found by trying ids.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..console_backends import GRANT_SECONDS, BaseConsoleBackend
from ..models import Range
from ..tenancy import tenant_uuid
from .models import ConsoleGrant

#: A connected relay renews its claim this often, and a claim lapses this long after its
#: last renewal: a relay that died frees the grant within ``LIVE_LEASE``, not at expiry.
RENEW_SECONDS = 10
LIVE_LEASE = timedelta(seconds=30)
#: Lapsed grants are removed this long after they expired (kept briefly for support questions).
KEEP_AFTER_EXPIRY = timedelta(hours=1)

DENIED, EXPIRED, BUSY, OK = "denied", "expired", "busy", "ok"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def short(grant_id: Any) -> str:
    """A grant's id as logs may show it: the first eight characters, never the token."""
    return str(grant_id)[:8]


def ws_base(headers: Any) -> str:
    """The public ``wss://`` origin of this TrueNorth: ``DOMAIN`` when the site sets it (as
    the cmi5 and xAPI URLs do), else the host the request came in on (nginx forwards ``Host``
    and ``X-Forwarded-Proto``)."""
    domain = os.getenv("DOMAIN", "").strip()
    if domain:
        return f"wss://{domain}"
    host = headers.get("host") or "localhost"
    plain = (headers.get("x-forwarded-proto") or "").lower() == "http"
    return f"{'ws' if plain else 'wss'}://{host}"


def issue(db: Session, backend: BaseConsoleBackend, *, user_id: uuid.UUID, tenant_id: uuid.UUID, rng: Range,
          vm: dict[str, Any], view_only: bool, observed: bool, base: str, adapter_view_only: bool = False,
          ) -> dict[str, Any]:
    """Store a grant for ``user_id`` on one VM of ``rng`` and return the adapter's ``open``
    answer (``kind`` ``rfb-ws``, ``url`` ``<base>/ws/console/<token>``). The caller commits.
    Nothing is stored when the adapter refuses."""
    token = secrets.token_urlsafe(32)  # 256 bits
    url = f"{base}/ws/console/{token}"
    open_ = backend.open_view_only if adapter_view_only else backend.open
    access = open_(vm, db=db, rng=rng, relay_url=url)
    now = _now()
    db.add(ConsoleGrant(
        token_hash=token_hash(token), tenant_id=tenant_id, user_id=user_id, range_id=rng.id,
        vm_id=str(vm.get("vm_id") or ""), vm_name=str(vm.get("name") or ""), view_only=view_only,
        observed=observed, created_at=now, expires_at=now + timedelta(seconds=GRANT_SECONDS)))
    # tenant-safe: housekeeping of lapsed grants of every tenant; reads and returns no row.
    db.query(ConsoleGrant).filter(ConsoleGrant.expires_at < now - KEEP_AFTER_EXPIRY).delete(
        synchronize_session=False)
    return access


@dataclass(frozen=True)
class Claim:
    outcome: str
    grant: ConsoleGrant | None = None
    holder: str = ""  # who holds the live claim, on OK: pass it to renew and release


def claim(db: Session, token: str, user: CurrentUser) -> Claim:
    """Take the grant's one live socket for ``user``. ``DENIED`` for a token that is unknown,
    or whose grant belongs to another user or tenant (the same answer for all, no oracle);
    ``EXPIRED`` and ``BUSY`` are said only to the grant's own user. Commits."""
    if not token or len(token) > 128:
        return Claim(DENIED)
    grant = (
        db.query(ConsoleGrant)
        .filter(ConsoleGrant.token_hash == token_hash(token), ConsoleGrant.tenant_id == tenant_uuid(user),
                ConsoleGrant.user_id == uuid.UUID(str(user.id)))
        .first()
    )
    if grant is None:
        return Claim(DENIED)
    now = _now()
    if _aware(grant.expires_at) <= now:
        return Claim(EXPIRED, grant)
    # One statement decides it, so two workers racing for the grant cannot both win.
    holder = secrets.token_hex(8)
    won = db.execute(
        update(ConsoleGrant)
        .where(ConsoleGrant.id == grant.id, or_(ConsoleGrant.live_until.is_(None), ConsoleGrant.live_until < now))
        .values(live_until=now + LIVE_LEASE, live_by=holder),
        execution_options={"synchronize_session": False},
    ).rowcount
    db.commit()
    return Claim(OK, grant, holder) if won else Claim(BUSY, grant)


def renew(db: Session, grant_id: uuid.UUID, holder: str) -> bool:
    """Extend the claim ``holder`` took; False when it no longer holds it (it lapsed and
    another socket took the grant)."""
    won = db.execute(
        update(ConsoleGrant).where(ConsoleGrant.id == grant_id, ConsoleGrant.live_by == holder)
        .values(live_until=_now() + LIVE_LEASE),
        execution_options={"synchronize_session": False},
    ).rowcount
    db.commit()
    return bool(won)


def release(db: Session, grant_id: uuid.UUID, holder: str) -> None:
    """Give the grant's one socket back, if ``holder`` still has it. Idempotent."""
    db.execute(
        update(ConsoleGrant).where(ConsoleGrant.id == grant_id, ConsoleGrant.live_by == holder)
        .values(live_until=None, live_by=None),
        execution_options={"synchronize_session": False},
    )
    db.commit()
