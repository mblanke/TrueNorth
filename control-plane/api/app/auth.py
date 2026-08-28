"""TrueNorth Range — Auth middleware (Keycloak OIDC JWT validation).

Two dependencies, deliberately separate:

``get_token_identity``  validates the JWT and stops. No database. This is what
                        the registration endpoints depend on, because someone
                        registering has — by definition — no user row yet.

``get_current_user``    resolves that identity to a ``users`` row, and refuses
                        anyone who does not have one.

That second refusal is load-bearing. Access is gated by *having a row at all*,
so a pending registrant is rejected by every route that depends on
``get_current_user`` — including any route written in future. Do not add a
"current user or pending" variant; it would turn a structural guarantee into a
convention that a new router can forget.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth_backends import get_auth_backend
from .db import get_db
from .models import RegistrationRequest, RegistrationStatus, User, UserRole

# For dev/testing: skip JWT validation if set
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() == "true"

bearer_scheme = HTTPBearer(auto_error=not AUTH_DISABLED)

# Response header carrying the identity state, so a client can branch without
# parsing prose out of `detail`. Absent on ordinary permission denials, which is
# how "you need to register" stays distinguishable from "you may not do that".
AUTH_STATE_HEADER = "X-TrueNorth-Auth-State"


class TokenPayload(BaseModel):
    """Claims we consume. Unknown claims are ignored, not rejected."""

    sub: str
    email: str = ""
    preferred_username: str = ""
    name: str = ""
    given_name: str = ""
    family_name: str = ""
    realm_access: dict = {}
    tenant_id: str = ""
    # AD-federated claims, supplied by the Keycloak mappers the installer
    # configures. Absent unless that federation is wired up — see
    # install/roles/tn_keycloak.
    groups: list[str] = []
    ad_object_guid: str | None = None
    ad_distinguished_name: str | None = None
    clearance_level: str = ""
    # Authentication context, used for auth-zone MFA policy.
    amr: list[str] = []
    acr: str = ""


class CurrentUser(BaseModel):
    """Resolved user context for request handlers."""

    id: str
    email: str
    display_name: str
    role: UserRole
    tenant_id: str
    keycloak_id: str


def _dev_identity() -> TokenPayload:
    """The identity assumed when AUTH_DISABLED is set."""
    return TokenPayload(
        sub="dev-admin",
        email="admin@truenorth.local",
        name="Dev Admin",
        preferred_username="dev-admin",
    )


async def get_token_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> TokenPayload:
    """Validate the bearer token and return its claims. Never touches the DB.

    Raises 401 — and only 401 — for anything wrong with the token itself, so a
    client can treat 401 as "refresh or re-login" without ambiguity.
    """
    if AUTH_DISABLED:
        return _dev_identity()

    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    raw_payload = await get_auth_backend().validate_token(credentials.credentials)
    return TokenPayload(**raw_payload)


def _unregistered_error(db: Session, identity: TokenPayload) -> HTTPException:
    """Build the 403 for a valid token with no user row.

    Distinguishes "you have not registered", "your request is waiting", and
    "your request was declined" — three states a client must route on
    differently, and which were previously one indistinguishable message.
    """
    request_row = (
        db.query(RegistrationRequest)
        .filter(RegistrationRequest.keycloak_id == identity.sub)
        .order_by(RegistrationRequest.submitted_at.desc())
        .first()
    )

    if request_row is not None and request_row.status == RegistrationStatus.pending:
        state, code, message = (
            "pending",
            "registration_pending",
            "Your registration is awaiting instructor approval",
        )
    elif request_row is not None and request_row.status == RegistrationStatus.rejected:
        state, code, message = (
            "rejected",
            "registration_rejected",
            "Your registration request was declined",
        )
    else:
        state, code, message = (
            "unregistered",
            "registration_required",
            "No TrueNorth account for this identity — registration required",
        )

    return HTTPException(
        status_code=403,
        detail={"code": code, "state": state, "message": message, "next": "/auth/me"},
        headers={AUTH_STATE_HEADER: state},
    )


async def get_current_user(
    request: Request,
    identity: Annotated[TokenPayload, Depends(get_token_identity)],
    db: Session = Depends(get_db),
) -> CurrentUser:
    """Extract and validate user from JWT, resolve from DB."""
    if AUTH_DISABLED:
        # Dev mode: return a default admin user
        return CurrentUser(
            id="00000000-0000-0000-0000-000000000001",
            email="admin@truenorth.local",
            display_name="Dev Admin",
            role=UserRole.admin,
            tenant_id="00000000-0000-0000-0000-000000000001",
            keycloak_id="dev-admin",
        )

    # Look up user in DB by keycloak_id
    user = db.query(User).filter(User.keycloak_id == identity.sub).first()
    if user is None:
        raise _unregistered_error(db, identity)
    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_disabled",
                "state": "disabled",
                "message": "User account is disabled",
            },
            headers={AUTH_STATE_HEADER: "disabled"},
        )

    return CurrentUser(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
        keycloak_id=str(user.keycloak_id),
    )


def client_ip(request: Request) -> str | None:
    """Best-effort client address, honouring the proxy header nginx sets."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def require_role(*roles: UserRole):
    """Dependency that checks the current user has one of the required roles."""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {user.role} not authorized. Required: {[r.value for r in roles]}",
            )
        return user

    return _check
