"""Drive the TrueNorth API in-process, as a named administrator, from inside the api image.

Shared by scripts/load_content_inprocess.py and scripts/arc2_batch_inprocess.py. An
installed host has no token to hand a script (direct grants are off), so the token check
is replaced by an existing user's identity; every request still goes through the API's
routes, permission checks, CSRF and validation. No password or token is used.

Two details matter and are easy to get wrong:

* CSRF is double-submit. Every response sets a fresh ``truenorth_csrf`` cookie, so the
  header is read from the cookie jar immediately before each non-GET attempt (including
  each retry after a 429), never once up front.
* Rate limits answer 429 with ``retry_after``; the request is retried after that.
"""

from __future__ import annotations

import json as jsonlib
import sys
import time
from collections.abc import Callable
from typing import Any

CSRF_COOKIE = "truenorth_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class InProcessApi:
    """A thin client over a Starlette TestClient (or anything with ``request`` and
    ``cookies``). Methods return ``(status, body)``; body is parsed JSON when it can be."""

    def __init__(self, client: Any, *, sleep: Callable[[float], None] = time.sleep, max_429: int = 20):
        self.client = client
        self.sleep = sleep
        self.max_429 = max_429

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        files: Any = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, Any]:
        method = method.upper()
        if json is not None:
            content = jsonlib.dumps(json).encode()
            headers = {**(headers or {}), "Content-Type": "application/json"}
        r = None
        for _ in range(self.max_429):
            hdrs = dict(headers or {})
            if method not in SAFE_METHODS:
                # Read now: the previous response (a GET, or the 429 itself) rotated it.
                hdrs[CSRF_HEADER] = self.client.cookies.get(CSRF_COOKIE, "")
            r = self.client.request(method, path, content=content, files=files, params=params, headers=hdrs)
            if r.status_code != 429:
                break
            try:
                wait = int((r.json() or {}).get("retry_after", 60))
            except (ValueError, AttributeError, TypeError):
                wait = 60
            self.sleep(wait + 1)
        try:
            return r.status_code, (r.json() if r.content else None)
        except ValueError:
            return r.status_code, r.text[:400]

    def get(self, path: str, **kw: Any) -> tuple[int, Any]:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> tuple[int, Any]:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> tuple[int, Any]:
        return self.request("PATCH", path, **kw)


def connect(admin_upn: str) -> tuple[Any, InProcessApi] | None:
    """Sign in as ``admin_upn`` (an existing user) and return ``(TestClient, InProcessApi)``,
    or None when there is no such user. Imports the app, so it runs inside the api image."""
    from app import auth
    from app.db import SessionLocal
    from app.main import app
    from app.models import User
    from fastapi.testclient import TestClient

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == admin_upn).first()
        if user is None:
            print(f"administrator {admin_upn} not found", file=sys.stderr)
            return None
        ident = auth.TokenPayload(
            sub=user.keycloak_id,
            email=user.email,
            name=user.display_name or user.email,
            preferred_username=user.email,
        )
    finally:
        db.close()
    app.dependency_overrides[auth.get_token_identity] = lambda: ident

    client = TestClient(app, base_url="https://api.internal", raise_server_exceptions=False)
    client.get("/health/live")  # the CSRF middleware sets truenorth_csrf on every response
    return client, InProcessApi(client)
