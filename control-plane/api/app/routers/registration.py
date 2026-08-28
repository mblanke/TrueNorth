"""Trainee registration and instructor approval.

The account lifecycle this implements::

    AD credentials -> Keycloak (LDAPS federation)
      -> authenticated, but no TrueNorth account
      -> POST /registration           (what AD does not hold)
      -> RegistrationRequest, pending
      -> instructor approves, assigning role + tenant
      -> User row + enrollments, created in ONE transaction

Approval is the only path in the codebase that creates a trainee. Nothing here
infers authority from AD group membership: groups gate *eligibility to apply*
and pre-fill the approver's screen, and that is all.

``GET /auth/me`` is the one endpoint reachable without a ``users`` row. It
depends on ``get_token_identity`` rather than ``get_current_user`` and always
answers 200 for a valid token, so the SPA can decide where to send someone
without having to interpret an error.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import identity as ident
from ..auth import CurrentUser, TokenPayload, client_ip, get_token_identity
from ..db import get_db
from ..enrollment import ensure_enrollment, ensure_path_enrollment
from ..models import (
    AuditLog,
    Course,
    Qualification,
    RegistrationRequest,
    RegistrationStatus,
    Tenant,
    User,
    UserRole,
)
from ..rbac import Permission, require_permission
from ..schemas import (
    AuthMeOut,
    RegistrationApproveIn,
    RegistrationBulkApproveIn,
    RegistrationBulkApproveOut,
    RegistrationBulkResultItem,
    RegistrationPrefill,
    RegistrationRejectIn,
    RegistrationRequestIn,
    RegistrationRequestOut,
    RegistrationSuggestions,
)

logger = logging.getLogger("truenorth.registration")

router = APIRouter(tags=["registration"])


def _audit(db: Session, user: CurrentUser, action: str, rid: str) -> None:
    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id),
            action=action,
            resource_type="registration_request",
            resource_id=rid,
        )
    )


def _open_request(db: Session, keycloak_id: str) -> RegistrationRequest | None:
    return (
        db.query(RegistrationRequest)
        .filter(
            RegistrationRequest.keycloak_id == keycloak_id,
            RegistrationRequest.status == RegistrationStatus.pending,
        )
        .first()
    )


def _latest_request(db: Session, keycloak_id: str) -> RegistrationRequest | None:
    return (
        db.query(RegistrationRequest)
        .filter(RegistrationRequest.keycloak_id == keycloak_id)
        .order_by(RegistrationRequest.submitted_at.desc())
        .first()
    )


def _suggestions(db: Session, groups: list[str]) -> RegistrationSuggestions:
    role = ident.suggest_role(groups)
    tenant = ident.suggest_tenant(db, groups)
    return RegistrationSuggestions(
        role=role.value if role else None,
        tenant_id=tenant.id if tenant else None,
        tenant_name=tenant.name if tenant else None,
        matched_groups=groups,
        may_register=ident.may_register(groups),
    )


# ══════════════════════════════════════════════════════════════════════════
# Trainee-facing — authenticated by token alone, no user row required
# ══════════════════════════════════════════════════════════════════════════


@router.get("/auth/me", response_model=AuthMeOut)
def auth_me(
    identity: TokenPayload = Depends(get_token_identity),
    db: Session = Depends(get_db),
) -> AuthMeOut:
    """Identity state for the current token. Always 200 while the token is valid."""
    user = db.query(User).filter(User.keycloak_id == identity.sub).first()
    if user is not None:
        return AuthMeOut(status="registered", user=user)

    groups = ident.normalise_groups(identity.groups)
    request_row = _latest_request(db, identity.sub)
    if request_row is not None and request_row.status == RegistrationStatus.pending:
        return AuthMeOut(status="pending", request=request_row)
    if request_row is not None and request_row.status == RegistrationStatus.rejected:
        return AuthMeOut(status="rejected", request=request_row)

    return AuthMeOut(
        status="unregistered",
        prefill=RegistrationPrefill(**ident.claims_to_prefill(identity.model_dump())),
        suggestions=_suggestions(db, groups),
    )


@router.post("/registration", response_model=RegistrationRequestOut, status_code=status.HTTP_201_CREATED)
def submit_registration(
    body: RegistrationRequestIn,
    request: Request,
    identity: TokenPayload = Depends(get_token_identity),
    db: Session = Depends(get_db),
) -> RegistrationRequest:
    """Submit a registration request for the authenticated AD identity."""
    if db.query(User).filter(User.keycloak_id == identity.sub).first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This identity already has an account")

    groups = ident.normalise_groups(identity.groups)
    if not ident.may_register(groups):
        allowed = sorted(ident.allowed_groups())
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Registration is limited to members of: {', '.join(allowed)}",
        )

    denial = ident.auth_zone_denial(db, identity.model_dump(), client_ip(request))
    if denial:
        raise HTTPException(status.HTTP_403_FORBIDDEN, denial)

    prefill = ident.claims_to_prefill(identity.model_dump())
    if not prefill["email"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The identity provider supplied no email address — check the Keycloak email claim mapper",
        )

    role = ident.suggest_role(groups)
    tenant = ident.suggest_tenant(db, groups)

    existing = _open_request(db, identity.sub)
    if existing is not None:
        # Re-submitting while pending edits the open request rather than
        # stacking duplicates in the approval queue.
        target = existing
    else:
        target = RegistrationRequest(keycloak_id=identity.sub)
        db.add(target)

    target.email = prefill["email"]
    target.display_name = prefill["display_name"] or prefill["email"]
    target.first_name = prefill["first_name"]
    target.last_name = prefill["last_name"]
    target.ad_object_guid = prefill["ad_object_guid"]
    target.ad_distinguished_name = prefill["ad_distinguished_name"]
    target.ad_groups = json.dumps(groups)
    target.rank = body.rank
    target.service_branch = body.service_branch
    target.unit = body.unit
    target.callsign = body.callsign
    target.nation_id = body.nation_id
    target.timezone = body.timezone or "UTC"
    target.requested_qualification_id = body.requested_qualification_id
    target.requested_learning_path_id = body.requested_learning_path_id
    target.requested_cohort = body.requested_cohort
    target.justification = body.justification
    target.suggested_role = role.value if role else None
    target.suggested_tenant_id = tenant.id if tenant else None
    target.status = RegistrationStatus.pending

    try:
        db.commit()
    except IntegrityError:
        # The partial unique index caught a concurrent duplicate submission.
        db.rollback()
        current = _open_request(db, identity.sub)
        if current is None:
            raise
        return current

    db.refresh(target)
    logger.info("Registration submitted by %s (%s)", target.email, identity.sub)
    return target


@router.get("/registration/mine", response_model=RegistrationRequestOut)
def my_registration(
    identity: TokenPayload = Depends(get_token_identity),
    db: Session = Depends(get_db),
) -> RegistrationRequest:
    """The caller's most recent registration request."""
    row = _latest_request(db, identity.sub)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No registration request found")
    return row


