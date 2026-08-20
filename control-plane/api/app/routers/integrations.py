"""TrueNorth Range - External platform integrations router.

Manages registration and connectivity for Moodle, Immersive Labs, OffSec,
and custom LTI/API platforms. Handles external activity sync and LTI 1.3 flows.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
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
from ..tenancy import get_owned

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
    p = get_owned(db, ExternalPlatform, platform_id, user)
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
    p = get_owned(db, ExternalPlatform, platform_id, user)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(p, field, value)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/platforms/{platform_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def deregister_platform(
    platform_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Remove a registered platform."""
    p = get_owned(db, ExternalPlatform, platform_id, user)
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

    p = get_owned(db, ExternalPlatform, platform_id, user)
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

import time as _time
from html import escape as _html_escape

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jose import jwt as _jwt
from pydantic import BaseModel as _BaseModel

from .. import lti13
from ..models import Course, Quiz, User, UserRole

WEB_BASE_URL = __import__("os").getenv("LTI_WEB_BASE_URL", "http://localhost:4200")

lti_router = APIRouter(prefix="/lti", tags=["lti"])


@lti_router.get("/jwks")
def lti_jwks(db: Session = Depends(get_db)):
    """Public JWKS for LTI 1.3 tool registration (Moodle fetches this)."""
    return lti13.jwks(db)


@lti_router.api_route("/login", methods=["GET", "POST"])
async def lti_oidc_login(request: Request, db: Session = Depends(get_db)):
    """LTI 1.3 OIDC initiation: validate issuer, mint state+nonce, redirect."""
    params = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({k: str(v) for k, v in form.items()})

    iss = params.get("iss", "")
    login_hint = params.get("login_hint", "")
    target_link_uri = params.get("target_link_uri", "")
    if not iss or not login_hint or not target_link_uri:
        raise HTTPException(422, "Missing iss / login_hint / target_link_uri")

    try:
        redirect_url = lti13.build_login_redirect(
            db,
            iss=iss,
            login_hint=login_hint,
            target_link_uri=target_link_uri,
            client_id=params.get("client_id"),
            lti_message_hint=params.get("lti_message_hint"),
        )
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    return RedirectResponse(redirect_url, status_code=302)


