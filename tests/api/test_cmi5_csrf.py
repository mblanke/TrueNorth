"""With auth on, the cmi5 token routes are not CSRF-checked, and every other cmi5 route is.

The fetch URL (a one-time secret in its path) and the AU's xAPI endpoint (the session token
as Basic credentials) read no cookie, and an AU on another origin cannot hold TrueNorth's
CSRF cookie: checking them would refuse every AU. Launch, waive and abandon are ordinary
signed-in calls and keep the check. Built like tests/api/test_csrf_exemptions.py: the
production middleware stack (AUTH_DISABLED=false) around the real router.
"""

from __future__ import annotations

import pytest
from app.cmi5.router import router as cmi5_router
from app.db import get_db
from app.middleware import CSRF_EXEMPT_PREFIXES, redact_path, setup_middleware
from fastapi import FastAPI
from fastapi.testclient import TestClient

CSRF_403 = {"detail": "CSRF token missing or invalid"}


@pytest.fixture
def prod_app(monkeypatch, db_session):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    app = FastAPI()
    setup_middleware(app, redis_url=None)
    app.include_router(cmi5_router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_the_fetch_url_reaches_its_handler(prod_app):
    r = prod_app.post("/cmi5/fetch/" + "s" * 43)
    assert r.status_code == 200 and r.json()["error-code"] == "2"


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_the_au_endpoint_reaches_its_credential_check(prod_app, method):
    r = getattr(prod_app, method)("/cmi5/lrs/statements", headers={"Authorization": "Basic bm86bm8="})
    assert r.json() != CSRF_403 and r.status_code == 401


def test_signed_in_cmi5_routes_still_need_the_token(prod_app):
    rid = "00000000-0000-0000-0000-0000000000aa"
    assert prod_app.post(f"/cmi5/releases/{rid}/aus/0/launch", json={}).json() == CSRF_403
    assert prod_app.post(f"/cmi5/registrations/{rid}/aus/0/waive", json={"reason": "Tested Out"}).json() == CSRF_403
    assert prod_app.post(f"/cmi5/sessions/{rid}/abandon").json() == CSRF_403


def test_the_exempt_prefixes_are_exactly_these():
    assert CSRF_EXEMPT_PREFIXES == ("/cmi5/fetch/", "/cmi5/lrs/")


def test_fetch_secrets_are_not_logged():
    assert redact_path("/cmi5/fetch/abcDEF_123-x") == "/cmi5/fetch/<redacted>"
    assert redact_path("/schedule/feed/tok.ics") == "/schedule/feed/<redacted>"
