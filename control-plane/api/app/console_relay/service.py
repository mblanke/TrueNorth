"""Console grants: issue one with a console, claim it when the browser's socket opens (#366).

A grant is permission to open one VM's console for one user for a few minutes, once at a
time. It is not the authentication of the socket (the signed-in user is, ``ws_auth``); it
names the VM and the audience, and being unguessable it cannot be found by trying ids.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..console_backends import GRANT_SECONDS, BaseConsoleBackend, ConsoleError
from ..models import Range, User
from ..tenancy import tenant_uuid
from .models import ConsoleGrant

#: A connected relay renews its claim this often, and a claim lapses this long after its
#: last renewal: a relay that died frees the grant within ``LIVE_LEASE``, not at expiry.
RENEW_SECONDS = 10
LIVE_LEASE = timedelta(seconds=30)
#: Lapsed grants are removed this long after they expired (kept briefly for support questions).
KEEP_AFTER_EXPIRY = timedelta(hours=1)

#: Abuse bounds. A user holds at most this many live grants (unexpired, not used up); a grant
#: opens a socket at most ``MAX_DIALS`` times (a page reload or a dropped connection, not a
#: standing link); and one API process lets a user dial ``DIALS_PER_MINUTE`` times a minute.
#: The rate limit is a counter in this process, not shared: with several workers the bound is
#: that many times higher, which the other two bounds (4 grants x 3 dials) still cap.
MAX_LIVE_GRANTS = 4
MAX_DIALS = 3
DIALS_PER_MINUTE = 10

DENIED, EXPIRED, SPENT, BUSY, OK = "denied", "expired", "spent", "busy", "ok"
RENEWED, LOST, GONE = "renewed", "lost", "gone"
STUDENT, OBSERVER, STAFF = "student", "observer", "staff"

_DIAL_TIMES: dict[str, deque] = defaultdict(deque)
_DIAL_LOCK = threading.Lock()


class GrantRefusedError(ConsoleError):
    """A grant was not issued; ``status`` is the HTTP answer for the caller."""

    def __init__(self, message: str, status: int = 429):
        super().__init__(message)
        self.status = status


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


def dial_allowed(user_id: Any) -> bool:
    """Count one dial for ``user_id`` unless they dialled ``DIALS_PER_MINUTE`` times in the
    last minute (per process, see above)."""
    now = time.monotonic()
    with _DIAL_LOCK:
        times = _DIAL_TIMES[str(user_id)]
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= DIALS_PER_MINUTE:
            return False
        times.append(now)
        for key in [k for k, v in _DIAL_TIMES.items() if not v]:  # no key outlives its last dial
            del _DIAL_TIMES[key]
        return True


def issue(db: Session, backend: BaseConsoleBackend, *, user_id: uuid.UUID, tenant_id: uuid.UUID, rng: Range,
          vm: dict[str, Any], view_only: bool, observed: bool, base: str, adapter_view_only: bool = False,
          audience: str = STUDENT, class_id: uuid.UUID | None = None, event_id: uuid.UUID | None = None,
          lab_session_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Store a grant for ``user_id`` on one VM of ``rng`` and return the adapter's ``open``
    answer (``kind`` ``rfb-ws``, ``url`` ``<base>/ws/console/<token>``). The caller commits.
    Nothing is stored when the adapter refuses."""
    now = _now()
    # One issue at a time per user: on PostgreSQL the user's row is locked until the caller
    # commits, so two requests cannot both count 3 and both insert. SQLite (tests, single
    # process) has no row locks: there the cap is best effort.
    db.query(User.id).filter(User.id == user_id).with_for_update().first()
    # Replace, do not stack: grants for this VM that no socket ever used or holds are dropped,
    # so reopening a console page four times does not lock the Student out for five minutes.
    db.query(ConsoleGrant).filter(
        ConsoleGrant.user_id == user_id, ConsoleGrant.tenant_id == tenant_id,
        ConsoleGrant.range_id == rng.id, ConsoleGrant.vm_name == str(vm.get("name") or ""),
        ConsoleGrant.dials == 0, or_(ConsoleGrant.live_until.is_(None), ConsoleGrant.live_until < now),
    ).delete(synchronize_session=False)
    # Live: a grant that can still open a socket, or one a socket holds right now.
    live = (db.query(ConsoleGrant)
            .filter(ConsoleGrant.user_id == user_id, ConsoleGrant.tenant_id == tenant_id,
                    or_(ConsoleGrant.live_until > now,
                        (ConsoleGrant.expires_at > now) & (ConsoleGrant.dials < MAX_DIALS)))
            .count())
    if live >= MAX_LIVE_GRANTS:
        raise GrantRefusedError(f"You already have {MAX_LIVE_GRANTS} consoles open; close one first")
    token = secrets.token_urlsafe(32)  # 256 bits
    url = f"{base}/ws/console/{token}"
    open_ = backend.open_view_only if adapter_view_only else backend.open
    access = open_(vm, db=db, rng=rng, relay_url=url)
    db.add(ConsoleGrant(
        audience=audience, class_id=class_id, event_id=event_id, lab_session_id=lab_session_id,
        token_hash=token_hash(token), tenant_id=tenant_id, user_id=user_id, range_id=rng.id,
        vm_id=str(vm.get("vm_id") or ""), vm_name=str(vm.get("name") or ""), view_only=view_only,
        observed=observed, created_at=now, expires_at=now + timedelta(seconds=GRANT_SECONDS)))
    # The issuing user's own lapsed grants only (no table-wide sweep in the request path), and
    # never one a socket still holds.
    db.query(ConsoleGrant).filter(
        ConsoleGrant.user_id == user_id, ConsoleGrant.tenant_id == tenant_id,
        ConsoleGrant.expires_at < now - KEEP_AFTER_EXPIRY,
        or_(ConsoleGrant.live_until.is_(None), ConsoleGrant.live_until < now),
    ).delete(synchronize_session=False)
    return access


