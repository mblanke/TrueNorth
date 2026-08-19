"""TrueNorth Range - Admin router.

Tenant management, user CRUD (with military fields), team CRUD (full schema),
team membership, and audit log access.

Permissions required per endpoint:

=========================================  ==========================
Endpoint                                   Permission(s)
=========================================  ==========================
POST   /tenants                            TENANT_CREATE
GET    /tenants                            TENANT_READ
PUT    /tenants/{tenant_id}                TENANT_UPDATE
GET    /users                              USER_READ
GET    /users/me                           (any authenticated)
POST   /users                              USER_CREATE
PATCH  /users/{user_id}                    USER_UPDATE
DELETE /users/{user_id}                    USER_DELETE
POST   /teams                              USER_UPDATE
GET    /teams                              USER_READ
DELETE /teams/{team_id}                    USER_UPDATE
POST   /teams/{team_id}/members            USER_UPDATE
GET    /teams/{team_id}/members            USER_READ
DELETE /teams/{team_id}/members/{user_id}  USER_UPDATE
GET    /audit-log                          AUDIT_READ
=========================================  ==========================
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..tenancy import get_owned
from ..db import get_db
from ..models import AuditLog, Team, TeamMembership, Tenant, User, UserRole
from ..rbac import Permission, require_permission
from ..schemas import (
    AuditLogOut,
    TeamFullIn,
    TeamFullOut,
    TeamUpdate,
    TenantIn,
    TenantOut,
    UserCreateIn,
    UserFullOut,
    UserUpdateIn,
)

logger = logging.getLogger("truenorth.api.admin")

router = APIRouter(tags=["admin"])


def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str) -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=rtype, resource_id=rid))


# -- Tenants ----------------------------------------------------------------
@router.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(
    body: TenantIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TENANT_CREATE)),
) -> Tenant:
    """Create a new tenant.  **Permission: tenant:create**"""
    if db.query(Tenant).filter(Tenant.slug == body.slug).first():
        raise HTTPException(409, f"Tenant slug '{body.slug}' already exists")
    tenant = Tenant(name=body.name, slug=body.slug)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    _audit(db, user, "create", "tenant", str(tenant.id))
    db.commit()
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TENANT_READ)),
) -> list[Tenant]:
    """List all tenants.  **Permission: tenant:read**"""
    return db.query(Tenant).order_by(Tenant.created_at.desc()).all()


@router.put("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TENANT_UPDATE)),
) -> Tenant:
    """Update a tenant.  **Permission: tenant:update**"""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    tenant.name = body.name
    tenant.slug = body.slug
    db.commit()
    db.refresh(tenant)
    _audit(db, user, "update", "tenant", str(tenant.id))
    db.commit()
    return tenant


# -- Users ------------------------------------------------------------------
@router.get("/users/me", response_model=UserFullOut)
def get_me(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> User:
    """Return current user profile.  **Permission: any authenticated user**"""
    u = db.query(User).filter(User.id == uuid.UUID(user.id)).first()
    if not u:
        raise HTTPException(404, "User not found")
    return u


@router.get("/users", response_model=list[UserFullOut])
def list_users(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_READ)),
) -> list[User]:
    """List users in the caller's tenant.  **Permission: user:read**"""
    return db.query(User).filter(User.tenant_id == uuid.UUID(user.tenant_id)).all()


@router.post("/users", response_model=UserFullOut, status_code=201)
def create_user(
    body: UserCreateIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_CREATE)),
) -> User:
    """Provision a new user with all military/identity fields.  **Permission: user:create**"""
    new_user = User(
        email=body.email,
        display_name=body.display_name,
        role=UserRole(body.role),
        tenant_id=uuid.UUID(user.tenant_id),
        keycloak_id=body.keycloak_id or str(uuid.uuid4()),
        first_name=body.first_name,
        last_name=body.last_name,
        rank=body.rank,
        service_branch=body.service_branch,
        nation_id=body.nation_id,
        clearance_level=body.clearance_level,
        unit=body.unit,
        callsign=body.callsign,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    _audit(db, user, "create", "user", str(new_user.id))
    db.commit()
    return new_user


@router.patch("/users/{user_id}", response_model=UserFullOut)
def update_user(
    user_id: uuid.UUID = Path(...),
    body: UserUpdateIn = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_UPDATE)),
) -> User:
    """Update a user's profile fields.  **Permission: user:update**"""
    target = get_owned(db, User, user_id, user, not_found="User not found")
    update_data = body.model_dump(exclude_unset=True)
    if "role" in update_data:
        update_data["role"] = UserRole(update_data["role"])
    for key, val in update_data.items():
        setattr(target, key, val)
    db.commit()
    db.refresh(target)
    _audit(db, user, "update", "user", str(user_id))
    db.commit()
    return target


