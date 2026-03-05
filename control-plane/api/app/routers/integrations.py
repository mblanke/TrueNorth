"""TrueNorth Range - External platform integrations router.

Manages registration and connectivity for Moodle, Immersive Labs, OffSec,
and custom LTI/API platforms. Handles external activity sync and LTI 1.3 flows.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    ExternalActivity,
    ExternalPlatform,
    IntegrationAuthType,
)
from ..schemas import (
    ExternalActivityOut,
    ExternalPlatformIn,
    ExternalPlatformOut,
    ExternalPlatformUpdate,
    PaginatedResponse,
)

logger = logging.getLogger("truenorth.integrations")

router = APIRouter(prefix="/integrations", tags=["integrations"])


# ══════════════════════════════════════════════════════════════════════════
# External Platform Registration
# ══════════════════════════════════════════════════════════════════════════


@router.post("/platforms", response_model=ExternalPlatformOut, status_code=status.HTTP_201_CREATED)
def register_platform(
    body: ExternalPlatformIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Register an external learning platform (Moodle, Immersive Labs, OffSec)."""
    platform = ExternalPlatform(
        name=body.name,
        slug=body.slug,
        platform_type=body.platform_type,
        base_url=body.base_url,
        auth_type=IntegrationAuthType(body.auth_type),
        tenant_id=user.tenant_id,
        lti_client_id=body.lti_client_id,
        lti_deployment_id=body.lti_deployment_id,
        lti_issuer=body.lti_issuer,
        lti_jwks_url=body.lti_jwks_url,
        lti_token_url=body.lti_token_url,
    )
    db.add(platform)
    db.commit()
    db.refresh(platform)
    logger.info("Platform registered: %s (%s)", body.name, body.platform_type)
    return platform


