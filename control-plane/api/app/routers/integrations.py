"""TrueNorth Range - External platform integrations router.

Manages registration and connectivity for Moodle, Immersive Labs, OffSec,
and custom LTI/API platforms. Handles external activity sync and LTI 1.3 flows.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel as _PydanticModel
from sqlalchemy.orm import Session

from .. import moodle_sso, net_guard
from ..auth import CurrentUser, get_current_user
from ..course_publishing.models import CoursePublication
from ..db import get_db
from ..delete_guard import commit_delete, refuse_if
from ..lti_identity.models import ExerciseLearner, LTIHandoff, LTILinkRequest, LTIUserLink
from ..models import (
    ExternalActivity,
    ExternalPlatform,
    IntegrationAuthType,
    LTILaunch,
    LTINonce,
    User,
)
from ..moodle_backends import MoodleError, supported_moodle_types
from ..moodle_results import service as results_service
from ..moodle_results.models import MoodleResultCursor, MoodleResultRecord
from ..moodle_results.schemas import MoodleResultsPullIn, MoodleResultsPullOut, MoodleResultsStatusOut
from ..platforms import get_platform_adapter
from ..rbac import Permission, is_platform_admin, require_permission, user_has_permission
from ..schemas import (
    ExternalActivityOut,
    ExternalPlatformIn,
    ExternalPlatformOut,
    ExternalPlatformUpdate,
    PaginatedResponse,
)
from ..tenancy import get_owned, tenant_uuid

logger = logging.getLogger("truenorth.integrations")

router = APIRouter(prefix="/integrations", tags=["integrations"])


# ══════════════════════════════════════════════════════════════════════════
# External Platform Registration
# ══════════════════════════════════════════════════════════════════════════


# Platform records decide which issuer may sign users in over LTI (its JWKS URL is the
# trust anchor), so registering or editing one is an admin act, not a user one.
#
# lti_issuer is also the audience of the Moodle sign-in and sync tickets this API signs
# with the one tool key every tenant shares (moodle_sso, moodle_backends.local_truenorth),
# and the key LTI launches are matched on (lti13.find_platform). So one issuer belongs to
# one tenant, and re-pointing an existing Moodle at another site is the platform
# operator's call, not a tenant admin's.


def _same_issuer(value: str) -> list[str]:
    v = value.rstrip("/")
    return [v, v + "/"]


def _check_issuer(db: Session, user: CurrentUser, issuer: str | None) -> None:
    """409 if another tenant already registered this issuer."""
    if not issuer:
        return
    # tenant-safe: a cross-tenant existence check by design; it returns no row's data.
    taken = (
        db.query(ExternalPlatform.id)
        .filter(
            ExternalPlatform.lti_issuer.in_(_same_issuer(issuer)),
            ExternalPlatform.tenant_id != tenant_uuid(user),
        )
        .first()
    )
    if taken:
        raise HTTPException(status.HTTP_409_CONFLICT, "This issuer is registered to another unit")


@router.post("/platforms", response_model=ExternalPlatformOut, status_code=status.HTTP_201_CREATED)
def register_platform(
    body: ExternalPlatformIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_WRITE)),
):
    """Register an external learning platform (Moodle, Immersive Labs, OffSec).

    **Permission: integration:write**

    409: another tenant already registered this ``lti_issuer``.
    """
    _check_issuer(db, user, body.lti_issuer)
    platform = ExternalPlatform(
        name=body.name,
        slug=body.slug,
        platform_type=body.platform_type,
        base_url=body.base_url,
        auth_type=IntegrationAuthType(body.auth_type),
        tenant_id=tenant_uuid(user),
        lti_client_id=body.lti_client_id,
        lti_deployment_id=body.lti_deployment_id,
        lti_issuer=body.lti_issuer,
        lti_jwks_url=body.lti_jwks_url,
        lti_token_url=body.lti_token_url,
        lti_auth_login_url=body.lti_auth_login_url,
    )
    db.add(platform)
    db.commit()
    db.refresh(platform)
    logger.info("Platform registered: %s (%s)", body.name, body.platform_type)
    return platform


@router.get("/platforms", response_model=list[ExternalPlatformOut])
def list_platforms(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_READ)),
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
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_READ)),
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
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_WRITE)),
):
    """Update a registered platform.

    403: changing a Moodle platform's ``lti_issuer`` (the site its sign-in tickets are
    addressed to) needs a platform administrator. 409: another tenant already registered
    that issuer.
    """
    p = get_owned(db, ExternalPlatform, platform_id, user)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")
    changes = body.model_dump(exclude_unset=True)
    if "lti_issuer" in changes and (changes["lti_issuer"] or "").rstrip("/") != (p.lti_issuer or "").rstrip("/"):
        # A Moodle-farm platform (one the moodle_backends registry can publish to) is
        # addressed by its issuer in the tickets this API signs.
        if p.platform_type in supported_moodle_types() and p.lti_issuer and not is_platform_admin(user, db):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only a platform administrator can re-point a Moodle at another site"
            )
        _check_issuer(db, user, changes["lti_issuer"])
    for field, value in changes.items():
        setattr(p, field, value)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/platforms/{platform_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def deregister_platform(
    platform_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_WRITE)),
):
    """Remove a registered platform.

    409 while Student activity records synced from it, or course publications to it,
    exist: those are history (deactivate the platform instead). Its LTI nonces and
    launches only serve talking to the platform, so they go with it."""
    p = get_owned(db, ExternalPlatform, platform_id, user)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")
    # tenant-safe (all four): p came from get_owned(); counts disclose no row.
    refuse_if(
        db.query(ExternalActivity.id).filter(ExternalActivity.platform_id == p.id),
        "Platform has {n} synced Student activity record(s) and is kept as part of their history",
    )
    refuse_if(
        db.query(CoursePublication.id).filter(CoursePublication.platform_id == p.id),
        "Platform has {n} course publication(s) on record and is kept as part of their history",
    )
    refuse_if(
        db.query(MoodleResultRecord.id).filter(MoodleResultRecord.platform_id == p.id),
        "Platform has {n} Student result record(s) pulled from it and is kept as part of their history",
    )
    refuse_if(
        db.query(ExerciseLearner.id).filter(ExerciseLearner.platform_id == p.id),
        "Platform launched {n} exercise learner(s) and is kept as part of their history",
    )
    # Talking-to-the-platform state goes with it: the results cursor, unspent sign-in codes.
    db.query(MoodleResultCursor).filter(MoodleResultCursor.platform_id == p.id).delete(synchronize_session=False)
    db.query(LTIHandoff).filter(LTIHandoff.platform_id == p.id).delete(synchronize_session=False)
    # Account links name accounts on this platform only; they go with it.
    db.query(LTILinkRequest).filter(LTILinkRequest.platform_id == p.id).delete(synchronize_session=False)
    db.query(LTIUserLink).filter(LTIUserLink.platform_id == p.id).delete(synchronize_session=False)
    db.query(LTINonce).filter(LTINonce.platform_id == p.id).delete(synchronize_session=False)
    db.query(LTILaunch).filter(LTILaunch.platform_id == p.id).delete(synchronize_session=False)
    db.flush()
    db.delete(p)
    commit_delete(db, "Platform")


@router.post("/platforms/{platform_id}/test", status_code=status.HTTP_200_OK)
async def test_connectivity(
    platform_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_WRITE)),
):
    """Test connectivity to an external platform.

    The probe goes through app.net_guard (http(s) only, internal addresses refused unless
    INTEGRATION_ALLOW_PRIVATE_URLS is on, pinned, no redirects) and a failure reads as one
    fixed message per outcome. Before (PR #112 review), it fetched the stored base_url
    unguarded and returned the exception text: an SSRF and a port/host oracle.
    """
    p = get_owned(db, ExternalPlatform, platform_id, user)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")

    url = get_platform_adapter(p.platform_type).health_url(p.base_url or "")
    allow_private = os.getenv("INTEGRATION_ALLOW_PRIVATE_URLS", "false").strip().lower() in ("1", "true", "yes", "on")
    try:
        code = await asyncio.to_thread(net_guard.probe, url, allow_private=allow_private, timeout=10.0)
    except net_guard.DestinationRefusedError:
        return {"platform_id": str(p.id), "reachable": False, "error": PROBE_REFUSED}
    except net_guard.GuardError:
        return {"platform_id": str(p.id), "reachable": False, "error": PROBE_UNREACHABLE}
    return {"platform_id": str(p.id), "reachable": code < 500, "status_code": code}


PROBE_REFUSED = "The platform URL must be http(s) and point at an allowed address"
PROBE_UNREACHABLE = "The platform could not be reached"


class LtiToolConfigOut(_PydanticModel):
    """What a platform admin types into the LMS's tool registration (all real routes)."""

    tool_url: str
    initiate_login_url: str
    redirection_uris: list[str]
    public_keyset_url: str
    deep_linking_url: str
    public_key_pem_url: str


@router.get("/lti/tool-config", response_model=LtiToolConfigOut)
def lti_tool_config(user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_READ))):
    """TrueNorth's LTI 1.3 tool URLs, from ``LTI_TOOL_BASE_URL`` (gap #5).

    Deep linking has no route of its own: the LMS sends the deep-linking request to the
    launch URL, which shows the content picker."""
    from .. import lti13 as _lti

    base = _lti.TOOL_BASE_URL.rstrip("/")
    launch = f"{base}/lti/launch"
    return LtiToolConfigOut(
        tool_url=launch,
        initiate_login_url=f"{base}/lti/login",
        redirection_uris=[launch],
        public_keyset_url=f"{base}/lti/jwks",
        deep_linking_url=launch,
        public_key_pem_url=f"{base}/lti/public-key.pem",
    )


