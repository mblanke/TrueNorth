"""COV-I: scripts/_inprocess_api.py — the branches the harness tests never reach.

Whole-slice coverage before this file: 75.6% (102/135 statements). Missing were the 429
retry loop (its sleeps and its per-attempt CSRF header), the unparseable-body fallback, the
PATCH/PUT/DELETE shorthands, the IdentitySwitch's no-identity 401, UserApi (construction,
request, raw), Users.api, the inactive-user lookup and connect()'s unknown-administrator
refusal.

The retry and body tests run against a fake client because the script accepts "anything
with ``request`` and ``cookies``" — the same seam the real TestClient occupies in
test_inprocess_api.py; the scripted 429s carry exactly the ``retry_after`` shape the API's
rate limiter answers with. The Users/connect tests use the real in-process app, as
test_inprocess_api.py does.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from app import auth
from app.models import User, UserRole

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _inprocess_api  # noqa: E402


# ── InProcessApi over a scripted client ─────────────────────────────────────────
class _Resp:
    def __init__(self, status: int, body=None, raw: bytes = b""):
        self.status_code = status
        self._body = body
        self.content = raw

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    @property
    def text(self) -> str:
        return self.content.decode(errors="replace")


class _Client:
    """The seam InProcessApi documents: ``request`` plus a rotating cookie jar."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.cookies = {_inprocess_api.CSRF_COOKIE: "tok-0"}
        self.requests: list[tuple] = []
        self._n = 0

    def _next(self) -> _Resp:
        self._n += 1
        self.cookies[_inprocess_api.CSRF_COOKIE] = f"tok-{self._n}"  # rotated by the response
        return self.responses.pop(0)

    def request(self, method, path, **kw):
        self.requests.append((method, path, kw))
        return self._next()

    def get(self, path, **kw):
        return self._next()


def test_a_429_is_retried_after_its_retry_after_with_a_fresh_csrf_header():
    client = _Client([
        _Resp(429, {"retry_after": 2}),
        _Resp(429, raw=b"gateway sludge"),  # a 429 whose body does not parse: wait the default
        _Resp(200, {"ok": True}, raw=b'{"ok": true}'),
    ])
    sleeps: list[float] = []
    api = _inprocess_api.InProcessApi(client, sleep=sleeps.append, max_429=5)
    assert api.post("/things", json={"a": 1}) == (200, {"ok": True})
    assert sleeps == [3, 61]
    csrf = _inprocess_api.CSRF_HEADER
    assert [(m, kw["headers"].get(csrf)) for m, _p, kw in client.requests] == [
        ("POST", "tok-0"), ("POST", "tok-1"), ("POST", "tok-2")]
    assert client.requests[0][2]["headers"]["Content-Type"] == "application/json"


def test_429s_past_the_cap_are_reported_not_endless():
    client = _Client([_Resp(429, {"retry_after": 0}), _Resp(429, {"retry_after": 0})])
    api = _inprocess_api.InProcessApi(client, sleep=lambda s: None, max_429=2)
    assert api.get("/things")[0] == 429
    assert len(client.requests) == 2


def test_a_body_that_does_not_parse_comes_back_as_text():
    client = _Client([_Resp(200, raw=b"<html>gateway error</html>")])
    api = _inprocess_api.InProcessApi(client, sleep=lambda s: None)
    assert api.get("/things") == (200, "<html>gateway error</html>")


def test_the_shorthands_and_a_preencoded_body():
    client = _Client([_Resp(200, {}, raw=b"{}")] * 4)
    api = _inprocess_api.InProcessApi(client, sleep=lambda s: None)
    assert api.patch("/t/1", json={"x": 1})[0] == 200
    assert api.put("/t/1", json={"x": 2})[0] == 200
    assert api.delete("/t/1")[0] == 200
    assert api.post("/t", content=b"raw-bytes")[0] == 200  # no JSON content-type added
    assert [m for m, _p, _kw in client.requests] == ["PATCH", "PUT", "DELETE", "POST"]
    assert client.requests[3][2]["content"] == b"raw-bytes"
    assert "Content-Type" not in client.requests[3][2]["headers"]


# ── IdentitySwitch and UserApi ──────────────────────────────────────────────────
def test_the_switch_answers_401_when_no_identity_is_set():
    from fastapi import HTTPException

    switch = _inprocess_api.IdentitySwitch()
    with pytest.raises(HTTPException) as exc:
        switch()
    assert exc.value.status_code == 401
    ident = object()
    switch.current = ident
    assert switch() is ident


def test_a_user_api_sends_its_own_identity_and_can_fetch_raw():
    client = _Client([_Resp(200, {"me": 1}, raw=b'{"me": 1}'), _Resp(200, raw=b"bytes!")])
    switch = _inprocess_api.IdentitySwitch()
    ident = object()
    api = _inprocess_api.UserApi(client, ident, switch, email="u@example.test", role="admin")
    assert api.email == "u@example.test" and api.role == "admin"
    assert api.get("/users/me") == (200, {"me": 1})
    assert switch.current is ident
    assert api.raw("/export").content == b"bytes!"
    assert switch.current is ident


# ── Users and connect() against the real in-process app ─────────────────────────
class _Shared:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


@pytest.fixture
def harness(monkeypatch, client, db_session):
    import app.db

    monkeypatch.setattr(app.db, "SessionLocal", lambda: _Shared(db_session))
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(sys, "argv", ["cov_i_inprocess.py"])
    _inprocess_api._audited.clear()
    admin = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email="cov-i-admin@example.test",
                 display_name="Cov", role=UserRole.admin,
                 tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
    db_session.add(admin)
    db_session.flush()
    return admin


def test_users_api_wraps_a_user_and_refuses_an_unknown_one(harness):
    users = _inprocess_api.connect_users()
    api = users.api(harness.email)
    assert api.email == harness.email and api.role == "admin"
    assert api.get("/users/me")[0] == 200
    assert users.api("nobody@example.test") is None


def test_an_inactive_user_is_not_lookupable(harness, db_session):
    from app.models import AuditLog

    ghost = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email="ghost@example.test",
                 display_name="Ghost", role=UserRole.admin, is_active=False,
                 tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
    db_session.add(ghost)
    db_session.flush()
    users = _inprocess_api.connect_users()
    assert users.lookup("ghost@example.test") is None
    # No session was audited for the user it refused to act as.
    assert db_session.query(AuditLog).filter_by(resource_id=str(ghost.id)).count() == 0


def test_connect_refuses_an_unknown_administrator(harness, capsys):
    assert _inprocess_api.connect("nobody@example.test") is None
    assert "administrator nobody@example.test not found" in capsys.readouterr().err
