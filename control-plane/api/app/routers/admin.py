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
POST   /teams                              USER_UPDATE or TEAM_MANAGE_OWN *
GET    /teams                              USER_READ
PATCH  /teams/{team_id}                    USER_UPDATE or TEAM_MANAGE_OWN *
DELETE /teams/{team_id}                    USER_UPDATE or TEAM_MANAGE_OWN *
POST   /teams/{team_id}/members            USER_UPDATE or TEAM_MANAGE_OWN *
GET    /teams/{team_id}/members            USER_READ
DELETE /teams/{team_id}/members/{user_id}  USER_UPDATE or TEAM_MANAGE_OWN *
GET    /audit-log                          AUDIT_READ
=========================================  ==========================

* TEAM_MANAGE_OWN (an instructor, #200): only a team of an exercise they run, and only
  Students as members (app/class_setup/scope.py). USER_UPDATE: any team of the tenant.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..class_setup import scope as class_scope
from ..class_setup.team_schemas import TeamFullIn, TeamFullOut, TeamUpdate
from ..db import get_db
from ..models import AuditLog, Team, TeamMembership, Tenant, User, UserRole
from ..rbac import Permission, is_platform_admin, require_any_permission, require_permission, user_has_permission
from ..schemas import (
    AuditLogOut,
    TenantIn,
    TenantOut,
    UserCreateIn,
    UserFullOut,
    UserUpdateIn,
)
from ..tenancy import get_owned

logger = logging.getLogger("truenorth.api.admin")

router = APIRouter(tags=["admin"])


def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str, detail: str = "") -> None:
    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id),
            tenant_id=uuid.UUID(user.tenant_id),
            action=action,
            resource_type=rtype,
            resource_id=rid,
            detail=detail,
        )
    )


# -- Tenants ----------------------------------------------------------------
@router.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(
    body: TenantIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TENANT_CREATE)),
) -> Tenant:
    """Create a new tenant.  **Permission: tenant:create**, platform administrator only"""
    if not is_platform_admin(user, db):
        raise HTTPException(403, "Only the platform administrator can create tenants")
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
    """List tenants: every tenant for the platform administrator, otherwise the caller's own.
    **Permission: tenant:read**"""
    q = db.query(Tenant)
    if not is_platform_admin(user, db):
        q = q.filter(Tenant.id == uuid.UUID(user.tenant_id))
    return q.order_by(Tenant.created_at.desc()).all()


@router.put("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.TENANT_UPDATE)),
) -> Tenant:
    """Update a tenant: the caller's own, or any for the platform administrator.
    **Permission: tenant:update**"""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant or (not is_platform_admin(user, db) and str(tenant.id) != user.tenant_id):
        # 404, not 403: another tenant's existence is not the caller's business.
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
    # The fields are query parameters (UserUpdateIn as a dependency): every one the caller
    # left out arrives as None and counts as set, so exclude_unset nulled them all (a PATCH
    # without display_name was a 500). A query string cannot ask for null; omit the Nones.
    update_data = body.model_dump(exclude_none=True)
    if "role" in update_data:
        update_data["role"] = UserRole(update_data["role"])
    disables = update_data.get("is_active") is False
    demotes = "role" in update_data and update_data["role"] != UserRole.admin
    if target.is_active and target.deleted_at is None and (disables or demotes):
        # The same locked check as DELETE and the AD status sync: never the last active admin.
        from ..ad_status_sync.service import is_last_admin

        if is_last_admin(db, target):
            raise HTTPException(409, "This is the tenant's last active administrator; make someone else an "
                                     "administrator first")
    if update_data.get("is_active") and not target.is_active:
        # Re-enabled: the 90-day clock for a local account starts again (app/ad_status_sync).
        from ..ad_status_sync.last_seen import touch

        touch(db, target.id)
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
    """Delete (deactivate) a user.  **Permission: user:delete**

    409 for the tenant's last active admin: nobody would be left to re-enable anyone. The check
    locks the tenant's admin rows (app/ad_status_sync ``is_last_admin``), so two deletions at
    once, or one beside the nightly AD status sync, cannot both pass."""
    from ..ad_status_sync.service import is_last_admin

    target = get_owned(db, User, user_id, user, not_found="User not found")
    if target.is_active and is_last_admin(db, target):
        raise HTTPException(409, "This is the tenant's last active administrator; make someone else an "
                                 "administrator first")
    target.is_active = False
    db.commit()
    _audit(db, user, "delete", "user", str(user_id))
    db.commit()


