"""The calendar feed (ADR 0004 slice 5): per-user bearer-token URL, hash-only storage,
revocable, role re-checked on every fetch, tenant-scoped, and never logged."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.middleware import RedactSecretPaths, redact_path
from app.models import User, UserRole
from app.scheduler.models import EventState, FeedToken, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
SOON = (datetime.now(UTC) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)


def _user(db, role: UserRole = UserRole.instructor, tenant: str = DEV_TENANT) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=uuid.UUID(tenant),
    )
    db.add(u)
    db.flush()
    return u


@contextmanager
def signed_in(u: User):
    who = CurrentUser(
        id=str(u.id),
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        tenant_id=str(u.tenant_id),
        keycloak_id=u.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _booking(db, name: str, tenant: str = DEV_TENANT, state=EventState.scheduled, start=SOON) -> ScheduledEvent:
    ev = ScheduledEvent(
        name=name, state=state, tenant_id=uuid.UUID(tenant), start_time=start, end_time=start + timedelta(hours=2)
    )
    db.add(ev)
    db.flush()
    return ev


def _issue(client, u: User) -> dict:
    with signed_in(u):
        r = client.post("/schedule/feed-token")
    assert r.status_code == 200, r.text
    return r.json()


def _path(url: str) -> str:
    """The server-side path: the API strips /api/v1 (VersionPrefixMiddleware), as here."""
    return url.split("/api/v1", 1)[1]


def test_a_staff_member_gets_a_working_feed_url_once(client, db_session):
    u = _user(db_session)
    _booking(db_session, "Blue team drill")
    issued = _issue(client, u)
    assert issued["url"].startswith("http") and issued["url"].endswith(".ics")
    assert issued["webcal_url"].startswith("webcal://")
    assert issued["webcal_url"].split("://", 1)[1] == issued["url"].split("://", 1)[1]

    r = client.get(_path(issued["url"]))  # no sign-in: calendar clients cannot
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "SUMMARY:Blue team drill" in r.text

    with signed_in(u):
        status = client.get("/schedule/feed-token").json()
    assert status["active"] is True
    assert "url" not in status  # the token is never shown again


def test_only_a_hash_of_the_token_is_stored(client, db_session):
    u = _user(db_session)
    token = _path(_issue(client, u)["url"]).rsplit("/", 1)[1].removesuffix(".ics")
    row = db_session.get(FeedToken, u.id)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row.token_hash
    assert len(token) >= 40  # 256 bits, url-safe


def test_regenerating_kills_the_old_url_and_revoking_kills_the_new_one(client, db_session):
    u = _user(db_session)
    old = _path(_issue(client, u)["url"])
    new = _path(_issue(client, u)["url"])
    assert client.get(old).status_code == 404
    assert client.get(new).status_code == 200
    with signed_in(u):
        assert client.delete("/schedule/feed-token").status_code == 204
    assert client.get(new).status_code == 404


def test_a_student_can_have_a_feed_of_their_own_sessions(client, db_session):
    """Decided 2026-10-06: Students get their own sessions in Outlook (see test_scheduler_students)."""
    with signed_in(_user(db_session, UserRole.student)):
        assert client.post("/schedule/feed-token").status_code == 200


def test_losing_staff_access_narrows_the_feed_to_your_own_sessions(client, db_session):
    u = _user(db_session)
    _booking(db_session, "Someone else's class")
    url = _path(_issue(client, u)["url"])
    assert "Someone else's class" in client.get(url).text
    u.role = UserRole.student
    db_session.flush()
    r = client.get(url)
    assert r.status_code == 200
    assert "Someone else's class" not in r.text


def test_a_deactivated_account_closes_the_feed(client, db_session):
    u = _user(db_session)
    url = _path(_issue(client, u)["url"])
    u.is_active = False
    db_session.flush()
    assert client.get(url).status_code == 404


def test_unknown_tokens_get_the_same_404(client):
    assert client.get("/schedule/feed/not-a-real-token.ics").status_code == 404
    assert client.get(f"/schedule/feed/{'x' * 500}.ics").status_code == 404


def test_the_feed_holds_only_the_owners_tenant(client, db_session):
    u = _user(db_session)
    _booking(db_session, "Ours")
    _booking(db_session, "Theirs", tenant=OTHER_TENANT)
    body = client.get(_path(_issue(client, u)["url"])).text
    assert "SUMMARY:Ours" in body
    assert "Theirs" not in body


def test_drafts_are_left_out_and_cancellations_are_announced(client, db_session):
    u = _user(db_session)
    _booking(db_session, "Draft", state=EventState.draft)
    _booking(db_session, "Called off", state=EventState.cancelled)
    body = client.get(_path(_issue(client, u)["url"])).text
    assert "Draft" not in body
    block = body.split("SUMMARY:Called off")[0].rsplit("BEGIN:VEVENT", 1)[1]
    assert "STATUS:CANCELLED" in block


def test_rescheduling_and_cancelling_raise_the_sequence(client, db_session):
    u = _user(db_session)
    with signed_in(u):
        ev = client.post(
            "/schedule/events",
            json={"name": "Drill", "start_time": SOON.isoformat(), "end_time": (SOON + timedelta(hours=1)).isoformat()},
        ).json()
        client.put(
            f"/schedule/events/{ev['id']}",
            json={
                "name": "Drill",
                "start_time": (SOON + timedelta(hours=2)).isoformat(),
                "end_time": (SOON + timedelta(hours=3)).isoformat(),
            },
        )
        client.post(f"/schedule/events/{ev['id']}/cancel")
    row = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    db_session.refresh(row)
    assert row.sequence == 2


def test_the_token_never_reaches_the_request_log(client, db_session, caplog):
    u = _user(db_session)
    url = _path(_issue(client, u)["url"])
    token = url.rsplit("/", 1)[1].removesuffix(".ics")
    with caplog.at_level(logging.INFO):
        client.get(url)
    # `httpx` is the test client logging its own outgoing request, not the server.
    server_side = [r.getMessage() for r in caplog.records if r.name != "httpx"]
    assert any("/schedule/feed/<redacted>" in m for m in server_side)
    assert not any(token in m for m in server_side)


@pytest.mark.parametrize(
    "path",
    ["/schedule/feed/SECRET.ics", "/api/v1/schedule/feed/SECRET.ics", "GET /api/schedule/feed/SECRET.ics HTTP/1.1"],
)
def test_redaction(path):
    assert "SECRET" not in redact_path(path)
    assert "<redacted>" in redact_path(path)


def test_uvicorn_access_log_is_redacted():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/api/v1/schedule/feed/SECRET.ics", "1.1", 200),
        None,
    )
    RedactSecretPaths().filter(record)
    assert "SECRET" not in record.getMessage()
    assert json.dumps(record.args)  # still loggable


def test_a_forwarded_host_never_chooses_the_feed_origin(client, db_session, monkeypatch):
    """Security sweep L3: X-Forwarded-Host passes through nginx from the client."""
    monkeypatch.delenv("SCHEDULER_FEED_BASE_URL", raising=False)
    u = _user(db_session)
    with signed_in(u):
        url = client.post("/schedule/feed-token", headers={"X-Forwarded-Host": "evil.example"}).json()["url"]
    assert "evil.example" not in url
    assert url.startswith("http://testserver/api/v1/schedule/feed/")

    monkeypatch.setenv("SCHEDULER_FEED_BASE_URL", "https://range.example/")
    with signed_in(u):
        url = client.post("/schedule/feed-token", headers={"X-Forwarded-Host": "evil.example"}).json()["url"]
    assert url.startswith("https://range.example/api/v1/schedule/feed/")


def test_startup_warns_when_production_has_no_feed_origin(monkeypatch, caplog):
    from app.scheduler.feed import warn_if_unconfigured

    monkeypatch.delenv("SCHEDULER_FEED_BASE_URL", raising=False)
    monkeypatch.setenv("AUTH_DISABLED", "false")
    with caplog.at_level(logging.WARNING, logger="truenorth.scheduler"):
        assert warn_if_unconfigured() is True
    assert "SCHEDULER_FEED_BASE_URL" in caplog.text
    monkeypatch.setenv("SCHEDULER_FEED_BASE_URL", "https://range.example")
    assert warn_if_unconfigured() is False
    monkeypatch.delenv("SCHEDULER_FEED_BASE_URL")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    assert warn_if_unconfigured() is False
