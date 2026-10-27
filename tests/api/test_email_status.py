"""GET /email/status and POST /email/test (#306; install/README.md "Email (SMTP)").

- Every signed-in role learns whether email is configured, and nothing else.
- The platform administrator also sees the relay (host, port, mode, sender, account name).
  Another tenant's admin does not.
- The relay password appears in no response, for anyone.
- The test message goes only to the caller's own address, only from the platform
  administrator, only while email is configured, at most once a minute, and is audited.
- The channel speaks each security mode (starttls, implicit tls, none), and an env from
  before SMTP_SECURITY (SMTP_USE_TLS) still works.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app.email_status import router as email_router
from app.models import AuditLog, UserRole
from app.notifications.smtp import SMTPChannel, smtp_settings

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
# Made up for the test; never a real credential.
PASSWORD = "fake-relay-pw-4-tests"
RELAY = {
    "SMTP_HOST": "smtp.corp.example",
    "SMTP_PORT": "587",
    "SMTP_SECURITY": "starttls",
    "SMTP_USER": "truenorth-relay",
    "SMTP_PASSWORD": PASSWORD,
    "SMTP_FROM": "truenorth@corp.example",
}
ALL_ROLES = [UserRole.student, UserRole.instructor, UserRole.observer, UserRole.range_ops, UserRole.admin]


@pytest.fixture
def env(monkeypatch):
    """No SMTP_* unless a test sets them; DEV_TENANT is the platform operator's."""
    for key in (*RELAY, "SMTP_USE_TLS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLATFORM_TENANT_ID", DEV_TENANT)

    def relay(**over):
        for k, v in {**RELAY, **over}.items():
            monkeypatch.setenv(k, v)

    return relay


@pytest.fixture
def outbox(monkeypatch):
    sent: list[tuple[object, dict]] = []

    async def fake_send(msg, **kw):
        sent.append((msg, kw))

    monkeypatch.setattr("aiosmtplib.send", fake_send)
    return sent


def _as(db, role: UserRole, tenant=DEV_TENANT):
    who = real_user(db, role, tenant)
    act_as(who)
    return who


# ── Status ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ALL_ROLES)
def test_unconfigured_is_reported_to_every_role(client, db_session, env, role):
    _as(db_session, role)
    r = client.get("/email/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is False
    assert body["relay"] is None and body["can_send_test"] is False
    assert "Email (SMTP)" in body["docs"]


@pytest.mark.parametrize("role", [UserRole.student, UserRole.instructor, UserRole.observer, UserRole.range_ops])
def test_non_admins_learn_only_that_email_is_configured(client, db_session, env, role):
    env()
    _as(db_session, role)
    r = client.get("/email/status")
    assert r.status_code == 200
    assert r.json()["configured"] is True and r.json()["relay"] is None
    for secret in ("smtp.corp.example", "truenorth-relay", PASSWORD, "truenorth@corp.example"):
        assert secret not in r.text


def test_another_tenants_admin_sees_no_relay(client, db_session, env):
    env()
    other = real_tenant(db_session)
    _as(db_session, UserRole.admin, tenant=other.id)
    r = client.get("/email/status")
    assert r.json()["configured"] is True and r.json()["relay"] is None
    assert "smtp.corp.example" not in r.text and PASSWORD not in r.text


def test_the_platform_admin_sees_the_relay_but_never_the_password(client, db_session, env):
    env()
    _as(db_session, UserRole.admin)
    r = client.get("/email/status")
    body = r.json()
    assert body["configured"] is True and body["can_send_test"] is True
    assert body["relay"] == {
        "host": "smtp.corp.example", "port": 587, "security": "starttls",
        "from_address": "truenorth@corp.example", "username": "truenorth-relay", "signs_in": True,
    }
    assert PASSWORD not in r.text


@pytest.mark.parametrize("role", ALL_ROLES)
def test_the_password_is_in_no_response(client, db_session, env, role):
    env()
    _as(db_session, role)
    assert PASSWORD not in client.get("/email/status").text
    assert PASSWORD not in client.post("/email/test").text


def test_a_host_without_a_sender_is_not_configured(client, db_session, env):
    env(SMTP_FROM="", SMTP_USER="")
    _as(db_session, UserRole.admin)
    body = client.get("/email/status").json()
    assert body["configured"] is False and body["can_send_test"] is False
    # The administrator still sees what is half set, to fix it.
    assert body["relay"]["host"] == "smtp.corp.example" and body["relay"]["signs_in"] is False


def test_status_needs_a_signed_in_user(client, db_session, env, monkeypatch):
    from app import auth
    from app.auth import get_current_user
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    assert client.get("/email/status").status_code == 401


# ── Test message ─────────────────────────────────────────────────────
def _tests_audited(db, user_id) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.action == "email.test", AuditLog.user_id == uuid.UUID(user_id)).all()