# -- Teams ------------------------------------------------------------------
# Changing a team: user:update (an admin, any team of the tenant) or team:manage_own (an
# instructor, a team of an exercise they run); the second is scoped in the handler.
TEAM_WRITE = require_any_permission(Permission.USER_UPDATE, Permission.TEAM_MANAGE_OWN)


@router.post("/teams", response_model=TeamFullOut, status_code=201)
def create_team(
    body: TeamFullIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(TEAM_WRITE),
) -> Team:
    """Create a team with full schema (type, color, max_members, etc).

    **Permission: user:update, or team:manage_own** with ``exercise_id`` naming the exercise
    of your own upcoming or running session (app/class_setup/scope.py). Another tenant's
    exercise is 404; an exercise you do not run, or no exercise, is 403; a completed or
    cancelled exercise, or another instructor's running session of it, is 409. ``ou_id``
    must be an OU of your tenant and ``nation_id`` a known nation (422).
    """
    exercise_id = class_scope.authorize_new_team(db, user, body.exercise_id)
    class_scope.check_team_refs(db, user, body.ou_id, body.nation_id)
    team = Team(
        exercise_id=exercise_id,
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
    db.flush()  # the id, so the audit row goes in the same transaction as the team
    _audit(db, user, "create", "team", str(team.id), f"exercise {exercise_id}" if exercise_id else "")
    db.commit()
    db.refresh(team)
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
    user: CurrentUser = Depends(TEAM_WRITE),
) -> Team:
    """Update a team.  **Permission: user:update, or team:manage_own** for a team of an
    exercise you run. A completed or cancelled exercise's team is frozen (409, admins too).
    Audited (``update``, the fields changed).

    Until 2026-10-07 this needed only a login: a student could rename or resize
    any team in their tenant.
    """
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    class_scope.authorize_team(db, user, team)
    class_scope.refuse_frozen_team(db, user, team)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(team, field, value)
    _audit(db, user, "update", "team", str(team_id), ", ".join(sorted(changes)))
    db.commit()
    db.refresh(team)
    return team


@router.delete("/teams/{team_id}", status_code=204, response_class=Response)
def delete_team(
    team_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(TEAM_WRITE),
):
    """Delete a team and its memberships.  **Permission: user:update, or team:manage_own**
    for a team of an exercise you run that has only Students on it (staff on a team are an
    admin's to move: 403). A completed or cancelled exercise's team is frozen (409, admins
    too). Audited (``delete``, with the members removed)."""
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    class_scope.authorize_team(db, user, team)
    class_scope.refuse_frozen_team(db, user, team)
    member_ids = [m for (m,) in db.query(TeamMembership.user_id).filter(TeamMembership.team_id == team_id)]
    if member_ids and not user_has_permission(user, Permission.USER_UPDATE):
        students = db.query(User.id).filter(
            User.id.in_(member_ids), User.tenant_id == uuid.UUID(user.tenant_id), User.role == UserRole.student
        ).count()
        if students != len(set(member_ids)):
            raise HTTPException(403, "The team has staff on it; an administrator deletes it")
    db.query(TeamMembership).filter(TeamMembership.team_id == team_id).delete()
    from ..classes import teams as class_teams

    class_teams.unlink(db, team)  # a class session's team: its link goes first (#282)
    db.delete(team)
    _audit(db, user, "delete", "team", str(team_id),
           f"{team.name}; members removed: {', '.join(str(m) for m in member_ids) or 'none'}")
    db.commit()


# -- Team Membership -------------------------------------------------------
class TeamMemberAddedOut(BaseModel):
    user_id: uuid.UUID
    team_id: uuid.UUID
    role: str


class TeamMemberOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    role: str
    position: str | None = None
    joined_at: datetime | None = None