# ══════════════════════════════════════════════════════════════════════════
# Moodle single sign-on (the app hands a signed-in Student into Moodle)
# ══════════════════════════════════════════════════════════════════════════


@router.post("/moodle/sso", response_model=moodle_sso.MoodleSsoOut)
def moodle_sso_ticket(
    body: moodle_sso.MoodleSsoIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """A one-minute, single-use ticket that signs the caller into their unit's Moodle.

    The browser POSTs ``token`` to ``action``. Students need an active enrolment in
    the course; staff (``learning_record:write``) enter as teachers. A course in
    another tenant is 404. Never put the ticket in a URL: it would land in Moodle's
    access log and browser history.
    """
    try:
        return moodle_sso.mint_ticket(db, user, body.course_id)
    except moodle_sso.NotAvailableError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


# ══════════════════════════════════════════════════════════════════════════
# Moodle results (completions and quiz grades pulled back; app/moodle_results)
# ══════════════════════════════════════════════════════════════════════════


def _results_platform(db: Session, platform_id: uuid.UUID, user: CurrentUser) -> ExternalPlatform:
    p = get_owned(db, ExternalPlatform, platform_id, user)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform not found")
    if p.platform_type not in supported_moodle_types():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This platform does not report Moodle results")
    return p


@router.get("/platforms/{platform_id}/moodle-results", response_model=MoodleResultsStatusOut)
def moodle_results_status(
    platform_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_READ)),
):
    """Where TrueNorth is in this Moodle's results: cursor, last run, last error, totals.

    **Permission: integration:read**. 404 for another tenant's platform; 422 for a
    platform that is not a Moodle with the TrueNorth plugin."""
    p = _results_platform(db, platform_id, user)
    row = db.get(MoodleResultCursor, p.id)  # tenant-safe: p came from get_owned()
    if row is None:
        return MoodleResultsStatusOut(platform_id=p.id)
    out = MoodleResultsStatusOut.model_validate(row)
    lease = row.lease_until if row.lease_until is None or row.lease_until.tzinfo else row.lease_until.replace(tzinfo=UTC)
    out.running = lease is not None and lease > datetime.now(UTC)
    return out


