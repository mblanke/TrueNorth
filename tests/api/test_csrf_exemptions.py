"""With auth on, CSRF does not block the cross-site POSTs that carry their own signature.

Release blocker (2026-10-08): with AUTH_DISABLED=false, CsrfMiddleware answered 403 to
POST /lti/login, /lti/launch, /lti/deeplink/finish (posted by the LMS, cross-site, with
no TrueNorth cookie) and /noise/agent/report (posted by an agent with a bearer token).
No LTI launch and no noise report could ever succeed in production. The suite runs with
AUTH_DISABLED=true, which turns CSRF off, so nothing caught it.

These build the production middleware stack (``setup_middleware`` with
AUTH_DISABLED=false) around the real routers.
"""

from __future__ import annotations

import pytest
from app.db import get_db
from app.middleware import CSRF_EXEMPT_PATHS, setup_middleware
from app.routers.integrations import lti_router
from app.routers.noise import router as noise_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

CSRF_403 = {"detail": "CSRF token missing or invalid"}


@pytest.fixture
def prod_app(monkeypatch, db_session):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    app = FastAPI()
    setup_middleware(app, redis_url=None)
    app.include_router(lti_router)
    app.include_router(noise_router)

    @app.post("/something/else")
    def other():
        return {"ok": True}

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/lti/login", {"data": {"iss": "https://lms.example", "login_hint": "u", "target_link_uri": "https://x"}}),
        ("/lti/launch", {"data": {"id_token": "not-a-jwt", "state": "s"}}),
        ("/lti/deeplink/finish", {"data": {"session": "not-a-jwt", "selections": "[]"}}),
        ("/noise/agent/report", {"json": {"results": []}, "headers": {"Authorization": "Bearer wrong"}}),
    ],
)
def test_self_authenticated_cross_site_posts_reach_their_handler(prod_app, path, kwargs):
    r = prod_app.post(path, **kwargs)
    # The handler ran and judged its own credentials (refusing these fakes), rather than
    # CSRF refusing the request before it got there.
    assert r.json() != CSRF_403, path
    assert r.status_code != 500, r.text


def test_every_other_post_still_needs_the_csrf_token(prod_app):
    assert prod_app.post("/something/else").status_code == 403
    assert prod_app.post("/something/else").json() == CSRF_403
    assert prod_app.post("/noise/ranges/x/deploy").json() == CSRF_403  # same router, not exempt
    assert prod_app.post("/lti/grades").json() == CSRF_403


def test_the_exemption_is_exact_paths_only():
    expected = {"/lti/login", "/lti/launch", "/lti/deeplink/finish", "/noise/agent/report"}
    assert set(CSRF_EXEMPT_PATHS) == expected