@router.post("/teams/{team_id}/members", status_code=201, response_model=TeamMemberAddedOut)
def add_team_member(
    team_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Query(...),
    role: str = Query("member"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(TEAM_WRITE),
) -> dict:
    """Add a user to a team.  **Permission: user:update** (anyone in the tenant), **or
    team:manage_own** (a Student enrolled in your session's course, to a team of an
    exercise you run). Never to a completed or cancelled exercise's team (409): membership
    opens that run's after-action report."""
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    class_scope.authorize_team(db, user, team)
    class_scope.team_member_target(db, user, team, user_id)
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
    _audit(db, user, "add_member", "team", str(team_id), f"user {user_id} as {role}")
    db.commit()
    return {"user_id": str(user_id), "team_id": str(team_id), "role": role}


@router.get("/teams/{team_id}/members", response_model=list[TeamMemberOut])
def list_team_members(
    team_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_READ)),
) -> list[dict]:
    """List members of a team.  **Permission: user:read**

    A member whose account was deleted (or is not in the caller's tenant) is listed as
    "Unknown" with no email; before 2026-10-12 one such row made the whole list 404."""
    get_owned(db, Team, team_id, user, not_found="Team not found")
    memberships = db.query(TeamMembership).filter(TeamMembership.team_id == team_id).all()
    ids = [m.user_id for m in memberships]
    people = {
        u.id: u
        for u in db.query(User).filter(
            User.id.in_(ids), User.tenant_id == uuid.UUID(user.tenant_id), User.deleted_at.is_(None)
        )
    } if ids else {}
    results = []
    for m in memberships:
        u = people.get(m.user_id)
        results.append(
            {
                "user_id": m.user_id,
                "display_name": u.display_name if u else "Unknown",
                "email": u.email if u else "",
                "role": m.role,
                "position": m.position,
                "joined_at": m.joined_at,
            }
        )
    return results


@router.delete("/teams/{team_id}/members/{member_user_id}", status_code=204, response_class=Response)
def remove_team_member(
    team_id: uuid.UUID = Path(...),
    member_user_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(TEAM_WRITE),
):
    """Remove a user from a team.  **Permission: user:update** (anyone), **or
    team:manage_own** (a Student, from a team of an exercise you run). A completed or
    cancelled exercise's team is frozen (409, admins too): removing a member would take
    their own run's AAR from them."""
    # The membership row has no tenant of its own; without this check a caller could
    # strip members from another tenant's team by id.
    team = get_owned(db, Team, team_id, user, not_found="Team not found")
    class_scope.authorize_team(db, user, team)
    class_scope.refuse_frozen_team(db, user, team)
    if not user_has_permission(user, Permission.USER_UPDATE):
        class_scope.own_student(db, user, member_user_id)  # staff on a team are an admin's to move
    m = (
        db.query(TeamMembership)
        .filter(TeamMembership.team_id == team_id, TeamMembership.user_id == member_user_id)
        .first()
    )
    if not m:
        raise HTTPException(404, "Membership not found")
    db.delete(m)
    _audit(db, user, "remove_member", "team", str(team_id), f"user {member_user_id}")
    db.commit()


# -- Audit Log --------------------------------------------------------------
@router.get("/audit-log", response_model=list[AuditLogOut])
def list_audit_log(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AUDIT_READ)),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    tenant_id: uuid.UUID | None = Query(
        None, description="Platform administrator only: restrict to one tenant's entries."
    ),
) -> list[AuditLog]:
    """Query the audit log.  **Permission: audit:read**

    Scoped to the caller's tenant; legacy rows with no tenant are not shown. Only the
    platform administrator (``rbac.is_platform_admin``: an admin of PLATFORM_TENANT_ID's
    tenant, or of a single-tenant install) sees every tenant's entries and the legacy
    rows, and may narrow them with ``tenant_id``. Holding ``tenant:read`` is not enough:
    every tenant's admin holds it (the bug fixed in v1.2.0).
    """
    q = db.query(AuditLog)
    if is_platform_admin(user, db):
        if tenant_id is not None:
            q = q.filter(AuditLog.tenant_id == tenant_id)
    else:
        if tenant_id is not None and str(tenant_id) != user.tenant_id:
            raise HTTPException(403, "Only the platform administrator reads another tenant's audit log")
        q = q.filter(AuditLog.tenant_id == uuid.UUID(user.tenant_id))
    return q.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit).all()


# ── Roster import ───────────────────────────────────────────────────────
# Upload ceiling, mirroring courses.MAX_CSV_BYTES.
MAX_ROSTER_BYTES = 4 * 1024 * 1024

ROSTER_REQUIRED = ("email",)
ROSTER_FIELDS = (
    "email",
    "display_name",
    "first_name",
    "last_name",
    "rank",
    "service_branch",
    "unit",
    "callsign",
    "role",
)