@router.delete("/users/{user_id}", status_code=204, response_class=Response)
def delete_user(
    user_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_DELETE)),
):
    """Delete (deactivate) a user.  **Permission: user:delete**"""
    target = get_owned(db, User, user_id, user, not_found="User not found")
    target.is_active = False
    db.commit()
    _audit(db, user, "delete", "user", str(user_id))
    db.commit()


# -- Teams ------------------------------------------------------------------
@router.post("/teams", response_model=TeamFullOut, status_code=201)
def create_team(
    body: TeamFullIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_UPDATE)),
) -> Team:
    """Create a team with full schema (type, color, max_members, etc).  **Permission: user:update**"""
    team = Team(
        name=body.name,
        tenant_id=uuid.UUID(user.tenant_id),
        description=body.description,
        team_type=body.team_type,
        color_hex=body.color_hex,
        ou_id=body.ou_id,
        nation_id=body.nation_id,
        max_members=body.max_members,
        is_persistent=body.is_persistent,
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    _audit(db, user, "create", "team", str(team.id))
    db.commit()
    return team


@router.get("/teams", response_model=list[TeamFullOut])
def list_teams(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_READ)),
) -> list[Team]:
    """List teams in the caller's tenant.  **Permission: user:read**"""
    return db.query(Team).filter(Team.tenant_id == uuid.UUID(user.tenant_id)).all()


@router.patch("/teams/{team_id}", response_model=TeamFullOut)
def update_team(
    body: TeamUpdate,
    team_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> Team:
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(team, field, value)
    db.commit()
    db.refresh(team)
    return team


@router.delete("/teams/{team_id}", status_code=204, response_class=Response)
def delete_team(
    team_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_UPDATE)),
):
    """Delete a team and its memberships.  **Permission: user:update**"""
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    db.query(TeamMembership).filter(TeamMembership.team_id == team_id).delete()
    db.delete(team)
    db.commit()
    _audit(db, user, "delete", "team", str(team_id))
    db.commit()


# -- Team Membership -------------------------------------------------------
@router.post("/teams/{team_id}/members", status_code=201)
def add_team_member(
    team_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Query(...),
    role: str = Query("member"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_UPDATE)),
) -> dict:
    """Add a user to a team.  **Permission: user:update**"""
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    target = get_owned(db, User, user_id, user, not_found="User not found")
    existing = (
        db.query(TeamMembership).filter(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id).first()
    )
    if existing:
        raise HTTPException(409, "User already in team")
    if team.max_members:
        count = db.query(TeamMembership).filter(TeamMembership.team_id == team_id).count()
        if count >= team.max_members:
            raise HTTPException(409, f"Team is full ({team.max_members} max)")
    membership = TeamMembership(
        user_id=user_id,
        team_id=team_id,
        role=role,
        joined_at=datetime.now(UTC),
    )
    db.add(membership)
    db.commit()
    _audit(db, user, "add_member", "team", str(team_id))
    db.commit()
    return {"user_id": str(user_id), "team_id": str(team_id), "role": role}


@router.get("/teams/{team_id}/members")
def list_team_members(
    team_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_READ)),
) -> list[dict]:
    """List members of a team.  **Permission: user:read**"""
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    memberships = db.query(TeamMembership).filter(TeamMembership.team_id == team_id).all()
    results = []
    for m in memberships:
        u = get_owned(db, User, m.user_id, user)
        results.append(
            {
                "user_id": str(m.user_id),
                "display_name": u.display_name if u else "Unknown",
                "email": u.email if u else "",
                "role": m.role,
                "position": m.position,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            }
        )
    return results


@router.delete("/teams/{team_id}/members/{member_user_id}", status_code=204, response_class=Response)
def remove_team_member(
    team_id: uuid.UUID = Path(...),
    member_user_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_UPDATE)),
):
    """Remove a user from a team.  **Permission: user:update**"""
    m = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id, TeamMembership.user_id == member_user_id)
        .first()
    )
    if not m:
        raise HTTPException(404, "Membership not found")
    db.delete(m)
    db.commit()
    _audit(db, user, "remove_member", "team", str(team_id))
    db.commit()


# -- Audit Log --------------------------------------------------------------
@router.get("/audit-log", response_model=list[AuditLogOut])
def list_audit_log(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AUDIT_READ)),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
) -> list[AuditLog]:
    """Query the audit log.  **Permission: audit:read**"""
    return db.query(AuditLog).order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit).all()
