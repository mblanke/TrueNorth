"""Moodle with the local_truenorth plugin (infra/platform/moodle/local_truenorth).

Every call is one POST to ``/local/truenorth/api.php`` carrying a ticket signed with the
TrueNorth LTI tool key: ``typ`` "sync", single-use ``jti``, 60-second life, audience the
Moodle's wwwroot (``lti_issuer``), ``tid`` the platform's tenant (the Moodle refuses any
other tenant) and bound to the request body by its SHA-256. Moodle
already trusts that key for LTI, so no Moodle token is stored in TrueNorth.

The request goes to ``base_url`` (the address the API reaches, e.g. a container name) with
the wwwroot's Host header, so a Moodle behind a private name still sees its own site URL.

``pull_results`` is the one call whose answer TrueNorth acts on as data about people, so
the answer is signed too: by the Moodle's LTI site key, verified against the JWKS
registered for the platform (``lti_jwks_url``, the trust anchor of LTI launches), issued
by the platform's ``lti_issuer`` for TrueNorth, naming the platform's tenant and the jti
of the ticket it answers. An unsigned, re-signed or replayed answer is refused.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
import jwt

from .base import BaseMoodleBackend, MoodleError

API_PATH = "/local/truenorth/api.php"
TICKET_SECONDS = 60
TIMEOUT = 180  # creating a course with questions takes Moodle a while
RESULTS_AUDIENCE = "truenorth"  # local_truenorth's ssoissuer: the name TrueNorth signs as
RESULTS_SETTLE_SECONDS = 5  # rows younger than this are left for the next pull

# (private key PEM, kid) — the platform's signing key, supplied by the caller.
KeyProvider = Callable[[], tuple[str, str]]


class LocalTrueNorthMoodle(BaseMoodleBackend):
    kind: ClassVar[str] = "moodle"

    def __init__(self, key_provider: KeyProvider | None = None, transport: httpx.BaseTransport | None = None):
        self._key_provider = key_provider
        self._transport = transport

    def upsert_course(self, platform: Any, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(platform, {"op": "upsert_course", "course": payload})

    def describe_course(self, platform: Any, idnumber: str) -> dict[str, Any]:
        return self._post(platform, {"op": "describe_course", "idnumber": idnumber})

    def set_visible(self, platform: Any, idnumber: str, visible: bool) -> dict[str, Any]:
        return self._post(platform, {"op": "set_visible", "idnumber": idnumber, "visible": visible})

    def delete_stage(self, platform: Any, idnumber: str) -> dict[str, Any]:
        return self._post(platform, {"op": "delete_stage", "idnumber": idnumber})

    def pull_results(self, platform: Any, cursor: str, limit: int = 500) -> dict[str, Any]:
        if not platform.lti_jwks_url:
            raise MoodleError("this Moodle has no LTI key set (lti_jwks_url) registered, so its results cannot be verified")
        request = {"op": "pull_results", "cursor": cursor, "limit": limit, "settle": RESULTS_SETTLE_SECONDS}
        data, jti = self._call(platform, request)
        claims = self._verify_answer(platform, str(data.get("signed") or ""), jti)
        results = claims.get("results")
        if not isinstance(results, dict) or not isinstance(results.get("rows"), list):
            raise MoodleError("Moodle's signed results are malformed")
        return {"rows": results["rows"], "cursor": str(results.get("cursor") or ""), "more": bool(results.get("more"))}

    def _verify_answer(self, platform: Any, token: str, jti: str) -> dict[str, Any]:
        from .. import jwks as jwks_verify
        from ..lti13 import platform_route

        if not token:
            raise MoodleError("Moodle's results are not signed; refused")
        url, headers = platform_route(platform, platform.lti_jwks_url)
        try:
            with httpx.Client(timeout=20, transport=self._transport) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                keyset = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MoodleError(f"Moodle's LTI key set could not be fetched: {exc}") from exc
        issuer = (platform.lti_issuer or "").rstrip("/")
        try:
            claims = jwks_verify.decode(
                token, keyset, algorithms=["RS256"], audience=RESULTS_AUDIENCE, issuer=issuer
            )
        except jwt.PyJWTError as exc:
            raise MoodleError(f"Moodle's results are not signed by its registered key; refused ({exc})") from exc
        if claims.get("typ") != "results" or str(claims.get("tid")) != str(platform.tenant_id):
            raise MoodleError("Moodle's signed results are for another purpose or tenant; refused")
        if claims.get("req") != jti:
            raise MoodleError("Moodle's signed results answer another request (replayed); refused")
        return claims

    def _ticket(self, audience: str, body: bytes, tenant: str) -> tuple[str, str]:
        if self._key_provider is None:
            raise MoodleError("no TrueNorth signing key is configured for Moodle calls")
        private_pem, kid = self._key_provider()
        now = int(time.time())
        jti = uuid.uuid4().hex
        claims = {
            "iss": "truenorth",
            "typ": "sync",
            "aud": audience,
            "iat": now,
            "exp": now + TICKET_SECONDS,
            "jti": jti,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            # Every tenant's Moodle trusts this one key; the Moodle accepts a sync ticket
            # only for the tenant it serves (local_truenorth tenantid), so a job cannot be
            # replayed into another tenant's site.
            "tid": tenant,
        }
        return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid}), jti

    def _post(self, platform: Any, request: dict[str, Any]) -> dict[str, Any]:
        return self._call(platform, request)[0]

    def _call(self, platform: Any, request: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The plugin's answer and the jti of the ticket that asked."""
        wwwroot = (platform.lti_issuer or platform.base_url or "").rstrip("/")
        if not wwwroot:
            raise MoodleError("this Moodle has no site address (lti_issuer) registered")
        target = (platform.base_url or wwwroot).rstrip("/")
        body = json.dumps(request, separators=(",", ":")).encode()
        ticket, jti = self._ticket(wwwroot, body, str(platform.tenant_id))
        headers = {"Authorization": f"Bearer {ticket}", "Content-Type": "application/json"}
        public, private = urlparse(wwwroot), urlparse(target)
        if public.netloc and public.netloc != private.netloc:
            headers["Host"] = public.netloc
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
                resp = client.post(target + API_PATH, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise MoodleError(f"Moodle could not be reached: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not data.get("ok"):
            detail = data.get("detail") or data.get("error") or f"HTTP {resp.status_code}"
            raise MoodleError(f"Moodle refused {request['op']}: {detail}")
        data.pop("ok", None)
        return data, jti
