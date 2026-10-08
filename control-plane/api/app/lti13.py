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
from .models import ExternalPlatform, LTILaunch, LTINonce, LTIToolKey
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
        if kind in ("quiz", "exercise", "course", "lab"):  # lab: "<course uuid>:mod_NNN"
            return kind, rid
    return "", ""


def record_launch(
    db: Session,
    platform: ExternalPlatform,
    user_id: uuid.UUID,
    claims: dict,
    resource_kind: str,
    resource_id: str,
) -> LTILaunch:
    ags = claims.get(CLAIM_AGS) or {}
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


async def _ags_access_token(db: Session, platform: ExternalPlatform) -> str:
    """client_credentials grant with a private_key_jwt assertion."""
    if not platform.lti_token_url:
        raise ValueError("Platform has no token URL configured")
    key = get_tool_key(db)
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
    token_url, token_headers = platform_route(platform, platform.lti_token_url)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            token_url,
            headers=token_headers,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
                "scope": AGS_SCORE_SCOPE,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


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
    platform = db.get(ExternalPlatform, launch.platform_id)
    if not platform:
        return False
    try:
        token = await _ags_access_token(db, platform)
        scores_url = launch.ags_lineitem_url
        # Per AGS spec the /scores segment goes before any query string.
        if "?" in scores_url:
            base, _, query = scores_url.partition("?")
            scores_url = f"{base}/scores?{query}"
        else:
            scores_url = f"{scores_url}/scores"
        scores_url, route_headers = platform_route(platform, scores_url)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "scoreGiven": score,
            "scoreMaximum": max_score,
            "activityProgress": activity_progress,
            "gradingProgress": grading_progress,
            "userId": launch.lti_user_sub,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                scores_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/vnd.ims.lis.v1.score+json",
                    **route_headers,
                },
            )
            resp.raise_for_status()
        logger.info("AGS score pushed: %s %s/%s", launch.id, score, max_score)
        return True
    except Exception as exc:
        logger.warning("AGS score push failed for launch %s: %s", launch.id, exc)
        return False


async def push_score_for_resource(
    db: Session, user_id: uuid.UUID, resource_kind: str, resource_id: str, score: float, max_score: float
) -> bool:
    """Find the most recent LTI launch for this user+resource and push the grade."""
    launch = (
        db.query(LTILaunch)
        .filter(
            LTILaunch.user_id == user_id,
            LTILaunch.resource_kind == resource_kind,
            LTILaunch.resource_id == str(resource_id),
            LTILaunch.ags_lineitem_url != "",
        )
        .order_by(LTILaunch.created_at.desc())
        .first()
    )
    if not launch:
        return False
    return await push_score(db, launch, score, max_score)