@router.post("/users/import-csv")
async def import_roster_csv(
    file: UploadFile,
    dry_run: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.USER_CREATE)),
) -> dict:
    """Pre-provision a cohort from a CSV roster.  **Permission: user:create**

    For a course that must exist in TrueNorth before anyone signs in. Rows are
    created with ``source='csv_import'`` and a placeholder ``keycloak_id``; when
    that person later signs in and their registration is approved, the approval
    path **adopts** the existing row rather than creating a duplicate (see
    ``routers/registration._approve_one``).

    Idempotent on email: re-uploading the same roster updates rather than
    duplicating, and never touches a row whose identity is already linked to a
    real directory account.

    Required column: ``email``. Optional: display_name, first_name, last_name,
    rank, service_branch, unit, callsign, role.
    """
    raw = await file.read()
    if len(raw) > MAX_ROSTER_BYTES:
        raise HTTPException(413, "roster too large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f"roster must be UTF-8 CSV: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(422, "roster CSV has no header row")
    missing = [c for c in ROSTER_REQUIRED if c not in reader.fieldnames]
    if missing:
        raise HTTPException(422, f"roster is missing required column(s): {', '.join(missing)}")

    tenant_id = uuid.UUID(user.tenant_id)
    created = updated = skipped = 0
    errors: list[dict] = []
    # The tenant's active admins, locked once before any row is written (the same lock, in the
    # same order, as PATCH/DELETE /users and the AD status sync): no row may demote the last.
    from ..ad_status_sync.service import lock_active_admins

    active_admins = lock_active_admins(db, tenant_id)

    for line_no, row in enumerate(reader, start=2):
        email = (row.get("email") or "").strip().lower()
        if not email:
            errors.append({"line": line_no, "error": "blank email"})
            continue

        role_raw = (row.get("role") or "student").strip() or "student"
        try:
            role = UserRole(role_raw)
        except ValueError:
            errors.append({"line": line_no, "email": email, "error": f"unknown role '{role_raw}'"})
            continue

        existing = db.query(User).filter(User.email == email).first()

        # Never overwrite an account already linked to a directory identity —
        # a roster upload must not be able to re-point or downgrade a live user.
        if existing is not None and existing.source not in ("local", "csv_import"):
            skipped += 1
            errors.append(
                {
                    "line": line_no,
                    "email": email,
                    "error": f"already linked to identity source '{existing.source}' — not modified",
                }
            )
            continue

        # Email is unique platform-wide. Matching another tenant's row would move that
        # person into this tenant (target.tenant_id below) and overwrite their role.
        if existing is not None and str(existing.tenant_id) != str(tenant_id):
            skipped += 1
            errors.append({"line": line_no, "email": email, "error": "email is in use by another account — not modified"})
            continue

        # Never the tenant's last active admin: a row may not demote them (moving them to another
        # tenant cannot happen here: rows of other tenants are skipped above, and every row lands
        # in the caller's tenant). Counted against the admins locked before the loop, less those
        # earlier rows of this file demoted.
        if existing is not None and existing.id in active_admins and role != UserRole.admin:
            if not (active_admins - {existing.id}):
                skipped += 1
                errors.append({"line": line_no, "email": email,
                               "error": "the tenant's last active administrator — role not changed"})
                continue
            active_admins.discard(existing.id)

        fields = {
            "display_name": (row.get("display_name") or "").strip() or email.split("@")[0],
            "first_name": (row.get("first_name") or "").strip() or None,
            "last_name": (row.get("last_name") or "").strip() or None,
            "rank": (row.get("rank") or "").strip() or None,
            "service_branch": (row.get("service_branch") or "").strip() or None,
            "unit": (row.get("unit") or "").strip() or None,
            "callsign": (row.get("callsign") or "").strip() or None,
        }

        if existing is None:
            target = User(
                email=email,
                # A placeholder until the real subject arrives at approval. It
                # must be unique, because keycloak_id carries a unique index.
                keycloak_id=f"csv-import:{uuid.uuid4()}",
                tenant_id=tenant_id,
                source="csv_import",
                display_name=fields["display_name"],
            )
            db.add(target)
            created += 1
        else:
            target = existing
            updated += 1

        for key, value in fields.items():
            if value is not None or key == "display_name":
                setattr(target, key, value)
        target.role = role
        target.tenant_id = tenant_id

    if dry_run:
        db.rollback()
    else:
        db.flush()
        _audit(db, user, "import", "user_roster", f"{created}+{updated}")
        db.commit()

    return {
        "dry_run": dry_run,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "note": (
            "Imported rows are placeholders. Each person still signs in with their "
            "directory credentials and is admitted through the approval queue, which "
            "links their real identity to the row created here."
        ),
    }
