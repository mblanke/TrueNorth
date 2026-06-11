"""TrueNorth Range — Auth middleware (Keycloak OIDC JWT validation)."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth_backends import get_auth_backend
from .db import get_db
from .models import User, UserRole

# For dev/testing: skip JWT validation if set
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() == "true"

bearer_scheme = HTTPBearer(auto_error=not AUTH_DISABLED)


class TokenPayload(BaseModel):
    sub: str
    email: str = ""
    preferred_username: str = ""
    realm_access: dict = {}
    tenant_id: str = ""


class CurrentUser(BaseModel):
    """Resolved user context for request handlers."""

    id: str
    email: str
    display_name: str
    role: UserRole
    tenant_id: str
    keycloak_id: str


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
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

    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    raw_payload = await get_auth_backend().validate_token(credentials.credentials)
    token_data = TokenPayload(**raw_payload)

    # Look up user in DB by keycloak_id
    user = db.query(User).filter(User.keycloak_id == token_data.sub).first()
    if user is None:
        raise HTTPException(status_code=403, detail="User not registered in TrueNorth Range")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    return CurrentUser(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
        keycloak_id=str(user.keycloak_id),
    )


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
