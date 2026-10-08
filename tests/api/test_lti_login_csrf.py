"""LTI launches are bound to the browser that began them, and email links Students only.

Security sweep (low), 2026-10-08:

- login CSRF: an id_token + state issued for the attacker's own LMS account could be
  auto-posted to /lti/launch from the attacker's page, signing a victim's browser in to
  TrueNorth as the attacker. The state is now also an HttpOnly cookie on the browser that
  started /lti/login, and the launch must carry both;
- JIT matching by the platform-asserted email could sign in as an instructor or admin of
  the platform's tenant. The platform's subject identifies a returning user; email links
  an existing Student only.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import real_tenant, real_user
from app import lti13
from app.models import ExternalPlatform, IntegrationAuthType, User, UserRole
from app.routers import integrations
from app.routers.integrations import _jit_user, lti_state_cookie_name
from fastapi import HTTPException

ISSUER = "https://moodle.example.test"


@pytest.fixture
def platform(db_session):
    t = real_tenant(db_session, "lti-a")
    p = ExternalPlatform(id=uuid.uuid4(), name="Moodle", slug=f"moodle-{uuid.uuid4().hex[:6]}", platform_type="moodle",
                         base_url=ISSUER, auth_type=IntegrationAuthType.lti13, tenant_id=t.id, lti_issuer=ISSUER,
                         lti_client_id="client-1", lti_deployment_id="dep-1",
                         lti_auth_login_url=f"{ISSUER}/mod/lti/auth.php", lti_jwks_url=f"{ISSUER}/mod/lti/certs.php")
    db_session.add(p)
    db_session.flush()
    return p


def test_login_sets_the_state_as_a_cross_site_httponly_cookie(client, platform):
    r = client.post("/lti/login", data={"iss": ISSUER, "login_hint": "u1", "target_link_uri": "https://tn/lti/launch",
                                        "client_id": "client-1"}, follow_redirects=False)
    assert r.status_code == 302
    state = dict(p.split("=", 1) for p in r.headers["location"].split("?", 1)[1].split("&"))["state"]
    cookie = r.headers["set-cookie"]
    assert f"{lti_state_cookie_name(state)}={state}" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=none" in cookie.lower()


@pytest.fixture
def launch_ok(monkeypatch, platform, db_session):
    student = real_user(db_session, UserRole.student, platform.tenant_id)
    row = db_session.get(User, uuid.UUID(student.id))

    async def fake_validate(db, id_token, state):
        return platform, {"sub": "s-1", "email": row.email}

    monkeypatch.setattr(lti13, "validate_launch", fake_validate)
    monkeypatch.setattr(lti13, "record_launch", lambda *a, **k: None)
    return row


def test_a_launch_without_the_browsers_state_cookie_is_refused(client, launch_ok):
    r = client.post("/lti/launch", data={"id_token": "t", "state": "attacker-state"}, follow_redirects=False)
    assert r.status_code == 401 and "did not start the login" in r.json()["detail"]


def test_a_launch_with_someone_elses_state_is_refused(client, launch_ok):
    client.cookies.set(lti_state_cookie_name("victims-own-state"), "victims-own-state")
    client.cookies.set("tn_lti_state", "attacker-state")  # the old single-cookie name counts for nothing
    r = client.post("/lti/launch", data={"id_token": "t", "state": "attacker-state"}, follow_redirects=False)
    assert r.status_code == 401


def test_the_browser_that_began_the_login_launches(client, launch_ok):
    client.cookies.set(lti_state_cookie_name("s-123"), "s-123")
    r = client.post("/lti/launch", data={"id_token": "t", "state": "s-123"}, follow_redirects=False)
    assert r.status_code == 302, r.text
    # The spent launch's cookie is cleared.
    assert f'{lti_state_cookie_name("s-123")}=""' in r.headers["set-cookie"]


def test_two_launches_begun_in_two_tabs_both_complete(client, launch_ok):
    states = []
    for hint in ("tab-1", "tab-2"):
        r = client.post("/lti/login", data={"iss": ISSUER, "login_hint": hint,
                                            "target_link_uri": "https://tn/lti/launch", "client_id": "client-1"},
                        follow_redirects=False)
        states.append(dict(p.split("=", 1) for p in r.headers["location"].split("?", 1)[1].split("&"))["state"])
        # The browser keeps what was set. (The test client is plain http and would not
        # send a Secure cookie back by itself.)
        name, value = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)
        client.cookies.set(name, value)
    assert states[0] != states[1]

    for state in states:  # the first tab finishes after the second began
        r = client.post("/lti/launch", data={"id_token": "t", "state": state}, follow_redirects=False)
        assert r.status_code == 302, r.text


@pytest.mark.parametrize("state", ["é-state", "ünïcode", "☃"])
def test_a_non_ascii_state_is_a_401_not_a_500(client, launch_ok, state):
    r = client.post("/lti/launch", data={"id_token": "t", "state": state}, follow_redirects=False)
    assert r.status_code == 401, r.text


def test_an_empty_state_never_matches(client, launch_ok):
    r = client.post("/lti/launch", data={"id_token": "t", "state": ""}, follow_redirects=False)
    assert r.status_code in (401, 422)


def test_the_cookie_check_can_be_turned_off_for_iframe_deployments(client, launch_ok, monkeypatch):
    monkeypatch.setenv("LTI_REQUIRE_STATE_COOKIE", "false")
    r = client.post("/lti/launch", data={"id_token": "t", "state": "s-1"}, follow_redirects=False)
    assert r.status_code == 302


# ── JIT matching ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin, UserRole.range_ops])
def test_an_asserted_staff_email_does_not_sign_in_as_staff(db_session, platform, role):
    staff = db_session.get(User, uuid.UUID(real_user(db_session, role, platform.tenant_id).id))
    with pytest.raises(HTTPException) as exc:
        _jit_user(db_session, platform, {"sub": "lms-99", "email": staff.email})
    assert exc.value.status_code == 403


def test_a_students_email_still_links_their_account(db_session, platform):
    s = db_session.get(User, uuid.UUID(real_user(db_session, UserRole.student, platform.tenant_id).id))
    assert _jit_user(db_session, platform, {"sub": "lms-1", "email": s.email}).id == s.id


def test_the_platform_subject_identifies_a_returning_user(db_session, platform):
    first = _jit_user(db_session, platform, {"sub": "lms-7", "email": "learner@lms.example"})
    again = _jit_user(db_session, platform, {"sub": "lms-7", "email": "changed@lms.example"})
    assert again.id == first.id and first.keycloak_id == f"lti:{platform.id}:lms-7"


def test_integrations_module_exposes_the_cookie_name():
    assert integrations.LTI_STATE_COOKIE == "tn_lti_state"