@dataclass(frozen=True)
class Claim:
    outcome: str
    grant: ConsoleGrant | None = None
    holder: str = ""  # who holds the live claim, on OK: pass it to renew and release
    facts: Any = None  # the grant as plain values, safe after the session is gone


def _facts(g: ConsoleGrant) -> SimpleNamespace:
    return SimpleNamespace(id=g.id, tenant_id=g.tenant_id, range_id=g.range_id, vm_id=g.vm_id, vm_name=g.vm_name,
                           view_only=bool(g.view_only), audience=g.audience, class_id=g.class_id,
                           event_id=g.event_id, lab_session_id=g.lab_session_id)


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
    facts = _facts(grant)  # before the commit below: plain values for the caller
    now = _now()
    if _aware(grant.expires_at) <= now:
        return Claim(EXPIRED, grant, facts=_facts(grant))
    if grant.dials >= MAX_DIALS:
        return Claim(SPENT, grant, facts=_facts(grant))
    # One statement decides it, so two workers racing for the grant cannot both win.
    holder = secrets.token_hex(8)
    won = db.execute(
        update(ConsoleGrant)
        .where(ConsoleGrant.id == grant.id, ConsoleGrant.dials < MAX_DIALS,
               or_(ConsoleGrant.live_until.is_(None), ConsoleGrant.live_until < now))
        .values(live_until=now + LIVE_LEASE, live_by=holder),
        execution_options={"synchronize_session": False},
    ).rowcount
    db.commit()
    if won:
        return Claim(OK, grant, holder, facts)
    db.refresh(grant)  # lost: to a live socket (busy), or the last dial was taken meanwhile (spent)
    return Claim(SPENT if grant.dials >= MAX_DIALS else BUSY, grant, facts=facts)


def record_dial(db: Session, grant_id: uuid.UUID, holder: str) -> None:
    """Count a successful dial against the grant's ``MAX_DIALS``."""
    db.execute(
        update(ConsoleGrant).where(ConsoleGrant.id == grant_id, ConsoleGrant.live_by == holder)
        .values(dials=ConsoleGrant.dials + 1),
        execution_options={"synchronize_session": False},
    )
    db.commit()


def renew(db: Session, grant_id: uuid.UUID, holder: str) -> str:
    """Extend the claim ``holder`` took: ``RENEWED``; ``LOST`` when it no longer holds it (it
    lapsed and another socket took the grant); ``GONE`` when the grant row was deleted."""
    won = db.execute(
        update(ConsoleGrant).where(ConsoleGrant.id == grant_id, ConsoleGrant.live_by == holder)
        .values(live_until=_now() + LIVE_LEASE),
        execution_options={"synchronize_session": False},
    ).rowcount
    db.commit()
    if won:
        return RENEWED
    # tenant-safe: existence of the pump's own grant by primary key; returns no data.
    return LOST if db.query(ConsoleGrant.id).filter(ConsoleGrant.id == grant_id).first() else GONE


def release(db: Session, grant_id: uuid.UUID, holder: str) -> None:
    """Give the grant's one socket back, if ``holder`` still has it. Idempotent."""
    db.execute(
        update(ConsoleGrant).where(ConsoleGrant.id == grant_id, ConsoleGrant.live_by == holder)
        .values(live_until=None, live_by=None),
        execution_options={"synchronize_session": False},
    )
    db.commit()
