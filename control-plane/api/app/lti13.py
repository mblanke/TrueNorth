"""TrueNorth Range — LTI 1.3 tool-provider core.

Native implementation on PyJWT + cryptography + httpx:
  - persistent RSA tool keypair (DB) + JWKS publication
  - OIDC login initiation + id_token launch validation (nonce/state replay-safe)
  - Deep Linking response signing
  - Assignment & Grade Services (AGS) score push with client_credentials
    JWT-bearer assertions

Spec references: IMS LTI 1.3 Core, LTI-DL 2.0, LTI-AGS 2.0.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from . import jwks as jwks_verify
from . import net_guard
from .models import ExternalPlatform, LTILaunch, LTINonce, LTIToolKey, User
from .secretbox import seal, unseal

logger = logging.getLogger("truenorth.api.lti13")

TOOL_BASE_URL = os.getenv("LTI_TOOL_BASE_URL", "http://localhost:8081")

AGS_SCORE_SCOPE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"
CLAIM_MESSAGE_TYPE = "https://purl.imsglobal.org/spec/lti/claim/message_type"
CLAIM_DEPLOYMENT = "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
CLAIM_RESOURCE_LINK = "https://purl.imsglobal.org/spec/lti/claim/resource_link"
CLAIM_CONTEXT = "https://purl.imsglobal.org/spec/lti/claim/context"
CLAIM_CUSTOM = "https://purl.imsglobal.org/spec/lti/claim/custom"
CLAIM_AGS = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
CLAIM_DL_SETTINGS = "https://purl.imsglobal.org/spec/lti-dl/claim/deep_linking_settings"
CLAIM_DL_CONTENT_ITEMS = "https://purl.imsglobal.org/spec/lti-dl/claim/content_items"


# ── Server-side routing to the platform ──────────────────────────────────


def platform_route(platform: ExternalPlatform, url: str) -> tuple[str, dict[str, str]]:
    """Where TrueNorth's server should send a request for a platform's public URL.

    Registration URLs (issuer, JWKS, token, AGS lineitems) are the platform's public
    ones: browsers use them and the platform checks token audiences against them.
    When the platform is reached from inside the deployment at a different origin
    (``base_url``, e.g. ``http://moodle-unit:8080``), send the request there and keep
    the public Host. Moodle redirects any request whose Host is not its wwwroot.
    Only URLs on the issuer's own origin are rerouted, so a lineitem URL taken from a
    launch can never point our traffic at the internal address of something else.
    """
    public = urlsplit(platform.lti_issuer or "")
    internal = urlsplit(platform.base_url or "")
    target = urlsplit(url)
    same_origin = (target.scheme, target.netloc) == (public.scheme, public.netloc)
    if not public.netloc or not internal.netloc or not same_origin or internal.netloc == public.netloc:
        return url, {}
    rerouted = urlunsplit((internal.scheme, internal.netloc, target.path, target.query, target.fragment))
    return rerouted, {"Host": public.netloc}


# ── Tool keypair ─────────────────────────────────────────────────────────


def get_tool_key(db: Session) -> LTIToolKey:
    """Fetch (or lazily create) the active RSA signing key."""
    key = db.query(LTIToolKey).filter(LTIToolKey.is_active.is_(True)).first()
    if key:
        return key
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    key = LTIToolKey(
        kid=f"truenorth-{secrets.token_hex(6)}",
        private_key_pem=seal(private_pem),  # app/secretbox.py: never stored as generated
        public_key_pem=public_pem,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    logger.info("Generated LTI tool RSA keypair kid=%s", key.kid)
    return key


def signing_pem(key: LTIToolKey) -> str:
    """The tool key's private PEM, unsealed. Every signer reads the key through this.

    ``lti_tool_keys.private_key_pem`` is sealed (app/secretbox.py); rows from before the
    sealing migration are returned as stored.
    """
    return unseal(key.private_key_pem)


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks(db: Session) -> dict:
    key = get_tool_key(db)
    public = serialization.load_pem_public_key(key.public_key_pem.encode())
    numbers = public.public_numbers()  # type: ignore[union-attr]
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": key.kid,
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }


# ── OIDC login initiation ────────────────────────────────────────────────


def build_login_redirect(
    db: Session,
    iss: str,
    login_hint: str,
    target_link_uri: str,
    client_id: str | None,
    lti_message_hint: str | None,
) -> str:
    """Step 1: create state+nonce and build the platform authorize redirect URL."""
    platform = find_platform(db, iss, client_id)
    if not platform or not platform.lti_auth_login_url:
        raise ValueError("Unknown LTI platform issuer or missing auth login URL")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    db.add(
        LTINonce(
            nonce=nonce,
            state=state,
            platform_id=platform.id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    db.commit()

    from urllib.parse import urlencode

    params = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": platform.lti_client_id or client_id or "",
        "redirect_uri": target_link_uri,
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
    }
    if lti_message_hint:
        params["lti_message_hint"] = lti_message_hint
    return f"{platform.lti_auth_login_url}?{urlencode(params)}"


def find_platform(db: Session, iss: str, client_id: str | None = None) -> ExternalPlatform | None:
    """The registration for (issuer, client id), never a different client's.

    Several tenants can register the same Moodle (same issuer) under different client
    ids. An unknown client id used to fall back to whichever registration came first,
    so login initiation minted state for, and redirected as, someone else's client.
    A registration without a client id is still returned so launch can refuse it with
    a clear reason. Without a client id the issuer must identify exactly one platform.
    """
    q = db.query(ExternalPlatform).filter(
        ExternalPlatform.lti_issuer == iss, ExternalPlatform.is_active.is_(True)
    )
    if client_id:
        return (
            q.filter(ExternalPlatform.lti_client_id == client_id).first()
            or q.filter(ExternalPlatform.lti_client_id.is_(None)).first()
        )
    candidates = q.limit(2).all()
    return candidates[0] if len(candidates) == 1 else None


# ── Launch validation ────────────────────────────────────────────────────


async def validate_launch(db: Session, id_token: str, state: str) -> tuple[ExternalPlatform, dict]:
    """Step 2: verify state/nonce + the platform-signed id_token. Returns claims."""
    # Unverified read only to find the platform registration; verified below.
    unverified = jwt.decode(id_token, options={"verify_signature": False})
    iss = unverified.get("iss", "")
    aud = unverified.get("aud")
    # With several audiences, azp names the client (enforced after verification below).
    client_id = unverified.get("azp") or (aud[0] if isinstance(aud, list) and aud else aud)

    platform = find_platform(db, iss, client_id)
    if not platform:
        raise ValueError(f"No registered LTI platform for issuer {iss}")
    if not platform.lti_jwks_url:
        raise ValueError("Platform has no JWKS URL configured")
    # Both identifiers must come from our registration, never from the token. With no
    # registered client id the audience check below would compare the token's `aud`
    # against itself, and without a deployment id any install of the same client on
    # that issuer could launch into this tenant.
    if not platform.lti_client_id:
        raise ValueError("Platform has no LTI client id registered")
    if not platform.lti_deployment_id:
        raise ValueError("Platform has no LTI deployment id registered")

    jwks_url, jwks_headers = platform_route(platform, platform.lti_jwks_url)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(jwks_url, headers=jwks_headers)
        resp.raise_for_status()
        platform_jwks = resp.json()

    claims = jwks_verify.decode(
        id_token,
        platform_jwks,
        algorithms=["RS256"],
        audience=platform.lti_client_id,
        # Our registration, not the token's own claim (which found the platform).
        issuer=platform.lti_issuer,
    )
    # OIDC Core 3.1.3.7 / LTI 1.3 5.1.2: with several audiences azp is required, and
    # when present it must be our client id.
    azp = claims.get("azp")
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and azp is None:
        raise ValueError("LTI id_token has several audiences but no azp")
    if azp is not None and azp != platform.lti_client_id:
        raise ValueError("LTI id_token azp is not this tool's client id")
    if str(claims.get(CLAIM_DEPLOYMENT, "")) != platform.lti_deployment_id:
        raise ValueError("LTI deployment id does not match the registered platform")

    # Replay protection: nonce must exist with matching state, have been issued for
    # this platform's login, then be consumed.
    record = (
        db.query(LTINonce)
        .filter(
            LTINonce.nonce == claims.get("nonce", ""),
            LTINonce.state == state,
            LTINonce.platform_id == platform.id,
        )
        .first()
    )
    if not record:
        raise ValueError("Unknown or replayed LTI nonce/state")
    expires = record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        db.delete(record)
        db.commit()
        raise ValueError("Expired LTI login — restart the launch from the platform")
    db.delete(record)
    db.commit()

    return platform, claims


def parse_resource_target(claims: dict) -> tuple[str, str]:
    """Extract (resource_kind, resource_id) from the custom claim.

    Convention: deep-linked items carry custom = {"resource": "quiz:<uuid>"}
    (or exercise:/course:). Defaults to the training portal.
    """
    custom = claims.get(CLAIM_CUSTOM) or {}
    resource = str(custom.get("resource", ""))
    if ":" in resource:
        kind, _, rid = resource.partition(":")
        # lab: "<course uuid>:mod_NNN"; cmi5: "<release uuid>:<AU index>" (app/cmi5/lti.py)
        if kind in ("quiz", "exercise", "course", "lab", "cmi5"):
            return kind, rid
    return "", ""


def record_launch(
    db: Session,
    platform: ExternalPlatform,
    user_id: uuid.UUID,
    claims: dict,
    resource_kind: str,
    resource_id: str,
    *,
    grade_passback: bool = True,
) -> LTILaunch:
    """Record a resource-link launch. ``grade_passback`` False (the account was matched by
    email, not bound to this LMS account: ``lti_identity.links.is_bound``) drops the AGS
    claim, so no grade is ever sent to that LMS account's gradebook cell."""
    ags = (claims.get(CLAIM_AGS) or {}) if grade_passback else {}
    resource_link = claims.get(CLAIM_RESOURCE_LINK) or {}
    context = claims.get(CLAIM_CONTEXT) or {}
    launch = LTILaunch(
        platform_id=platform.id,
        user_id=user_id,
        lti_user_sub=str(claims.get("sub", "")),
        resource_kind=resource_kind,
        resource_id=resource_id,
        resource_link_id=str(resource_link.get("id", "")),
        context_title=str(context.get("title", ""))[:500],
        ags_lineitem_url=str(ags.get("lineitem", "")),
        ags_scopes=json.dumps(ags.get("scope", [])),
    )
    db.add(launch)
    db.commit()
    db.refresh(launch)
    return launch


# ── Deep Linking ─────────────────────────────────────────────────────────


def build_deep_link_response(
    db: Session,
    platform: ExternalPlatform,
    deployment_id: str,
    content_items: list[dict],
    deep_link_return_data: str | None,
) -> str:
    """Sign a LtiDeepLinkingResponse JWT containing the selected content items."""
    key = get_tool_key(db)
    now = int(time.time())
    payload = {
        "iss": platform.lti_client_id,
        "aud": platform.lti_issuer,
        "iat": now,
        "exp": now + 600,
        "nonce": secrets.token_urlsafe(16),
        CLAIM_MESSAGE_TYPE: "LtiDeepLinkingResponse",
        "https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0",
        CLAIM_DEPLOYMENT: deployment_id,
        CLAIM_DL_CONTENT_ITEMS: content_items,
    }
    if deep_link_return_data:
        payload["https://purl.imsglobal.org/spec/lti-dl/claim/data"] = deep_link_return_data
    return jwt.encode(
        payload, signing_pem(key), algorithm="RS256", headers={"kid": key.kid}
    )


def content_item_for(kind: str, resource_id: str, title: str, max_score: int = 100) -> dict:
    return {
        "type": "ltiResourceLink",
        "title": title,
        "url": f"{TOOL_BASE_URL}/lti/launch",
        "custom": {"resource": f"{kind}:{resource_id}"},
        "lineItem": {"scoreMaximum": max_score, "label": title},
    }


# ── AGS grade pass-back ──────────────────────────────────────────────────


class AGSError(Exception):
    """A score the platform did not take. ``transient``: worth sending again later (the
    platform or the network was unavailable, or asked us to slow down)."""

    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


def _transient_status(code: int) -> bool:
    return code in (408, 425, 429) or code >= 500


def _allow_private() -> bool:
    """Server-side calls to a platform may reach private addresses only where the platform
    test probe may (``INTEGRATION_ALLOW_PRIVATE_URLS``): a farm's internal Moodle."""
    return os.getenv("INTEGRATION_ALLOW_PRIVATE_URLS", "false").strip().lower() in ("1", "true", "yes", "on")


async def _guarded_post(platform: ExternalPlatform, url: str, **kw) -> net_guard.Answer:
    """POST to one of the platform's URLs through app.net_guard (vetted, pinned, no redirects),
    rerouted to its internal address as ``platform_route`` says. AGSError on failure."""
    target, route_headers = platform_route(platform, url)
    headers = {**kw.pop("headers", {}), **route_headers}
    try:
        return await asyncio.to_thread(
            net_guard.post, target, headers=headers, allow_private=_allow_private(), timeout=20.0, **kw
        )
    except net_guard.DestinationRefusedError as exc:
        raise AGSError("the platform's address is not one TrueNorth may call", transient=False) from exc
    except net_guard.TooLargeError as exc:
        raise AGSError("the platform's answer was too large", transient=False) from exc
    except net_guard.GuardError as exc:
        raise AGSError("the platform could not be reached", transient=True) from exc


# Access tokens per (platform, token URL, client, signing key), until shortly before they
# expire: one token request per platform and hour, not one per score.
_TOKENS: dict[tuple[str, str, str, str], tuple[str, float]] = {}
_TOKEN_MARGIN = 60.0


def _forget_token(platform: ExternalPlatform) -> None:
    for k in [k for k in _TOKENS if k[0] == str(platform.id)]:
        _TOKENS.pop(k, None)


async def _ags_access_token(db: Session, platform: ExternalPlatform) -> str:
    """client_credentials grant with a private_key_jwt assertion (cached until it expires)."""
    if not platform.lti_token_url:
        raise AGSError("the platform has no token URL configured", transient=False)
    key = get_tool_key(db)
    cache_key = (str(platform.id), platform.lti_token_url, platform.lti_client_id or "", key.kid)
    cached = _TOKENS.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": platform.lti_client_id,
            "sub": platform.lti_client_id,
            "aud": platform.lti_token_url,
            "iat": now,
            "exp": now + 300,
            "jti": secrets.token_urlsafe(16),
        },
        signing_pem(key),
        algorithm="RS256",
        headers={"kid": key.kid},
    )
    # The assertion's aud stays the public token URL: that is what the platform checks.
    answer = await _guarded_post(
        platform,
        platform.lti_token_url,
        data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "scope": AGS_SCORE_SCOPE,
        },
    )
    if not 200 <= answer.status < 300:
        raise AGSError(f"the token endpoint answered {answer.status}", transient=_transient_status(answer.status))
    try:
        body = json.loads(answer.body)
        token = str(body["access_token"])
        lifetime = float(body.get("expires_in") or 3600)
    except (ValueError, KeyError, TypeError) as exc:
        raise AGSError("the token endpoint gave no access token", transient=False) from exc
    _TOKENS[cache_key] = (token, time.time() + max(lifetime - _TOKEN_MARGIN, 0.0))
    return token