@router.delete(
    "/registration/mine",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    # No `-> None` return annotation: FastAPI would infer NoneType as a response
    # model, and a 204 may not carry a body.
    response_model=None,
)
def withdraw_registration(
    identity: TokenPayload = Depends(get_token_identity),
    db: Session = Depends(get_db),
):
    """Withdraw an open request, freeing the identity to submit a new one."""
    row = _open_request(db, identity.sub)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No open registration request")
    row.status = RegistrationStatus.withdrawn
    row.decided_at = datetime.now(UTC)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════
# Instructor / admin-facing
# ══════════════════════════════════════════════════════════════════════════


def _visible(db: Session, user: CurrentUser):
    """Requests this approver may see.

    Admins see everything. An instructor sees requests suggested into their own
    tenant, plus unassigned ones — otherwise a request whose AD groups map to no
    tenant would be invisible to everyone but an admin and would sit forever.
    """
    q = db.query(RegistrationRequest)
    if user.role == UserRole.admin:
        return q
    return q.filter(
        (RegistrationRequest.suggested_tenant_id == uuid.UUID(user.tenant_id))
        | (RegistrationRequest.suggested_tenant_id.is_(None))
    )


@router.get("/registration/requests", response_model=list[RegistrationRequestOut])
def list_requests(
    status_filter: str = Query(default="pending", alias="status"),
    cohort: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.REGISTRATION_READ)),
) -> list[RegistrationRequest]:
    """The approval queue.  **Permission: registration:read**"""
    q = _visible(db, user)
    if status_filter and status_filter != "all":
        try:
            q = q.filter(RegistrationRequest.status == RegistrationStatus(status_filter))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown status '{status_filter}'") from None
    if cohort:
        q = q.filter(RegistrationRequest.requested_cohort == cohort)
    return q.order_by(RegistrationRequest.submitted_at.asc()).offset(offset).limit(limit).all()


