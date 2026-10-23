"""Who must sign the rules of use before doing anything else (ADR 0014 decision 3).

A user must sign when a version has been published and any of these is true:

1. they have never signed;
2. a newer version has been published since their last signature;
3. their last signature of the current version is more than ``VALID_DAYS`` (365) days old.

All three reduce to one test: there is no signature by this user, in their current tenant,
of the **current** version made within the last 365 days. (A user moved to another tenant
signs again, so each organisation's export holds everyone it has.) Before any version is published (a first
install), nobody must sign.

Enforced on every request by ``auth.get_current_user``, after the account checks
(roll-call admission happens in ``GET /auth/me`` before any user row exists; then
disabled; then MFA; then this). ``get_signed_in_user`` stops before this check: only the
acknowledgement endpoints use it, so someone who must sign can read the text and sign.

Exempt, and why:

* ``tn_session == "inprocess"``: the in-process harness (scripts/_inprocess_api.py), the
  break-glass path when Keycloak is down. It needs root and Docker on the host and is
  audited per session; a published version must never lock the operator out of recovery.
* ``AUTH_DISABLED`` (dev, e2e and CI lanes, refused in production): ``get_current_user``
  returns the dev admin before any check, as it does for MFA.

Not exempt: LTI session hand-offs (a Student launched from Moodle signs on the same page,
then goes on to what they launched) and every Keycloak sign-in, platform administrators
included. An LTI launch checks this rule itself before it writes or opens anything
(``routers/integrations``): a resource-link launch records no launch, creates no cmi5
registration or exercise link, provisions no lab and mints no lab access token until the
user has signed, and goes to the form instead (returning to a quiz or course afterwards);
a deep-linking launch shows "Sign the rules of use in TrueNorth first" instead of the
content picker. Only the just-in-time account itself is created first, as roll-call
admission creates one.

Known exemptions (the per-request check never sees them; accepted, listed here so they
are not forgotten):

* **Lab access tokens issued earlier.** An ``X-Lab-Token`` minted before a new version was
  published (or before a signature expired) keeps opening its one lab session until it
  expires (at most the lab's own lifetime). New ones are minted only after signing.
* **Open WebSockets.** The check runs at the handshake; a socket opened before a publish
  stays open until its token expires (``ws_auth.close_expired``).
* **The calendar feed** (``/schedule/feed/{token}.ics``): calendar clients cannot sign;
  it serves only the token owner's own sessions, read-only.
* Also outside it by design: noise-agent tokens (no user), cmi5 AU fetch and LRS calls
  (per-registration credentials, issued by a gated launch) and the LTI tool endpoints.
  tests/api/test_acknowledgement_279.py lists every such route and fails on a new one.

Break-glass when the published text itself is the problem: docs/runbooks/break-glass.md
("Rules of use") and scripts/acknowledgement_breakglass_inprocess.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from .models import AcknowledgementSignature, AcknowledgementVersion

VALID_DAYS = 365
EXEMPT_SESSIONS = frozenset({"inprocess"})
ACKNOWLEDGEMENT_REQUIRED_DETAIL = {
    "code": "acknowledgement_required",
    "state": "acknowledgement_required",
    "message": "Read and agree to the rules of use before you continue",
    "next": "/acknowledgement/current",
}


def now() -> datetime:
    """The clock this rule reads. Tests move it (time travel)."""
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; every value written here is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Status:
    required: bool
    current: AcknowledgementVersion | None
    signed_at: datetime | None  # the user's latest signature of the current version
    expires_at: datetime | None  # when that signature stops counting
    reason: str  # none_published | never_signed | new_version | expired | signed


def current_version(db: Session) -> AcknowledgementVersion | None:
    """The published version with the highest number, or None before the first publish."""
    return (
        db.query(AcknowledgementVersion)
        .filter(AcknowledgementVersion.published_at.isnot(None))
        .order_by(AcknowledgementVersion.version.desc())
        .first()
    )


def status(db: Session, user_id: uuid.UUID | str, tenant_id: uuid.UUID | str, at: datetime | None = None) -> Status:
    at = at or now()
    uid, tid = uuid.UUID(str(user_id)), uuid.UUID(str(tenant_id))
    current = current_version(db)
    if current is None:
        return Status(False, None, None, None, "none_published")
    latest = (
        db.query(AcknowledgementSignature.signed_at)
        .filter(AcknowledgementSignature.user_id == uid, AcknowledgementSignature.tenant_id == tid,
                AcknowledgementSignature.version_id == current.id)
        .order_by(AcknowledgementSignature.signed_at.desc())
        .first()
    )
    if latest is None:
        ever = (
            db.query(AcknowledgementSignature.id)
            .filter(AcknowledgementSignature.user_id == uid, AcknowledgementSignature.tenant_id == tid)
            .first()
        )
        return Status(True, current, None, None, "new_version" if ever else "never_signed")
    signed_at = aware(latest[0])
    expires_at = signed_at + timedelta(days=VALID_DAYS)
    if at >= expires_at:
        return Status(True, current, signed_at, expires_at, "expired")
    return Status(False, current, signed_at, expires_at, "signed")


def is_exempt(identity: object) -> bool:
    return (getattr(identity, "tn_session", "") or "") in EXEMPT_SESSIONS


def required(db: Session, user_id: uuid.UUID | str, tenant_id: uuid.UUID | str, identity: object) -> bool:
    """Whether this request's user must sign before going on (exemptions applied)."""
    if is_exempt(identity):
        return False
    return status(db, user_id, tenant_id).required


def denial(db: Session, user_id: uuid.UUID | str, tenant_id: uuid.UUID | str, identity: object):
    """The 403 to raise, or None. FastAPI is imported lazily, as in mfa_policy."""
    if not required(db, user_id, tenant_id, identity):
        return None
    from fastapi import HTTPException

    from ..auth import AUTH_STATE_HEADER

    return HTTPException(status_code=403, detail=dict(ACKNOWLEDGEMENT_REQUIRED_DETAIL),
                         headers={AUTH_STATE_HEADER: "acknowledgement_required"})