def _jit_user(db: Session, platform, claims: dict) -> User:
    """Find-or-create a TrueNorth user from LTI launch claims."""
    sub = str(claims.get("sub", ""))
    email = str(claims.get("email") or f"lti-{sub}@{platform.slug}.local").lower()
    name = str(claims.get("name") or claims.get("given_name") or email.split("@")[0])
    lti_kc_id = f"lti:{platform.id}:{sub}"

    user = db.query(User).filter((User.keycloak_id == lti_kc_id) | (User.email == email)).first()
    if user:
        return user
    user = User(
        keycloak_id=lti_kc_id,
        email=email,
        display_name=name[:255],
        role=UserRole.student,
        tenant_id=platform.tenant_id,
        source="lti",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("JIT-provisioned LTI user %s from %s", email, platform.name)
    return user


@lti_router.post("/launch")
async def lti_launch(
    id_token: str = Form(...),
    state: str = Form(...),
    db: Session = Depends(get_db),
):
    """LTI 1.3 resource-link launch: verify id_token, JIT user, redirect into the app."""
    try:
        platform, claims = await lti13.validate_launch(db, id_token, state)
    except Exception as exc:
        raise HTTPException(401, f"LTI launch validation failed: {exc}") from exc

    message_type = claims.get(lti13.CLAIM_MESSAGE_TYPE, "")
    user = _jit_user(db, platform, claims)

    if message_type == "LtiDeepLinkingRequest":
        return _deep_link_picker(db, platform, claims)

    kind, rid = lti13.parse_resource_target(claims)
    lti13.record_launch(db, platform, user.id, claims, kind, rid)

    if kind == "quiz" and rid:
        target = f"{WEB_BASE_URL}/training?quiz={rid}&lti=1"
    elif kind == "exercise" and rid:
        target = f"{WEB_BASE_URL}/exercises?exercise={rid}&lti=1"
    elif kind == "course" and rid:
        target = f"{WEB_BASE_URL}/training?course={rid}&lti=1"
    else:
        target = f"{WEB_BASE_URL}/training?lti=1"
    return RedirectResponse(target, status_code=302)


def _deep_link_picker(db: Session, platform, claims: dict) -> HTMLResponse:
    """Render a minimal content picker; selection posts a signed DL response."""
    settings = claims.get(lti13.CLAIM_DL_SETTINGS) or {}
    return_url = settings.get("deep_link_return_url", "")
    if not return_url:
        raise HTTPException(422, "Deep linking request missing return URL")

    # Session token so /deeplink/finish can trust the return context.
    key = lti13.get_tool_key(db)
    session_jwt = _jwt.encode(
        {
            "platform_id": str(platform.id),
            "deployment_id": claims.get(lti13.CLAIM_DEPLOYMENT, ""),
            "return_url": return_url,
            "data": settings.get("data", ""),
            "exp": int(_time.time()) + 1800,
        },
        key.private_key_pem,
        algorithm="RS256",
        headers={"kid": key.kid},
    )

    quizzes = (
        db.query(Quiz)
        .filter(Quiz.tenant_id == platform.tenant_id, Quiz.is_published.is_(True), Quiz.deleted_at.is_(None))
        .order_by(Quiz.created_at.desc())
        .limit(50)
        .all()
    )
    courses = (
        db.query(Course)
        .filter(Course.tenant_id == platform.tenant_id, Course.is_published.is_(True))
        .order_by(Course.created_at.desc())
        .limit(50)
        .all()
    )

    rows = []
    for quiz in quizzes:
        rows.append(
            f'<label><input type="checkbox" name="item" value="quiz:{quiz.id}:{_html_escape(quiz.title)}"> '
            f"&#x1F4DD; {_html_escape(quiz.title)}</label>"
        )
    for course in courses:
        rows.append(
            f'<label><input type="checkbox" name="item" value="course:{course.id}:{_html_escape(course.name)}"> '
            f"&#x1F393; {_html_escape(course.name)}</label>"
        )
    items_html = "<br>".join(rows) or "<em>No published quizzes or courses yet.</em>"

    html = f"""<!doctype html><html><head><title>TrueNorth — Select Content</title>
<style>body{{font-family:system-ui;background:#071629;color:#F0F4F8;padding:40px;max-width:640px;margin:auto}}
label{{display:block;padding:8px 12px;border:1px solid #1B3A5E;border-radius:8px;margin:6px 0;cursor:pointer}}
button{{background:#1FB6A6;color:#071629;border:0;border-radius:8px;padding:12px 24px;font-weight:700;margin-top:16px;cursor:pointer}}
h1{{font-size:20px}}</style></head><body>
<h1>TrueNorth Range — add activities to your course</h1>
<form method="post" action="/api/lti/deeplink/finish">
<input type="hidden" name="session" value="{session_jwt}">
{items_html}
<br><button type="submit">Add selected to course</button>
</form></body></html>"""
    return HTMLResponse(html)


@lti_router.post("/deeplink/finish")
async def lti_deep_link_finish(
    request: Request,
    db: Session = Depends(get_db),
):
    """Build the signed LtiDeepLinkingResponse and auto-post it back to the platform."""
    form = await request.form()
    session_token = str(form.get("session", ""))
    selections = [str(v) for v in form.getlist("item")]

    key = lti13.get_tool_key(db)
    try:
        session = _jwt.decode(session_token, key.public_key_pem, algorithms=["RS256"])
    except Exception as exc:
        raise HTTPException(401, "Invalid deep-linking session") from exc

    platform = db.get(ExternalPlatform, uuid.UUID(session["platform_id"]))
    if not platform:
        raise HTTPException(404, "Platform not found")

    content_items = []
    for sel in selections:
        kind, _, rest = sel.partition(":")
        rid, _, title = rest.partition(":")
        if kind in ("quiz", "course", "exercise") and rid:
            content_items.append(lti13.content_item_for(kind, rid, title or kind))

    response_jwt = lti13.build_deep_link_response(
        db, platform, session.get("deployment_id", ""), content_items, session.get("data") or None
    )
    html = f"""<!doctype html><html><body onload="document.forms[0].submit()">
<form method="post" action="{_html_escape(session["return_url"])}">
<input type="hidden" name="JWT" value="{response_jwt}">
<noscript><button type="submit">Continue</button></noscript>
</form></body></html>"""
    return HTMLResponse(html)


class GradePushIn(_BaseModel):
    user_id: uuid.UUID
    resource_kind: str
    resource_id: str
    score: float
    max_score: float


@lti_router.post("/grades")
async def lti_grade_passback(
    body: GradePushIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Manually push a score to the launching platform's gradebook (AGS).

    Automatic pushes happen on quiz submission / exercise completion; this
    endpoint lets operators retry or backfill.
    """
    pushed = await lti13.push_score_for_resource(
        db, body.user_id, body.resource_kind, body.resource_id, body.score, body.max_score
    )
    if not pushed:
        raise HTTPException(404, "No LTI launch with an AGS lineitem found for this user/resource.")
    return {"status": "pushed"}