@router.get("/registration/requests/{request_id}", response_model=RegistrationRequestOut)
def get_request(
    request_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.REGISTRATION_READ)),
) -> RegistrationRequest:
    """**Permission: registration:read**"""
    row = _visible(db, user).filter(RegistrationRequest.id == request_id).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration request not found")
    return row


def _resolve_tenant(db: Session, req: RegistrationRequest, approver: CurrentUser, chosen: uuid.UUID | None) -> uuid.UUID:
    """Decide which tenant the new user lands in, and whether that is permitted."""
    target = chosen or req.suggested_tenant_id or uuid.UUID(approver.tenant_id)
    if approver.role != UserRole.admin and str(target) != approver.tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Approving into another tenant requires an administrator",
        )
    if db.query(Tenant).filter(Tenant.id == target).first() is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Tenant {target} does not exist")
    return target


def _enroll_for(
    db: Session,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qualification_id: uuid.UUID | None,
    learning_path_id: uuid.UUID | None,
) -> int:
    """Enroll a newly approved trainee. Returns the course count."""
    count = 0
    if learning_path_id:
        count += len(
            ensure_path_enrollment(
                db, user_id=user_id, learning_path_id=learning_path_id, tenant_id=tenant_id
            )
        )
    if qualification_id:
        # Scoped to the tenant the account is being created in, or to shared
        # catalogue content (tenant_id NULL). An approver must not be able to
        # enrol someone onto another tenant's qualification by id.
        qual = (
            db.query(Qualification)
            .filter(
                Qualification.id == qualification_id,
                (Qualification.tenant_id == tenant_id) | (Qualification.tenant_id.is_(None)),
            )
            .first()
        )
        if qual is not None:
            # tenant-safe: reached only through the qualification scoped above.
            courses = db.query(Course).filter(Course.qualification_id == qualification_id).all()
            for course in courses:
                ensure_enrollment(db, user_id=user_id, course_id=course.id, tenant_id=tenant_id)
                count += 1
    return count


def _approve_one(
    db: Session,
    req: RegistrationRequest,
    approver: CurrentUser,
    *,
    role: str,
    tenant_id: uuid.UUID | None,
    qualification_id: uuid.UUID | None,
    learning_path_id: uuid.UUID | None,
    note: str | None,
) -> User:
    """Create the user and enrollments for one approved request.

    Does not commit — the caller owns the transaction so that the user, the
    enrollments and the decision all land together or not at all.
    """
    if req.status != RegistrationStatus.pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Request is already {req.status.value}",
        )

    target_tenant = _resolve_tenant(db, req, approver, tenant_id)

    # Resolve the target row deterministically rather than by catching an
    # IntegrityError: a failed flush poisons the enclosing SAVEPOINT, which in a
    # bulk approve would take out every account already created in the batch.
    #
    # 1. Same Keycloak subject — this identity already has an account.
    user = db.query(User).filter(User.keycloak_id == req.keycloak_id).first()

    if user is None:
        # 2. Adopt a pre-provisioned row (CSV roster / local account) rather than
        #    colliding on the unique email. Restricted to sources that could not
        #    have chosen their own email address — with a self-registration IdP
        #    this would be an account-takeover vector.
        existing = db.query(User).filter(User.email == req.email).first()
        if existing is not None and existing.source not in ("local", "csv_import"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A user with email {req.email} already exists from source '{existing.source}'",
            )
        if existing is not None:
            user = existing
            user.keycloak_id = req.keycloak_id
        else:
            # 3. Genuinely new.
            user = User(
                keycloak_id=req.keycloak_id,
                email=req.email,
                display_name=req.display_name,
            )
            db.add(user)

    user.source = "ad"

    user.role = UserRole(role)
    user.tenant_id = target_tenant
    user.first_name = req.first_name
    user.last_name = req.last_name
    user.rank = req.rank
    user.service_branch = req.service_branch
    user.unit = req.unit
    user.callsign = req.callsign
    user.nation_id = req.nation_id
    user.timezone = req.timezone or "UTC"
    user.ad_object_guid = req.ad_object_guid
    user.ad_distinguished_name = req.ad_distinguished_name
    user.last_synced_at = datetime.now(UTC)
    user.is_active = True
    user.onboarding_state = "not_started"

    db.flush()

    _enroll_for(
        db,
        user_id=user.id,
        tenant_id=target_tenant,
        qualification_id=qualification_id or req.requested_qualification_id,
        learning_path_id=learning_path_id or req.requested_learning_path_id,
    )

    req.status = RegistrationStatus.approved
    req.decided_by_user_id = uuid.UUID(approver.id)
    req.decided_at = datetime.now(UTC)
    req.decision_reason = note
    req.created_user_id = user.id
    return user