@router.post("/platforms/{platform_id}/moodle-results/pull", response_model=MoodleResultsPullOut)
def moodle_results_pull(
    platform_id: uuid.UUID,
    body: MoodleResultsPullIn | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.INTEGRATION_WRITE)),
):
    """Pull completions and quiz grades from this Moodle now and record them.

    **Permission: integration:write** (admins). The scheduled pull
    (``MOODLE_RESULTS_PULL_SECONDS``) does the same. The Moodle's answer must be signed by
    its registered LTI key and answer this request, or nothing is recorded (502). 409 while
    another pull of the same Moodle is running. ``reset`` reads from the beginning again;
    recording is idempotent, so that only re-checks."""
    p = _results_platform(db, platform_id, user)
    try:
        summary = results_service.pull(db, p, reset=bool(body and body.reset))
    except results_service.PullBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except results_service.LeaseLostError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MoodleError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return MoodleResultsPullOut(**summary.as_dict())


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
    """List external learning activities with optional filters.

    Without ``learning_record:read`` a caller sees only their own activities. With it,
    they see their tenant's — never another tenant's. ExternalActivity carries no
    tenant_id of its own, so the scope comes through the platform that recorded it.
    """
    q = db.query(ExternalActivity).join(
        ExternalPlatform, ExternalActivity.platform_id == ExternalPlatform.id
    ).filter(ExternalPlatform.tenant_id == tenant_uuid(user))
    if not user_has_permission(user, Permission.LEARNING_RECORD_READ):
        if user_id and str(user_id) != str(user.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing permission: learning_record:read")
        user_id = uuid.UUID(str(user.id))
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
    user: CurrentUser = Depends(require_permission(Permission.LEARNING_RECORD_WRITE)),
):
    """Record a learning activity from an external platform.

    **Permission: learning_record:write** — this writes to someone's transcript, so a
    learner cannot credit themselves. Both the platform and the learner must belong to
    the caller's tenant.
    """
    get_owned(db, ExternalPlatform, platform_id, user, not_found="Platform not found")
    get_owned(db, User, user_id, user, not_found="User not found")
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