def scores_url_for(lineitem_url: str) -> str:
    """The AGS scores endpoint of a lineitem: ``/scores`` goes before any query string."""
    if "?" in lineitem_url:
        base, _, query = lineitem_url.partition("?")
        return f"{base}/scores?{query}"
    return f"{lineitem_url}/scores"


async def send_score(db: Session, platform: ExternalPlatform, lineitem_url: str, user_sub: str, score: dict) -> None:
    """POST one AGS score (``score``: the Score fields without ``userId``) to a lineitem of
    ``platform`` for the platform's user ``user_sub``. Raises AGSError for anything the
    platform or the network did. Both requests go through app.net_guard."""
    token = await _ags_access_token(db, platform)
    answer = await _guarded_post(
        platform,
        scores_url_for(lineitem_url),
        json_body={**score, "userId": user_sub},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/vnd.ims.lis.v1.score+json"},
    )
    if answer.status == 401:
        _forget_token(platform)  # revoked or rotated: the next attempt asks for a new one
    if not 200 <= answer.status < 300:
        raise AGSError(f"the platform answered {answer.status}", transient=_transient_status(answer.status))


async def push_score(
    db: Session,
    launch: LTILaunch,
    score: float,
    max_score: float,
    activity_progress: str = "Completed",
    grading_progress: str = "FullyGraded",
) -> bool:
    """POST a score to the platform's AGS lineitem for this launch."""
    if not launch.ags_lineitem_url:
        return False
    platform = db.get(ExternalPlatform, launch.platform_id)  # tenant-safe: the launch's own platform
    if not platform:
        return False
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "scoreGiven": score,
        "scoreMaximum": max_score,
        "activityProgress": activity_progress,
        "gradingProgress": grading_progress,
    }
    try:
        await send_score(db, platform, launch.ags_lineitem_url, launch.lti_user_sub, payload)
    except Exception as exc:  # advisory, as before: never fails the caller's path
        logger.warning("AGS score push failed for launch %s: %s", launch.id, exc)
        return False
    logger.info("AGS score pushed: %s %s/%s", launch.id, score, max_score)
    return True


async def push_score_for_resource(
    db: Session, user_id: uuid.UUID, resource_kind: str, resource_id: str, score: float, max_score: float
) -> bool:
    """Find the most recent LTI launch for this user+resource, by an LMS account bound to
    this user (never one matched by email: lti_identity.links.is_bound), and push the grade."""
    from .lti_identity.links import is_bound

    user = db.get(User, user_id)  # tenant-safe: the caller's own subject; only compared below
    if user is None:
        return False
    launches = (
        db.query(LTILaunch)
        .filter(
            LTILaunch.user_id == user_id,
            LTILaunch.resource_kind == resource_kind,
            LTILaunch.resource_id == str(resource_id),
            LTILaunch.ags_lineitem_url != "",
        )
        .order_by(LTILaunch.created_at.desc())
        .limit(20)
        .all()
    )
    launch = next((la for la in launches if is_bound(db, la.platform_id, la.lti_user_sub, user)), None)
    if not launch:
        return False
    return await push_score(db, launch, score, max_score)