@router.post("/registration/requests/{request_id}/approve", response_model=RegistrationRequestOut)
def approve_request(
    request_id: uuid.UUID,
    body: RegistrationApproveIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.REGISTRATION_APPROVE)),
) -> RegistrationRequest:
    """Approve a request, creating the account.  **Permission: registration:approve**"""
    req = _visible(db, user).filter(RegistrationRequest.id == request_id).first()
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration request not found")

    created = _approve_one(
        db,
        req,
        user,
        role=body.role,
        tenant_id=body.tenant_id,
        qualification_id=body.qualification_id,
        learning_path_id=body.learning_path_id,
        note=body.note,
    )
    _audit(db, user, "approve", str(req.id))
    db.commit()
    db.refresh(req)
    logger.info("Registration %s approved by %s -> user %s", req.id, user.email, created.id)
    return req


@router.post("/registration/requests/{request_id}/reject", response_model=RegistrationRequestOut)
def reject_request(
    request_id: uuid.UUID,
    body: RegistrationRejectIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.REGISTRATION_APPROVE)),
) -> RegistrationRequest:
    """Decline a request, with a reason the applicant sees.  **Permission: registration:approve**"""
    req = _visible(db, user).filter(RegistrationRequest.id == request_id).first()
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration request not found")
    if req.status != RegistrationStatus.pending:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Request is already {req.status.value}")

    req.status = RegistrationStatus.rejected
    req.decided_by_user_id = uuid.UUID(user.id)
    req.decided_at = datetime.now(UTC)
    req.decision_reason = body.reason
    _audit(db, user, "reject", str(req.id))
    db.commit()
    db.refresh(req)
    return req


@router.post("/registration/requests/bulk-approve", response_model=RegistrationBulkApproveOut)
def bulk_approve(
    body: RegistrationBulkApproveIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.REGISTRATION_APPROVE)),
) -> RegistrationBulkApproveOut:
    """Approve a cohort in one action.  **Permission: registration:approve**

    Each request runs inside its own SAVEPOINT, so one bad row in a class of
    thirty does not cost the other twenty-nine their accounts — and equally does
    not leave a half-applied approval behind. A single commit at the end keeps
    the whole batch in one transaction rather than issuing thirty of them.
    """
    results: list[RegistrationBulkResultItem] = []
    for rid in body.request_ids:
        req = _visible(db, user).filter(RegistrationRequest.id == rid).first()
        if req is None:
            results.append(
                RegistrationBulkResultItem(request_id=rid, ok=False, error="not found")
            )
            continue
        try:
            with db.begin_nested():
                created = _approve_one(
                    db,
                    req,
                    user,
                    role=body.role,
                    tenant_id=body.tenant_id,
                    qualification_id=None,
                    learning_path_id=body.learning_path_id,
                    note=body.note,
                )
                _audit(db, user, "approve", str(req.id))
            results.append(
                RegistrationBulkResultItem(request_id=rid, ok=True, user_id=created.id)
            )
        except HTTPException as exc:
            results.append(
                RegistrationBulkResultItem(request_id=rid, ok=False, error=str(exc.detail))
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the batch
            logger.exception("Bulk approve failed for %s", rid)
            results.append(
                RegistrationBulkResultItem(request_id=rid, ok=False, error=str(exc))
            )

    db.commit()
    approved = sum(1 for r in results if r.ok)
    return RegistrationBulkApproveOut(
        approved=approved, failed=len(results) - approved, results=results
    )
