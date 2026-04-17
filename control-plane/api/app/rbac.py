"""TrueNorth Range — Fine-grained RBAC enforcement.

Extends the role-based ``require_role`` guard in ``auth.py`` with a
permission-level model.  Each :class:`Permission` maps to one or more
:class:`UserRole` values via :data:`ROLE_PERMISSIONS`.  FastAPI dependencies
(:func:`require_permission`, :func:`require_any_permission`, etc.) can be
injected into route signatures to enforce access control declaratively.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from enum import Enum
from typing import Any

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from .auth import CurrentUser, get_current_user
from .db import get_db
from .models import Range, UserRole


# ── Permission Enum ────────────────────────────────────────────────────
class Permission(str, Enum):
    """Granular permission tokens used throughout the API."""

    # Range management
    RANGE_CREATE = "range:create"
    RANGE_READ = "range:read"
    RANGE_UPDATE = "range:update"
    RANGE_DELETE = "range:delete"
    RANGE_PROVISION = "range:provision"
    RANGE_DESTROY = "range:destroy"
    RANGE_BATCH_PROVISION = "range:batch_provision"

    # Template management
    TEMPLATE_CREATE = "template:create"
    TEMPLATE_READ = "template:read"
    TEMPLATE_UPDATE = "template:update"
    TEMPLATE_DELETE = "template:delete"

    # Scenario management
    SCENARIO_CREATE = "scenario:create"
    SCENARIO_READ = "scenario:read"
    SCENARIO_UPDATE = "scenario:update"
    SCENARIO_DELETE = "scenario:delete"

    # Exercise management
    EXERCISE_CREATE = "exercise:create"
    EXERCISE_READ = "exercise:read"
    EXERCISE_START = "exercise:start"
    EXERCISE_COMPLETE = "exercise:complete"
    EXERCISE_PAUSE = "exercise:pause"

    # User management
    USER_CREATE = "user:create"
    USER_READ = "user:read"
    USER_UPDATE = "user:update"
    USER_DELETE = "user:delete"

    # Tenant management
    TENANT_CREATE = "tenant:create"
    TENANT_READ = "tenant:read"
    TENANT_UPDATE = "tenant:update"

    # Admin / analytics
    AUDIT_READ = "audit:read"
    TELEMETRY_READ = "telemetry:read"
    STATS_READ = "stats:read"
    AAR_GENERATE = "aar:generate"
    AAR_READ = "aar:read"


# ── Role → Permission Mapping ─────────────────────────────────────────
ROLE_PERMISSIONS: dict[UserRole, set[Permission]] = {
    # Admin: every permission that exists
    UserRole.admin: set(Permission),
    # Instructor: full range lifecycle, scenarios, exercises, own-tenant users
    UserRole.instructor: {
        # Ranges
        Permission.RANGE_CREATE,
        Permission.RANGE_READ,
        Permission.RANGE_UPDATE,
        Permission.RANGE_PROVISION,
        Permission.RANGE_DESTROY,
        Permission.RANGE_BATCH_PROVISION,
        # Templates
        Permission.TEMPLATE_READ,
        Permission.TEMPLATE_CREATE,
        # Scenarios
        Permission.SCENARIO_CREATE,
        Permission.SCENARIO_READ,
        Permission.SCENARIO_UPDATE,
        # Exercises
        Permission.EXERCISE_CREATE,
        Permission.EXERCISE_READ,
        Permission.EXERCISE_START,
        Permission.EXERCISE_COMPLETE,
        Permission.EXERCISE_PAUSE,
        # Users
        Permission.USER_READ,
        # Analytics
        Permission.STATS_READ,
        Permission.AAR_GENERATE,
        Permission.AAR_READ,
        Permission.TELEMETRY_READ,
    },
    # Range-ops: infrastructure-focused, no exercises/scenarios write
    UserRole.range_ops: {
        Permission.RANGE_CREATE,
        Permission.RANGE_READ,
        Permission.RANGE_UPDATE,
        Permission.RANGE_DELETE,
        Permission.RANGE_PROVISION,
        Permission.RANGE_DESTROY,
        Permission.RANGE_BATCH_PROVISION,
        Permission.TEMPLATE_READ,
        Permission.TEMPLATE_CREATE,
        Permission.TEMPLATE_UPDATE,
        Permission.SCENARIO_READ,
        Permission.EXERCISE_READ,
        Permission.STATS_READ,
        Permission.TELEMETRY_READ,
    },
    # Student (trainee): consume ranges, run exercises
    UserRole.student: {
        Permission.RANGE_READ,
        Permission.TEMPLATE_READ,
        Permission.SCENARIO_READ,
        Permission.EXERCISE_READ,
        Permission.EXERCISE_START,
        Permission.EXERCISE_COMPLETE,
        Permission.AAR_READ,
    },
    # Observer: read-only plus telemetry
    UserRole.observer: {
        Permission.RANGE_READ,
        Permission.TEMPLATE_READ,
        Permission.SCENARIO_READ,
        Permission.EXERCISE_READ,
        Permission.AAR_READ,
        Permission.TELEMETRY_READ,
    },
}


# ── Dependency Factories ──────────────────────────────────────────────
def require_permission(*permissions: Permission) -> Callable[..., Any]:
    """FastAPI dependency — user must hold **all** listed permissions.

    Usage::

        @router.post("/ranges", dependencies=[Depends(require_permission(Permission.RANGE_CREATE))])
        def create_range(...): ...
    """

    async def _check(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        user_perms = ROLE_PERMISSIONS.get(user.role, set())
        missing = [p for p in permissions if p not in user_perms]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permissions: {[p.value for p in missing]}",
            )
        return user

    return _check


def require_any_permission(*permissions: Permission) -> Callable[..., Any]:
    """FastAPI dependency — user must hold **at least one** of the listed permissions.

    Usage::

        @router.get("/ranges", dependencies=[Depends(require_any_permission(Permission.RANGE_READ, Permission.STATS_READ))])
    """

    async def _check(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        user_perms = ROLE_PERMISSIONS.get(user.role, set())
        if not any(p in user_perms for p in permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires at least one of: {[p.value for p in permissions]}",
            )
        return user

    return _check


def require_tenant_access(tenant_id_param: str = "tenant_id") -> Callable[..., Any]:
    """FastAPI dependency — ensures the user belongs to the specified tenant.

    Admins bypass the check and may access any tenant.  The *tenant_id_param*
    is the **name of the path/query parameter** that carries the tenant UUID.

    Usage::

        @router.get("/tenants/{tenant_id}/users")
        def list_tenant_users(
            tenant_id: uuid.UUID,
            user: CurrentUser = Depends(require_tenant_access("tenant_id")),
        ): ...
    """

    async def _check(
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
        **kwargs: Any,
    ) -> CurrentUser:
        # Admins skip tenant isolation
        if user.role == UserRole.admin:
            return user

        # Resolve the target tenant_id from path params via FastAPI injection
        # This function is meant to be used alongside a path parameter of the
        # same name; FastAPI will inject it as a keyword argument.
        target_tenant_id: str | None = kwargs.get(tenant_id_param)
        if target_tenant_id is not None and str(target_tenant_id) != user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this tenant's resources",
            )
        return user

    return _check


def require_range_access() -> Callable[..., Any]:
    """FastAPI dependency — ensures the user has access to the range's tenant.

    Expects a ``range_id`` path parameter.  Looks up the range in the DB and
    verifies the user's ``tenant_id`` matches (admins bypass).

    Usage::

        @router.post("/ranges/{range_id}/provision")
        def provision(
            range_id: uuid.UUID,
            user: CurrentUser = Depends(require_range_access()),
        ): ...
    """

    async def _check(
        range_id: uuid.UUID = Path(...),
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> CurrentUser:
        if user.role == UserRole.admin:
            return user

        rng = db.query(Range).filter(Range.id == range_id).first()
        if rng is None:
            raise HTTPException(status_code=404, detail="Range not found")
        if rng.tenant_id is not None and str(rng.tenant_id) != user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this range",
            )
        return user

    return _check


# ── Helpers ────────────────────────────────────────────────────────────
def user_has_permission(user: CurrentUser, permission: Permission) -> bool:
    """Non-dependency utility to check a single permission imperatively."""
    return permission in ROLE_PERMISSIONS.get(user.role, set())


def user_permissions(user: CurrentUser) -> set[Permission]:
    """Return the full permission set for a user's role."""
    return ROLE_PERMISSIONS.get(user.role, set())
