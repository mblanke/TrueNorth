# Author: Marc-Andre Blanke
# Created: 2026-11-26T12:00:00Z
"""A read never changes the CSRF token a page is about to send.

Bug (journeys lane on main, 2026-11-25, "CSRF token missing or invalid" on a ticket's
PATCH): CsrfMiddleware issued a NEW token on every GET response. The SPA reads the cookie
into the X-CSRF-Token header, then waits for its access token, then sends
(core/interceptors/auth.interceptor.ts); the browser attaches whatever cookie it holds at
that moment. Any GET answered in between replaced the cookie, so header and cookie no
longer matched and the write was refused with 403. A page that reads while someone clicks
(every page that polls) could lose the click.

These build the production middleware stack (``setup_middleware`` with AUTH_DISABLED=false).
"""

from __future__ import annotations

import pytest
from app.middleware import setup_middleware
from fastapi import FastAPI
from fastapi.testclient import TestClient

COOKIE = "truenorth_csrf"
CSRF_403 = {"detail": "CSRF token missing or invalid"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    app = FastAPI()
    setup_middleware(app, redis_url=None)

    @app.get("/read")
    def read():
        return {"ok": True}

    @app.post("/write")
    def write():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_a_read_in_flight_does_not_invalidate_the_token_a_write_already_read(client):
    client.get("/read")
    token = client.cookies.get(COOKIE)
    assert token
    # The page has read `token` for its header. Before it sends, other reads are answered.
    for _ in range(3):
        client.get("/read")
    response = client.post("/write", headers={"X-CSRF-Token": token})
    assert response.status_code == 200, response.text
    assert client.cookies.get(COOKIE) == token


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_response_to_a_request_with_a_valid_cookie_sets_no_cookie(client, method):
    """No Set-Cookie at all, not even the same value: a read sent with an older cookie and
    answered late would otherwise put the older value back over a newer one."""
    client.get("/read")
    token = client.cookies.get(COOKIE)
    answer = getattr(client, method)(f"/{'read' if method == 'get' else 'write'}", headers={"X-CSRF-Token": token})
    assert answer.status_code == 200
    assert "set-cookie" not in answer.headers


def test_reads_that_overlap_before_any_cookie_exists_settle_on_one_token(client):
    """Two first reads leave with no cookie and each is given a token; the browser keeps the
    one that landed last. Reads sent after that carry it, and none of them changes it."""
    first = client.get("/read", headers={"Cookie": ""}).cookies.get(COOKIE)
    client.cookies.clear()
    kept = client.get("/read").cookies.get(COOKIE)
    assert first and kept and first != kept
    for _ in range(3):
        assert "set-cookie" not in client.get("/read").headers
    assert client.cookies.get(COOKIE) == kept
    assert client.post("/write", headers={"X-CSRF-Token": kept}).status_code == 200


def test_the_cookie_is_a_session_cookie_the_page_can_read(client):
    set_cookie = client.get("/read").headers["set-cookie"]
    lowered = set_cookie.lower()
    assert "max-age" not in lowered and "expires" not in lowered, set_cookie  # nothing expires under an open page
    assert "samesite=strict" in lowered and "path=/" in lowered and "httponly" not in lowered, set_cookie


@pytest.mark.parametrize(
    "planted",
    ["", "not-a-token", "0123456789abcdef0123456789abcdef.0000000000000000", "abc.\u00e9", "\u00e9.0000000000000000"],
)
def test_a_cookie_this_server_did_not_sign_is_replaced_on_a_read(client, planted):
    """A missing, malformed, forged or non-ASCII cookie is never kept and never an error:
    the next read answers normally and issues a real one."""
    answer = client.get("/read", headers={b"Cookie": f"{COOKIE}={planted}".encode()})  # bytes, as on the wire
    assert answer.status_code == 200, answer.text
    token = answer.cookies.get(COOKIE)
    assert token and token != planted
    assert client.post("/write", headers={"X-CSRF-Token": token}).status_code == 200


def test_a_non_ascii_cookie_on_a_write_is_refused_not_an_error(client):
    planted = "abc.\u00e9".encode()
    response = client.post("/write", headers={b"Cookie": f"{COOKIE}=".encode() + planted, b"X-CSRF-Token": planted})
    assert response.status_code == 403 and response.json() == CSRF_403


def test_a_valid_header_with_a_different_valid_cookie_is_refused(client):
    """The token carries no user or session, so header == cookie is the whole defence: a
    token an attacker obtained for themselves must not pass against the victim's cookie."""
    attackers = client.get("/read").cookies.get(COOKIE)
    client.cookies.clear()
    client.get("/read")
    assert client.cookies.get(COOKIE) != attackers
    response = client.post("/write", headers={"X-CSRF-Token": attackers})
    assert response.status_code == 403 and response.json() == CSRF_403


def test_a_forged_cookie_and_matching_header_are_still_refused(client):
    forged = "0123456789abcdef0123456789abcdef.0000000000000000"
    response = client.post("/write", headers={"X-CSRF-Token": forged, "Cookie": f"{COOKIE}={forged}"})
    assert response.status_code == 403 and response.json() == CSRF_403


def test_a_write_without_the_header_is_still_refused(client):
    client.get("/read")
    response = client.post("/write")
    assert response.status_code == 403 and response.json() == CSRF_403