@pytest.mark.parametrize("role", [UserRole.student, UserRole.instructor, UserRole.observer, UserRole.range_ops])
def test_only_the_platform_admin_may_send_a_test(client, db_session, env, outbox, role):
    env()
    _as(db_session, role)
    assert client.post("/email/test").status_code == 403
    assert outbox == []


def test_another_tenants_admin_may_not_send_a_test(client, db_session, env, outbox):
    env()
    _as(db_session, UserRole.admin, tenant=real_tenant(db_session).id)
    assert client.post("/email/test").status_code == 403
    assert outbox == []


def test_no_test_while_email_is_not_configured(client, db_session, env, outbox):
    _as(db_session, UserRole.admin)
    r = client.post("/email/test")
    assert r.status_code == 409 and "not configured" in r.json()["detail"]
    assert outbox == []


def test_the_test_goes_to_the_admins_own_address_and_is_audited(client, db_session, env, outbox):
    env()
    me = _as(db_session, UserRole.admin)
    # A recipient in the body is ignored: the endpoint takes none.
    r = client.post("/email/test", json={"to": "someone-else@corp.example"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent"] is True and body["to"] == me.email
    [(msg, kw)] = outbox
    assert msg["To"] == me.email and msg["From"] == "truenorth@corp.example"
    assert kw["hostname"] == "smtp.corp.example" and kw["port"] == 587
    assert kw["username"] == "truenorth-relay" and kw["start_tls"] is True and kw["use_tls"] is False
    [row] = _tests_audited(db_session, me.id)
    assert me.email in row.detail and "accepted" in row.detail and PASSWORD not in row.detail
    assert PASSWORD not in r.text


def test_the_test_is_rate_limited_per_admin(client, db_session, env, outbox, monkeypatch):
    env()
    me = _as(db_session, UserRole.admin)
    t0 = email_router.now()
    monkeypatch.setattr(email_router, "now", lambda: t0)
    assert client.post("/email/test").status_code == 200
    monkeypatch.setattr(email_router, "now", lambda: t0 + timedelta(seconds=30))
    r = client.post("/email/test")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert len(outbox) == 1
    monkeypatch.setattr(email_router, "now", lambda: t0 + timedelta(seconds=61))
    assert client.post("/email/test").status_code == 200
    assert len(outbox) == 2
    assert len(_tests_audited(db_session, me.id)) == 2


def test_a_refused_test_is_reported_and_audited(client, db_session, env, monkeypatch):
    env()
    me = _as(db_session, UserRole.admin)

    calls = []

    async def refuse(msg, **kw):
        calls.append(kw)
        raise ConnectionRefusedError("relay said no")

    monkeypatch.setattr("aiosmtplib.send", refuse)
    r = client.post("/email/test")
    assert r.status_code == 200 and r.json()["sent"] is False
    # One short attempt, not the background channel's three of 60 s each.
    assert len(calls) == 1 and calls[0]["timeout"] <= 15
    [row] = _tests_audited(db_session, me.id)
    assert "FAILED" in row.detail


# ── The channel's security modes ─────────────────────────────────────
@pytest.mark.parametrize(
    ("security", "use_tls", "start_tls"),
    [("starttls", False, True), ("tls", True, False), ("none", False, False)],
)
@pytest.mark.asyncio
async def test_the_channel_speaks_each_security_mode(env, outbox, security, use_tls, start_tls):
    env(SMTP_SECURITY=security)
    assert await SMTPChannel().send("a@corp.example", "s", "b")
    [(_, kw)] = outbox
    assert (kw["use_tls"], kw["start_tls"]) == (use_tls, start_tls)


def test_an_env_from_before_smtp_security_still_works(env, monkeypatch):
    env()
    monkeypatch.delenv("SMTP_SECURITY")
    assert smtp_settings().security == "starttls"
    monkeypatch.setenv("SMTP_USE_TLS", "false")
    assert smtp_settings().security == "none"
    # SMTP_FROM unset: the account name is the sender, as before.
    monkeypatch.delenv("SMTP_FROM")
    monkeypatch.setenv("SMTP_USER", "relay@corp.example")
    assert smtp_settings().from_addr == "relay@corp.example" and smtp_settings().configured


def test_unconfigured_channel_sends_nothing(env, outbox):
    async def go():
        return await SMTPChannel().send("a@corp.example", "s", "b")

    import asyncio

    assert asyncio.run(go()) is False
    assert outbox == []
