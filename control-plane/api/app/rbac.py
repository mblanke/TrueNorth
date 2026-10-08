"""TrueNorth Range — Fine-grained RBAC enforcement.

Extends the role-based ``require_role`` guard in ``auth.py`` with a
permission-level model.  Each :class:`Permission` maps to one or more
:class:`UserRole` values via :data:`ROLE_PERMISSIONS`.  FastAPI dependencies
(:func:`require_permission`, :func:`require_any_permission`, etc.) can be
injected into route signatures to enforce access control declaratively.
"""

from __future__ import annotations

import os
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
    # Force-release an abandoned operation's lease tombstone (app/range_ops/service.py).
    # Admin only: done wrong, a hung worker's vCenter work runs beside the next build.
    RANGE_LEASE_FORCE_RELEASE = "range:lease_force_release"

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
    # A Student's detection, judged by the server against the objective's answer key (ADR 0005)
    DETECTION_SUBMIT = "detection:submit"
    # Awarding an objective by hand. Deliberately separate from EXERCISE_COMPLETE: a
    # Student must never be able to award themselves points (ADR 0005 §4).
    OBJECTIVE_ACK = "objective:ack"

    # User management
    USER_CREATE = "user:create"
    USER_READ = "user:read"
    USER_UPDATE = "user:update"
    USER_DELETE = "user:delete"

    # Trainee registration intake. REGISTRATION_APPROVE is what actually creates
    # a user account, so it is deliberately narrower than USER_CREATE.
    REGISTRATION_READ = "registration:read"
    REGISTRATION_APPROVE = "registration:approve"

    # Infrastructure: hypervisor connections, nodes, storage, network devices,
    # and direct VM control. Distinct from the RANGE_* family, which is about
    # ranges as a product concept — every role holds RANGE_READ, and none of
    # them should imply "can see the hypervisor inventory" or "can power off a
    # virtual machine".
    INFRA_READ = "infra:read"
    INFRA_WRITE = "infra:write"
    INFRA_CONTROL = "infra:control"

    # AI backend configuration (endpoints, model routing, fleet nodes).
    AI_CONFIG_READ = "ai_config:read"
    AI_CONFIG_WRITE = "ai_config:write"

    # External learning platforms (Moodle etc.). A registered platform's JWKS URL is
    # what LTI launches are verified against, so INTEGRATION_WRITE amounts to "may
    # decide which issuer can sign users in" — admin only.
    INTEGRATION_READ = "integration:read"
    INTEGRATION_WRITE = "integration:write"

    # Other people's learning records: enrolments, module progress, transcripts,
    # external activities, gradebook pushes. Everyone may read their OWN record
    # without these; they govern acting on someone else's.
    LEARNING_RECORD_READ = "learning_record:read"
    LEARNING_RECORD_WRITE = "learning_record:write"

    # Course releases. COURSE_AUTHOR uploads ARC² release candidates and reads the instructor
    # pack; COURSE_RELEASE accepts a candidate (it becomes what students get) and publishes it
    # to the learning platform.
    COURSE_AUTHOR = "course:author"
    COURSE_RELEASE = "course:release"
    # The scheduling calendar (ADR 0004). Students hold neither: they never see other
    # bookings, capacity or the timeline.
    SCHEDULE_READ = "schedule:read"
    SCHEDULE_WRITE = "schedule:write"
    # Platform-wide scheduler policy (over-capacity block/warn). Admin only: no role lists it.
    SCHEDULE_ADMIN = "schedule:admin"

    # Tenant management
    TENANT_CREATE = "tenant:create"
    TENANT_READ = "tenant:read"
    TENANT_UPDATE = "tenant:update"

    # Admin / analytics
    AUDIT_READ = "audit:read"
    TELEMETRY_READ = "telemetry:read"
    # Write events into a range's telemetry index. Not for Students: that index is what
    # detection objectives are scored against, so writing to it would award points.
    TELEMETRY_WRITE = "telemetry:write"
    STATS_READ = "stats:read"
    AAR_GENERATE = "aar:generate"
    AAR_READ = "aar:read"

    # Background noise (synthetic personas). NOISE_READ exposes ground truth — which
    # activity on the wire was synthetic — so it must never reach students.
    NOISE_READ = "noise:read"
    NOISE_CONTROL = "noise:control"

    # Wiki. Space visibility ("all" / "staff") is enforced on top of WIKI_READ.
    WIKI_READ = "wiki:read"
    WIKI_EDIT = "wiki:edit"
    WIKI_ADMIN = "wiki:admin"

    # Trouble tickets. TICKET_CREATE alone sees only the caller's own tickets and
    # never internal comments; TICKET_WORK is triage across the tenant.
    TICKET_CREATE = "ticket:create"
    TICKET_WORK = "ticket:work"
    TICKET_ADMIN = "ticket:admin"


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
        Permission.DETECTION_SUBMIT,
        Permission.OBJECTIVE_ACK,
        # The calendar: instructors book sessions for their classes.
        Permission.SCHEDULE_READ,
        Permission.SCHEDULE_WRITE,
        # Users
        Permission.USER_READ,
        # Trainee intake: instructors drain the approval queue for their cohort.
        Permission.REGISTRATION_READ,
        Permission.REGISTRATION_APPROVE,
        # Read-only: an instructor should be able to see whether the platform is
        # healthy without being able to reconfigure or power-cycle it.
        Permission.INFRA_READ,
        Permission.AI_CONFIG_READ,
        Permission.INTEGRATION_READ,
        # Their cohort's records: progress, transcripts, grade passback retries.
        Permission.LEARNING_RECORD_READ,
        Permission.LEARNING_RECORD_WRITE,
        # Their courses: author release candidates, accept and publish them.
        Permission.COURSE_AUTHOR,
        Permission.COURSE_RELEASE,
        # Analytics
        Permission.STATS_READ,
        Permission.AAR_GENERATE,
        Permission.AAR_READ,
        Permission.TELEMETRY_READ,
        Permission.TELEMETRY_WRITE,
        # White cell: runs the background noise and sees its ground truth.
        Permission.NOISE_READ,
        Permission.NOISE_CONTROL,
        # Wiki + tickets
        Permission.WIKI_READ,
        Permission.WIKI_EDIT,
        Permission.TICKET_CREATE,
        Permission.TICKET_WORK,
    },
    # Range-ops: infrastructure-focused, no exercises/scenarios write
    UserRole.range_ops: {
        # Infrastructure is this role's whole purpose.
        Permission.INFRA_READ,
        Permission.INFRA_WRITE,
        Permission.INFRA_CONTROL,
        Permission.AI_CONFIG_READ,
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
        Permission.SCHEDULE_READ,
        Permission.STATS_READ,
        Permission.TELEMETRY_READ,
        Permission.TELEMETRY_WRITE,
        # Wiki + tickets: range ops works the range-support queue.
        Permission.WIKI_READ,
        Permission.WIKI_EDIT,
        Permission.TICKET_CREATE,
        Permission.TICKET_WORK,
    },
    # Student (trainee): take part in exercises. Not exercise:start / exercise:complete
    # (security sweep M3): a team exercise is run and closed by staff, and /run on a
    # completed one reset every participant's objectives. A Student's own lab has its own
    # lifecycle (app/lab_sessions), which does not use these.
    UserRole.student: {
        Permission.RANGE_READ,
        Permission.TEMPLATE_READ,
        Permission.SCENARIO_READ,
        Permission.EXERCISE_READ,
        Permission.DETECTION_SUBMIT,
        Permission.AAR_READ,
        Permission.WIKI_READ,
        Permission.TICKET_CREATE,
    },
    # Observer: read-only plus telemetry
    UserRole.observer: {
        Permission.RANGE_READ,
        Permission.TEMPLATE_READ,
        Permission.SCENARIO_READ,
        Permission.EXERCISE_READ,
        Permission.AAR_READ,
        Permission.SCHEDULE_READ,
        Permission.TELEMETRY_READ,
        Permission.WIKI_READ,
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
        # Only the platform operator skips tenant isolation: admins are per tenant.
        if is_platform_admin(user):
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
    verifies the user's ``tenant_id`` matches (the platform administrator bypasses).

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
        if is_platform_admin(user):  # admins are per tenant; only the operator crosses
            return user

        # tenant-safe: the range's tenant is compared with the caller's just below.
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


# ── Platform administration ────────────────────────────────────────────
def is_platform_admin(user: CurrentUser) -> bool:
    """An admin of the operator's own tenant, set by PLATFORM_TENANT_ID.

    Admins are per tenant. Settings that hold for every tenant at once (the shared
    cluster's over-capacity policy, running the scheduler clock by hand) belong to the
    platform operator, not to any one tenant's admin. Unset: a single-tenant install,
    where every admin is the operator.
    """
    if user.role != UserRole.admin:
        return False
    platform = os.getenv("PLATFORM_TENANT_ID", "").strip()
    return not platform or str(user.tenant_id) == platform


def require_platform_admin() -> Callable[..., Any]:
    """FastAPI dependency: the caller must be a platform administrator."""

    def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not is_platform_admin(user):
            raise HTTPException(403, "Only a platform administrator can change this: it applies to every tenant")
        return user

    return _check


# ── Helpers ────────────────────────────────────────────────────────────
def user_has_permission(user: CurrentUser, permission: Permission) -> bool:
    """Non-dependency utility to check a single permission imperatively."""
    return permission in ROLE_PERMISSIONS.get(user.role, set())


def user_permissions(user: CurrentUser) -> set[Permission]:
    """Return the full permission set for a user's role."""
    return ROLE_PERMISSIONS.get(user.role, set())


# Roles that carry authority over other people or the platform. Only an administrator
# grants them, whatever the permission arithmetic says.
STAFF_GRANTED_BY_ADMIN_ONLY = frozenset({UserRole.admin, UserRole.instructor, UserRole.range_ops})


def grant_refusal(granter: CurrentUser, role: UserRole) -> str | None:
    """Why ``granter`` may not give someone ``role``, or None when they may.

    Two rules, both required: the granted role's permissions must be a subset of the
    granter's own (nobody hands out authority they do not hold), and the staff roles
    (admin, instructor, range_ops) are granted by an administrator only. Before this, an
    instructor holding registration:approve could approve a request as ``admin``.
    """
    if role in STAFF_GRANTED_BY_ADMIN_ONLY and granter.role != UserRole.admin:
        return f"Only an administrator can grant the '{role.value}' role"
    granted = ROLE_PERMISSIONS.get(role, set())
    if not granted <= ROLE_PERMISSIONS.get(granter.role, set()):
        return f"The '{role.value}' role carries permissions you do not hold"
    return None
