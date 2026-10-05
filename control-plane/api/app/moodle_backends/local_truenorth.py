"""Moodle with the local_truenorth plugin (infra/platform/moodle/local_truenorth).

Every call is one POST to ``/local/truenorth/api.php`` carrying a ticket signed with the
TrueNorth LTI tool key: ``typ`` "sync", single-use ``jti``, 60-second life, audience the
Moodle's wwwroot (``lti_issuer``), ``tid`` the platform's tenant (the Moodle refuses any
other tenant) and bound to the request body by its SHA-256. Moodle
already trusts that key for LTI, so no Moodle token is stored in TrueNorth.

The request goes to ``base_url`` (the address the API reaches, e.g. a container name) with
the wwwroot's Host header, so a Moodle behind a private name still sees its own site URL.
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

    def _ticket(self, audience: str, body: bytes, tenant: str) -> str:
        if self._key_provider is None:
            raise MoodleError("no TrueNorth signing key is configured for Moodle calls")
        private_pem, kid = self._key_provider()
        now = int(time.time())
        claims = {
            "iss": "truenorth",
            "typ": "sync",
            "aud": audience,
            "iat": now,
            "exp": now + TICKET_SECONDS,
            "jti": uuid.uuid4().hex,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            # Every tenant's Moodle trusts this one key; the Moodle accepts a sync ticket
            # only for the tenant it serves (local_truenorth tenantid), so a job cannot be
            # replayed into another tenant's site.
            "tid": tenant,
        }
        return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})

    def _post(self, platform: Any, request: dict[str, Any]) -> dict[str, Any]:
        wwwroot = (platform.lti_issuer or platform.base_url or "").rstrip("/")
        if not wwwroot:
            raise MoodleError("this Moodle has no site address (lti_issuer) registered")
        target = (platform.base_url or wwwroot).rstrip("/")
        body = json.dumps(request, separators=(",", ":")).encode()
        ticket = self._ticket(wwwroot, body, str(platform.tenant_id))
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
        return data