import secrets
import time as _time
from html import escape as _html_escape

import jwt as _jwt
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel as _BaseModel

from .. import lti13
from ..lti_identity import links as lti_links
from ..lti_identity import session as lti_session
from ..models import Course, Quiz, UserRole

WEB_BASE_URL = __import__("os").getenv("LTI_WEB_BASE_URL", "http://localhost:4200")

lti_router = APIRouter(prefix="/lti", tags=["lti"])


@lti_router.get("/jwks")
def lti_jwks(db: Session = Depends(get_db)):
    """Public JWKS for LTI 1.3 tool registration (Moodle fetches this)."""
    return lti13.jwks(db)


@lti_router.get("/public-key.pem", response_class=Response)
def lti_public_key_pem(db: Session = Depends(get_db)):
    """The tool's public key as PEM, for Moodle farm nodes.

    A node fetches this when it starts (``infra/platform/moodle/hooks/04-truenorth-bootstrap.sh``)
    and trusts it for LTI messages, sign-in tickets and course sync, so a key rotation
    reaches every node on its next restart. It is the public half only.
    """
    return Response(lti13.get_tool_key(db).public_key_pem, media_type="application/x-pem-file")


# Two registrations, not api_route(methods=[...]): one route with two methods gets one
# operationId, and FastAPI picks its method suffix from a set, so the published
# contract (docs/interfaces/openapi.json) changed from run to run.
@lti_router.get("/login", operation_id="lti_oidc_login_get")
@lti_router.post("/login", operation_id="lti_oidc_login_post")
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
    response = RedirectResponse(redirect_url, status_code=302)
    if _state_cookie_required():
        # Login CSRF: the launch must come back to the browser that started it. The LMS
        # posts /lti/launch cross-site, so SameSite=None (and therefore Secure).
        state = parse_qs(urlsplit(redirect_url).query).get("state", [""])[0]
        response.set_cookie(
            lti_state_cookie_name(state), state, max_age=600, httponly=True, secure=True, samesite="none", path="/"
        )
    return response


# Security sweep (low), 2026-10-08: an id_token + state obtained for the attacker's own LMS
# account could be posted from the attacker's page into a victim's browser, signing the
# victim in to TrueNorth as the attacker (login CSRF). The state is now also a cookie set
# on the browser that began the login, and the launch must carry both. Set
# LTI_REQUIRE_STATE_COOKIE=false only where the tool runs in an iframe whose browsers block
# third-party cookies; then this protection is off.
#
# One cookie per launch, named for a short hash of its state, so two launches begun in two
# tabs do not overwrite each other's cookie (PR #112 review). Each lives 10 minutes and is
# cleared when its launch succeeds. Caveat: the cookie is Secure, so the tool must be
# served over https; plain-http deployments must turn the check off.
LTI_STATE_COOKIE = "tn_lti_state"


