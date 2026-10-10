"""The LTI session hand-off (gap #1 in docs/moodle-integration.md).

A Student who arrives from an LMS with no TrueNorth sign-in of their own (the launch
created their account: ``keycloak_id`` ``lti:<platform id>:<sub>``) still needs the SPA,
which every API call authenticates. Keycloak federation would mean creating Keycloak
users and then signing them in without a credential, which needs impersonation or token
exchange: the API's Keycloak account holds ``manage-users`` only, and widening it to
impersonate anyone is a bigger hole than the one it closes. So TrueNorth hands off itself:

1. ``/lti/launch`` stores a hand-off (single use, two minutes, the target SPA path) and
   redirects to ``/lti/session#code=<code>``. The fragment never reaches a server log or a
   Referer. With ``LTI_REQUIRE_STATE_COOKIE`` on (the default) the code is also bound to an
   HttpOnly cookie set on that launch, so a code posted into another browser is useless.
2. The SPA posts the code to ``/lti/session`` and receives a session token: RS256 with the
   tool key, ``typ`` "lti-session", audience ``truenorth-lti-session``, subject the
   account's ``keycloak_id``, at most ``LTI_SESSION_SECONDS`` (2 h) and not renewable: a
   new launch gives a new one.
3. ``app.auth.get_token_identity`` accepts that token in place of a Keycloak one. It is
   re-checked on every request: the account must still be an active Student created by an
   LTI launch, in the token's tenant, from a platform still registered and active. It can
   never be a staff account or one with a Keycloak sign-in: those sign in with Keycloak.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import lti13
from ..models import ExternalPlatform, User, UserRole
from .models import LTIHandoff

ISSUER = "truenorth"
TYP = "lti-session"
AUDIENCE = "truenorth-lti-session"
LTI_KC_PREFIX = "lti:"
HANDOFF_SECONDS = 120
HANDOFF_COOKIE = "tn_lti_handoff"


class HandoffError(ValueError):
    """The code is unknown, spent, expired, or presented by another browser (401)."""


class SessionTokenError(ValueError):
    """Not a valid LTI session token for an LTI-created Student (401)."""


def session_seconds() -> int:
    try:
        return max(300, min(int(os.getenv("LTI_SESSION_SECONDS", "7200")), 8 * 3600))
    except ValueError:
        return 7200


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def needs_handoff(user: User) -> bool:
    """Only an LTI-created Student: everyone else has (or is) a Keycloak sign-in."""
    return (user.keycloak_id or "").startswith(LTI_KC_PREFIX) and user.role == UserRole.student


def mint_handoff(db: Session, user: User, platform: ExternalPlatform, target: str, *, bind: str = "") -> str:
    """Store a hand-off and return its code. ``bind``: the cookie value it is bound to."""
    if not target.startswith("/") or target.startswith("//"):
        raise ValueError("a hand-off target is an SPA path")
    code = secrets.token_urlsafe(32)
    db.add(
        LTIHandoff(
            code_hash=_hash(code),
            bind_hash=_hash(bind) if bind else "",
            user_id=user.id,
            platform_id=platform.id,
            target=target,
            expires_at=datetime.now(UTC) + timedelta(seconds=HANDOFF_SECONDS),
        )
    )
    db.commit()
    return code


def exchange(db: Session, code: str, bind: str) -> tuple[str, int, User, str]:
    """Spend a code: (session token, lifetime seconds, user, target). Single use, atomic."""
    now = datetime.now(UTC)
    # tenant-safe: unauthenticated by nature; the code is the credential (looked up by hash).
    row = db.query(LTIHandoff).filter(LTIHandoff.code_hash == _hash(code or "")).first()
    if row is None:
        raise HandoffError("unknown sign-in code; launch again from your course")
    if row.bind_hash and (not bind or not secrets.compare_digest(row.bind_hash, _hash(bind))):
        raise HandoffError("this sign-in code was issued to another browser")
    spent = db.execute(
        update(LTIHandoff)
        .where(LTIHandoff.id == row.id, LTIHandoff.used_at.is_(None), LTIHandoff.expires_at > now)
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if spent != 1:
        raise HandoffError("this sign-in code is spent or expired; launch again from your course")
    user = db.get(User, row.user_id)  # tenant-safe: the user this hand-off was minted for
    platform = db.get(ExternalPlatform, row.platform_id)  # tenant-safe: the launch's own platform
    if user is None or platform is None or not needs_handoff(user):
        raise HandoffError("this account cannot be signed in this way")
    lifetime = session_seconds()
    return mint_session_token(db, user, platform, lifetime), lifetime, user, row.target


def mint_session_token(db: Session, user: User, platform: ExternalPlatform, lifetime: int) -> str:
    now = int(time.time())
    key = lti13.get_tool_key(db)
    claims = {
        "iss": ISSUER,
        "typ": TYP,
        "aud": AUDIENCE,
        "sub": user.keycloak_id,
        "uid": str(user.id),
        "tid": str(user.tenant_id),
        "plat": str(platform.id),
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, lti13.signing_pem(key), algorithm="RS256", headers={"kid": key.kid})


def is_session_token(token: str) -> bool:
    """Whether a bearer token claims to be an LTI session (verified by ``verify``, never trusted)."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return False
    return claims.get("typ") == TYP and claims.get("iss") == ISSUER


def verify(db: Session, token: str) -> dict[str, Any]:
    """The identity claims of a valid LTI session token, re-checked against the database."""
    key = lti13.get_tool_key(db)
    public = serialization.load_pem_public_key(key.public_key_pem.encode())
    try:
        claims = jwt.decode(
            token, public, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER, options={"require": ["exp", "sub"]}
        )
    except jwt.PyJWTError as exc:
        raise SessionTokenError(f"LTI session refused: {exc}") from exc
    if claims.get("typ") != TYP or not str(claims.get("sub", "")).startswith(LTI_KC_PREFIX):
        raise SessionTokenError("not an LTI session")
    # tenant-safe: the subject is a keycloak_id this API minted; tenant is compared below.
    user = db.query(User).filter(User.keycloak_id == claims["sub"]).first()
    if (
        user is None
        or str(user.id) != str(claims.get("uid"))
        or str(user.tenant_id) != str(claims.get("tid"))
        or not user.is_active
        or user.deleted_at is not None
        or not needs_handoff(user)
    ):
        raise SessionTokenError("this LTI session's account is no longer a launched Student")
    try:
        platform_id = uuid.UUID(str(claims.get("plat")))
    except ValueError as exc:
        raise SessionTokenError("LTI session names no platform") from exc
    platform = db.get(ExternalPlatform, platform_id)  # tenant-safe: compared with the user's tenant below
    if platform is None or not platform.is_active or platform.tenant_id != user.tenant_id:
        raise SessionTokenError("the learning platform of this LTI session is no longer registered")
    return {
        "sub": user.keycloak_id,
        "email": user.email,
        "name": user.display_name,
        "preferred_username": user.display_name,
        "tn_session": "lti",
    }