@router.get("/platforms", response_model=list[ExternalPlatformOut])
def list_platforms(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all registered external platforms for the tenant."""
    return (
        db.query(ExternalPlatform)
        .filter(ExternalPlatform.tenant_id == user.tenant_id)
        .order_by(ExternalPlatform.name)
        .all()
    )


@router.get("/platforms/{platform_id}", response_model=ExternalPlatformOut)
def get_platform(
    platform_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Get details of a registered platform."""
    p = db.query(ExternalPlatform).filter(ExternalPlatform.id == platform_id).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")
    return p


@router.patch("/platforms/{platform_id}", response_model=ExternalPlatformOut)
def update_platform(
    platform_id: uuid.UUID,
    body: ExternalPlatformUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a registered platform."""
    p = db.query(ExternalPlatform).filter(ExternalPlatform.id == platform_id).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(p, field, value)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/platforms/{platform_id}", status_code=status.HTTP_204_NO_CONTENT)
def deregister_platform(
    platform_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Remove a registered platform."""
    p = db.query(ExternalPlatform).filter(ExternalPlatform.id == platform_id).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")
    db.delete(p)
    db.commit()


@router.post("/platforms/{platform_id}/test", status_code=status.HTTP_200_OK)
async def test_connectivity(
    platform_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Test connectivity to an external platform."""
    import httpx

    p = db.query(ExternalPlatform).filter(ExternalPlatform.id == platform_id).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if p.platform_type == "moodle":
                # Moodle: hit /admin/tool/mobile/launch.php or /login/token.php
                resp = await client.get(f"{p.base_url}/lib/ajax/service-nologin.php")
                reachable = resp.status_code < 500
            elif p.platform_type == "immersive_labs":
                resp = await client.get(f"{p.base_url}/api/health", timeout=10)
                reachable = resp.status_code < 500
            elif p.platform_type == "offsec":
                resp = await client.get(f"{p.base_url}", timeout=10)
                reachable = resp.status_code < 500
            else:
                resp = await client.get(p.base_url, timeout=10)
                reachable = resp.status_code < 500
    except Exception as e:
        return {"platform_id": str(p.id), "reachable": False, "error": str(e)}

    return {"platform_id": str(p.id), "reachable": reachable, "status_code": resp.status_code}


# ══════════════════════════════════════════════════════════════════════════
# External Activities (cross-platform learning records)
# ══════════════════════════════════════════════════════════════════════════


@router.get("/activities", response_model=PaginatedResponse[ExternalActivityOut])
def list_external_activities(
    user_id: uuid.UUID | None = Query(default=None),
    platform_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List external learning activities with optional filters."""
    q = db.query(ExternalActivity)
    if user_id:
        q = q.filter(ExternalActivity.user_id == user_id)
    if platform_id:
        q = q.filter(ExternalActivity.platform_id == platform_id)
    total = q.count()
    items = q.order_by(ExternalActivity.created_at.desc()).offset(offset).limit(limit).all()
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("/activities", response_model=ExternalActivityOut, status_code=status.HTTP_201_CREATED)
def record_external_activity(
    platform_id: uuid.UUID,
    user_id: uuid.UUID,
    external_ref: str,
    activity_type: str,
    title: str,
    description: str = "",
    score: int | None = None,
    max_score: int | None = None,
    passed: bool | None = None,
    completed_at: datetime | None = None,
    duration_seconds: int | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Record a learning activity from an external platform."""
    ea = ExternalActivity(
        platform_id=platform_id,
        user_id=user_id,
        external_ref=external_ref,
        activity_type=activity_type,
        title=title,
        description=description,
        score=score,
        max_score=max_score,
        passed=passed,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
    )
    db.add(ea)
    db.commit()
    db.refresh(ea)
    return ea


# ══════════════════════════════════════════════════════════════════════════
# LTI 1.3 Endpoints (Tool Provider — lets Moodle/OffSec launch TrueNorth)
# ══════════════════════════════════════════════════════════════════════════

lti_router = APIRouter(prefix="/lti", tags=["lti"])


@lti_router.get("/jwks")
def lti_jwks():
    """Public JWKS endpoint for LTI 1.3 tool registration.

    External platforms (Moodle, OffSec) fetch TrueNorth's public keys from
    this endpoint to validate JWTs during the LTI launch flow.
    """
    # TODO: Generate and cache RSA key pair; return public key in JWKS format
    # For now, return a placeholder structure
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": "truenorth-lti-2026",
                "alg": "RS256",
                "n": "placeholder",
                "e": "AQAB",
            }
        ]
    }


@lti_router.post("/login")
async def lti_oidc_login(
    db: Session = Depends(get_db),
):
    """LTI 1.3 OIDC initiation endpoint.

    Step 1 of the LTI 1.3 launch flow: the platform (Moodle) redirects here
    with iss, login_hint, target_link_uri. We generate a nonce and redirect
    back to the platform's authorize endpoint.
    """
    # TODO: Implement full OIDC login initiation with pylti1p3
    # 1. Validate issuer against registered ExternalPlatform
    # 2. Generate nonce + state, store in LTINonce table
    # 3. Redirect to platform's authorize_url with nonce
    return {"status": "lti_login_endpoint_ready", "note": "Full implementation requires pylti1p3"}


@lti_router.post("/launch")
async def lti_launch(
    db: Session = Depends(get_db),
):
    """LTI 1.3 launch endpoint.

    Step 2: Platform posts id_token JWT here after OIDC auth. We validate
    the JWT, extract user identity + course context, create/map user in
    Keycloak, and redirect to the exercise/course.
    """
    # TODO: Implement full JWT validation with pylti1p3
    # 1. Validate id_token JWT against platform's JWKS
    # 2. Extract: sub, name, email, roles, resource_link, context
    # 3. JIT-provision user in Keycloak + TrueNorth User table
    # 4. Create enrollment if course context provided
    # 5. Redirect to exercise/course page
    return {"status": "lti_launch_endpoint_ready", "note": "Full implementation requires pylti1p3"}


@lti_router.post("/deeplink")
async def lti_deep_link(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """LTI 1.3 Deep Linking response endpoint.

    Allows platform instructors to browse and select TrueNorth exercises/courses
    to embed as LTI activities in their Moodle course.
    """
    # TODO: Return list of available content items for deep linking
    return {"status": "lti_deeplink_endpoint_ready"}


@lti_router.post("/grades")
async def lti_grade_passback(
    db: Session = Depends(get_db),
):
    """LTI 1.3 Assignment and Grade Services (AGS) callback.

    Pushes exercise scores back to the launching platform's gradebook.
    Called internally when an exercise completes if it was launched via LTI.
    """
    # TODO: Use platform's AGS endpoint to POST scores
    return {"status": "lti_grades_endpoint_ready"}