def lti_state_cookie_name(state: str) -> str:
    digest = hashlib.sha256(state.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{LTI_STATE_COOKIE}_{digest}"


def _state_cookie_matches(request: Request, state: str) -> bool:
    """The browser holds this launch's state cookie. Compared as bytes: compare_digest
    raises TypeError on non-ASCII str, which was a 500."""
    cookie = request.cookies.get(lti_state_cookie_name(state), "")
    return bool(state) and hmac.compare_digest(cookie.encode("utf-8", "surrogatepass"), state.encode("utf-8", "surrogatepass"))


def _state_cookie_required() -> bool:
    return os.getenv("LTI_REQUIRE_STATE_COOKIE", "true").strip().lower() not in ("0", "false", "no", "off")


class StaffEmailRefusedError(HTTPException):
    """The LMS asserted a staff account's email for an unlinked LMS account (403). A deep
    linking launch turns this into a link request (lti_identity.links); others are refused."""

    def __init__(self) -> None:
        super().__init__(403, "Staff accounts are not signed in through the learning platform.")


def _jit_user(db: Session, platform, claims: dict) -> User:
    """Find-or-create a TrueNorth user from LTI launch claims.

    An LMS account a staff member linked to their own account (signed in to TrueNorth,
    lti_identity.links) is that account. Otherwise the platform's subject is what
    identifies a returning user (``lti:<platform>:<sub>``). The email claim is asserted by
    the platform, not verified by us, so it links an existing account only inside the
    platform's own tenant (never across tenants: email is globally unique, so that would be
    impersonation) and only a Student's: an LMS naming an instructor's or admin's address
    does not become them (security sweep, low); it raises StaffEmailRefusedError.
    """
    sub = str(claims.get("sub", ""))
    email = str(claims.get("email") or f"lti-{sub}@{platform.slug}.local").lower()
    name = str(claims.get("name") or claims.get("given_name") or email.split("@")[0])
    lti_kc_id = f"lti:{platform.id}:{sub}"

    linked = lti_links.linked_user(db, platform, sub) if sub else None
    if linked is not None:
        return linked
    user = db.query(User).filter(User.keycloak_id == lti_kc_id).first()
    if user is None:
        user = db.query(User).filter(User.email == email).first()
        if user is not None and str(user.tenant_id) == str(platform.tenant_id) and user.role != UserRole.student:
            logger.warning("LTI launch from %s asserted a staff account's email; refused", platform.name)
            raise StaffEmailRefusedError()
    if user:
        if str(user.tenant_id) != str(platform.tenant_id):
            logger.warning(
                "LTI launch from %s asserted an identity that belongs to another tenant; refused",
                platform.name,
            )
            raise HTTPException(403, "This account belongs to a different organisation.")
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
    logger.info("JIT-provisioned LTI user %s from %s", user.id, platform.name)  # id, never the email
    return user


@lti_router.post("/launch")
async def lti_launch(
    request: Request,
    id_token: str = Form(...),
    state: str = Form(...),
    db: Session = Depends(get_db),
):
    """LTI 1.3 resource-link launch: verify id_token, JIT user, redirect into the app.

    The browser must carry the state cookie set at /lti/login (login CSRF), unless
    LTI_REQUIRE_STATE_COOKIE=false."""
    cookie_required = _state_cookie_required()
    if cookie_required and not _state_cookie_matches(request, state):
        raise HTTPException(401, "LTI launch validation failed: this browser did not start the login")
    try:
        platform, claims = await lti13.validate_launch(db, id_token, state)
    except Exception as exc:
        raise HTTPException(401, f"LTI launch validation failed: {exc}") from exc

    message_type = claims.get(lti13.CLAIM_MESSAGE_TYPE, "")
    try:
        user = _jit_user(db, platform, claims)
    except StaffEmailRefusedError:
        # Without the browser binding a link code could be phished: sent to a staff member
        # to confirm in their own session. So no staff link is offered then (still a 403).
        if message_type != "LtiDeepLinkingRequest" or not cookie_required:
            raise
        # Staff deep linking: never bound by email; the staff member confirms it, signed in.
        response = _link_request_page(db, platform, claims)
        if cookie_required:
            response.delete_cookie(lti_state_cookie_name(state), path="/", secure=True, httponly=True, samesite="none")
        return response

    if message_type == "LtiDeepLinkingRequest":
        response = _deep_link_picker(db, platform, claims)
    else:
        response = _resource_link_redirect(db, platform, user, claims, bind_cookie=cookie_required)
    if cookie_required:  # this launch is spent; another tab's cookie is left alone
        response.delete_cookie(lti_state_cookie_name(state), path="/", secure=True, httponly=True, samesite="none")
    return response


def _resource_link_redirect(db: Session, platform, user: User, claims: dict, *, bind_cookie: bool) -> RedirectResponse:
    """Send the browser to what was launched. A Student the launch created (no TrueNorth
    sign-in of their own) goes through the session hand-off first (lti_identity.session);
    anyone else lands on the page and signs in with Keycloak as usual."""
    kind, rid = lti13.parse_resource_target(claims)
    lti13.record_launch(db, platform, user.id, claims, kind, rid)

    if kind == "lab" and rid:
        return RedirectResponse(_launch_lab(db, user, rid), status_code=302)  # the lab page has its own token
    path = _launch_path(db, platform, user, claims, kind, rid)
    # Only the account this very LMS account created: one matched by an asserted email
    # (another LMS account, or another site) must not get a session for it.
    own = user.keycloak_id == f"lti:{platform.id}:{claims.get('sub', '')}"
    if not own or not lti_session.needs_handoff(user):
        return RedirectResponse(f"{WEB_BASE_URL}{path}", status_code=302)
    bind = secrets.token_urlsafe(32) if bind_cookie else ""
    code = lti_session.mint_handoff(db, user, platform, path, bind=bind)
    response = RedirectResponse(f"{WEB_BASE_URL}/lti/session#code={code}", status_code=302)
    if bind:
        # SameSite=Lax: set by this cross-site navigation, sent only on the SPA's own
        # same-site call to /lti/session, never on a request another site makes.
        response.set_cookie(
            lti_session.HANDOFF_COOKIE, bind, max_age=lti_session.HANDOFF_SECONDS, httponly=True, secure=True,
            samesite="lax", path="/",
        )
    return response


def _launch_path(db: Session, platform, user: User, claims: dict, kind: str, rid: str) -> str:
    """The SPA route for a launched resource (gaps #3 and #4)."""
    from urllib.parse import quote

    if kind == "quiz" and rid:
        return f"/quiz-player?quiz={quote(rid, safe='')}&lti=1"  # the route that reads ?quiz=
    if kind == "exercise" and rid:
        _link_exercise_learner(db, platform, user, claims, rid)
        return f"/exercises/{quote(rid, safe='')}?lti=1"  # the Student's own exercise page
    if kind == "course" and rid:
        return f"/training?course={quote(rid, safe='')}&lti=1"
    return "/training?lti=1"


def _link_exercise_learner(db: Session, platform, user: User, claims: dict, rid: str) -> None:
    """Record who launched this exercise run from the LMS (gap #4). The exercise must be the
    platform's tenant's; a launch naming another tenant's exercise is refused."""
    from ..models import Exercise

    try:
        exercise_id = uuid.UUID(rid)
    except ValueError as exc:
        raise HTTPException(400, "malformed exercise link") from exc
    exercise = db.get(Exercise, exercise_id)  # tenant-safe: compared with the platform's tenant below
    if exercise is None or exercise.deleted_at is not None or exercise.tenant_id != platform.tenant_id:
        raise HTTPException(404, "Exercise not found")
    exists_ = db.query(ExerciseLearner.id).filter_by(exercise_id=exercise.id, user_id=user.id).first()
    if exists_ is None:
        link = (claims.get(lti13.CLAIM_RESOURCE_LINK) or {}).get("id", "")
        db.add(ExerciseLearner(exercise_id=exercise.id, user_id=user.id, platform_id=platform.id,
                               resource_link_id=str(link)[:255]))
        db.commit()


class LtiSessionIn(_BaseModel):
    code: str


class LtiSessionUserOut(_BaseModel):
    id: uuid.UUID
    display_name: str
    role: str


class LtiSessionOut(_BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    target: str
    user: LtiSessionUserOut


@lti_router.post("/session", response_model=LtiSessionOut)
def lti_session_exchange(body: LtiSessionIn, request: Request, db: Session = Depends(get_db)):
    """Exchange a launch's hand-off code for a TrueNorth session (gap #1).

    Only for a Student the LTI launch created; the code is single use, lives two minutes
    and, with ``LTI_REQUIRE_STATE_COOKIE`` on, works only in the browser that launched.
    401 otherwise. The session lasts ``LTI_SESSION_SECONDS`` (2 h) and is not renewable.

    ```
    POST /lti/session {"code": "<from /lti/session#code=…>"}
    200 {"access_token": "<JWT>", "token_type": "Bearer", "expires_in": 7200,
         "target": "/quiz-player?quiz=<id>&lti=1",
         "user": {"id": "<uuid>", "display_name": "…", "role": "student"}}
    ```
    """
    try:
        token, lifetime, user, target = lti_session.exchange(
            db, body.code, request.cookies.get(lti_session.HANDOFF_COOKIE, "")
        )
    except lti_session.HandoffError as exc:
        raise HTTPException(401, str(exc)) from exc
    return LtiSessionOut(
        access_token=token,
        expires_in=lifetime,
        target=target,
        user=LtiSessionUserOut(id=user.id, display_name=user.display_name, role=user.role.value),
    )


def _launch_lab(db: Session, user: User, rid: str) -> str:
    """A Moodle "Start lab" link: start or resume this student's lab and send the browser to
    the lab page with the session's own access token (in the fragment, so it never reaches
    a server log or a Referer header)."""
    import uuid as _uuid

    from ..lab_sessions import service as labs
    from ..lab_sessions import tokens as lab_tokens

    course_part, _, activity_id = rid.partition(":")
    try:
        course_id = _uuid.UUID(course_part)
    except ValueError as exc:
        raise HTTPException(400, "malformed lab link") from exc
    try:
        with db.begin_nested():
            # Moodle course membership is the control on this path, so the launch enrols.
            release = labs.release_for_student(
                db, tenant_id=user.tenant_id, user_id=user.id, course_id=course_id, auto_enroll=True
            )
            session, _ = labs.launch(
                db, tenant_id=user.tenant_id, user_id=user.id, release_id=release.id, activity_id=activity_id
            )
    except labs.LabRefusedError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    db.commit()
    labs.flush_outbox(db)
    token = lab_tokens.mint(db, session.id, user.id, session.max_expires_at)
    return f"{WEB_BASE_URL}/labs/{session.id}?lti=1#token={token}"


# The tool key also signs the LtiDeepLinkingResponse and AGS client assertions; this
# audience means only a picker session is accepted back at /deeplink/finish.
DEEP_LINK_SESSION_AUDIENCE = "truenorth:lti-deeplink-session"


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
            "aud": DEEP_LINK_SESSION_AUDIENCE,
            "platform_id": str(platform.id),
            "deployment_id": claims.get(lti13.CLAIM_DEPLOYMENT, ""),
            "return_url": return_url,
            "data": settings.get("data", ""),
            "exp": int(_time.time()) + 1800,
        },
        lti13.signing_pem(key),
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


def _link_request_page(db: Session, platform, claims: dict) -> HTMLResponse:
    """A staff member's LMS account asked to deep-link: offer the one-time link, which
    they confirm signed in to TrueNorth (lti_identity.links), in this browser only (the
    request is always bound to the cookie set here). Nothing is bound here."""
    bind = secrets.token_urlsafe(32)
    code = lti_links.request_link(db, platform, claims, bind=bind)
    url = f"{WEB_BASE_URL}/lti/link#code={code}"
    html = f"""<!doctype html><html><head><title>TrueNorth — link your account</title>
<style>body{{font-family:system-ui;background:#071629;color:#F0F4F8;padding:40px;max-width:640px;margin:auto}}
a.button{{display:inline-block;background:#1FB6A6;color:#071629;border-radius:8px;padding:12px 24px;font-weight:700;
text-decoration:none;margin-top:16px}} h1{{font-size:20px}}</style></head><body>
<h1>Link this learning-platform account to TrueNorth</h1>
<p>This account's email belongs to a TrueNorth staff account. TrueNorth never signs staff in
by email alone, so link the two once: open TrueNorth, sign in as yourself, and confirm.
The link expires in ten minutes. Then choose <em>Select content</em> again.</p>
<a class="button" href="{_html_escape(url)}" target="_blank" rel="noopener noreferrer">Open TrueNorth to confirm</a>
</body></html>"""
    response = HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.set_cookie(
        lti_links.LINK_COOKIE, bind, max_age=lti_links.REQUEST_SECONDS, httponly=True, secure=True,
        samesite="lax", path="/",
    )
    return response


class LtiLinkCodeIn(_BaseModel):
    code: str


class LtiLinkPreviewOut(_BaseModel):
    platform_id: uuid.UUID
    platform_name: str
    lms_name: str


class LtiLinkOut(_BaseModel):
    id: uuid.UUID
    platform_id: uuid.UUID
    platform_name: str = ""
    lms_name: str
    confirmed_at: datetime | None = None


def _link_out(db: Session, link) -> LtiLinkOut:
    platform = db.get(ExternalPlatform, link.platform_id)  # tenant-safe: the link's own platform
    return LtiLinkOut(id=link.id, platform_id=link.platform_id, platform_name=platform.name if platform else "",
                      lms_name=link.lms_name, confirmed_at=link.confirmed_at)


def _signed_in_user(db: Session, user: CurrentUser) -> User:
    row = db.get(User, uuid.UUID(str(user.id)))  # tenant-safe: the caller's own row
    if row is None:
        raise HTTPException(403, "Sign in to TrueNorth with your own account")
    return row


@lti_router.post("/links/preview", response_model=LtiLinkPreviewOut)
def lti_link_preview(
    body: LtiLinkCodeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """What a staff member is about to link: the platform and the LMS account's name.

    Same checks as confirm, nothing changed. 404 unknown, spent or another tenant's;
    410 expired; 403 another browser, a Student, an LTI session, or an email that is not
    the caller's."""
    try:
        return lti_links.preview(db, body.code, request.cookies.get(lti_links.LINK_COOKIE, ""),
                                 _signed_in_user(db, user))
    except lti_links.LinkError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@lti_router.post("/links/confirm", response_model=LtiLinkOut, status_code=status.HTTP_201_CREATED)
def lti_link_confirm(
    body: LtiLinkCodeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Link the LMS account of a deep-linking launch to the signed-in staff account (once).

    ```
    POST /lti/links/confirm {"code": "<from /lti/link#code=…>"}
    201 {"id": "…", "platform_id": "…", "platform_name": "Moodle (default)",
         "lms_name": "Ada Lovelace", "confirmed_at": "…"}
    ```
    Must be the browser that launched (cookie), a staff account of the platform's tenant,
    signed in with its own sign-in (not an LTI session), whose email the LMS asserted.
    409 if either side is already linked on that platform."""
    try:
        link = lti_links.confirm(db, body.code, request.cookies.get(lti_links.LINK_COOKIE, ""),
                                 _signed_in_user(db, user))
    except lti_links.LinkError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    logger.info("LTI account on platform %s linked to user %s", link.platform_id, link.user_id)
    return _link_out(db, link)


@lti_router.get("/links", response_model=list[LtiLinkOut])
def lti_links_mine(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """The learning-platform accounts linked to the caller's own account."""
    rows =db.query(LTIUserLink).filter(LTIUserLink.user_id == uuid.UUID(str(user.id))).all()
    return [_link_out(db, r) for r in rows]


@lti_router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def lti_link_remove(link_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Unlink: the account holder, or an integration admin of the same tenant. 404 otherwise."""
    try:
        lti_links.unlink(db, link_id, _signed_in_user(db, user),
                         admin=user_has_permission(user, Permission.INTEGRATION_WRITE))
    except lti_links.LinkError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


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
        session = _jwt.decode(
            session_token,
            key.public_key_pem,
            algorithms=["RS256"],
            audience=DEEP_LINK_SESSION_AUDIENCE,
            options={"require": ["exp", "platform_id"]},
        )
        platform_id = uuid.UUID(str(session["platform_id"]))
    except Exception as exc:
        raise HTTPException(401, "Invalid deep-linking session") from exc

    # tenant-safe: unauthenticated LTI return leg; platform_id comes from the deep-link
    # session JWT this tool signed at launch (verified above), not from the caller.
    platform = db.get(ExternalPlatform, platform_id)
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
    user: CurrentUser = Depends(require_permission(Permission.LEARNING_RECORD_WRITE)),
):
    """Manually push a score to the launching platform's gradebook (AGS).

    **Permission: learning_record:write**, and the learner must be in the caller's
    tenant. The score lands in an external gradebook, so this is never self-service:
    a learner could otherwise post any score for themselves.

    Automatic pushes happen on quiz submission / exercise completion; this
    endpoint lets operators retry or backfill.
    """
    get_owned(db, User, body.user_id, user, not_found="User not found")
    pushed = await lti13.push_score_for_resource(
        db, body.user_id, body.resource_kind, body.resource_id, body.score, body.max_score
    )
    if not pushed:
        raise HTTPException(404, "No LTI launch with an AGS lineitem found for this user/resource.")
    return {"status": "pushed"}
