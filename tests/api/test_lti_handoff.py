"""LTI gaps #1 and #3-#5 (docs/moodle-integration.md): what a launch does after it is verified.

#1 A Student the launch created (no TrueNorth sign-in) gets a single-use, two-minute code,
   bound to the launching browser, that the SPA exchanges for a short TrueNorth session.
   Threat cases: a replayed code, a code posted from another browser, an expired code, a
   session token whose account became staff, was deactivated or whose platform went away,
   a token signed by anyone but TrueNorth. Staff and Students with a TrueNorth sign-in never
   get one (they sign in with Keycloak).
#3 A quiz launch lands on /quiz-player?quiz=<id>, the route that reads it.
#4 An exercise launch records who launched it and lands on the Student's exercise page;
   another tenant's exercise is refused.
#5 The tool URLs the UI shows come from the API and are all real routes.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from _shared import real_exercise, real_tenant, real_user
from app import auth, lti13
from app.lti_identity import session as lti_session
from app.lti_identity.models import ExerciseLearner, LTIHandoff
from app.models import ExternalPlatform, IntegrationAuthType, User, UserRole
from app.routers.integrations import lti_state_cookie_name
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://moodle.example.test"
QUIZ = str(uuid.uuid4())


@pytest.fixture
def platform(db_session):
    t = real_tenant(db_session, f"lti-h-{uuid.uuid4().hex[:6]}")
    p = ExternalPlatform(id=uuid.uuid4(), name="Moodle", slug=f"moodle-{uuid.uuid4().hex[:6]}", platform_type="moodle",
                         base_url=ISSUER, auth_type=IntegrationAuthType.lti13, tenant_id=t.id, lti_issuer=ISSUER,
                         lti_client_id="client-1", lti_deployment_id="dep-1",
                         lti_auth_login_url=f"{ISSUER}/mod/lti/auth.php", lti_jwks_url=f"{ISSUER}/mod/lti/certs.php")
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def launch(monkeypatch, client, platform):
    """POST /lti/launch with a verified id_token carrying these claims (validation is
    lti13's job and is tested in test_lti13.py), from a browser holding the state cookie."""
    claims_box: dict = {}

    async def fake_validate(db, id_token, state):
        return platform, dict(claims_box)

    monkeypatch.setattr(lti13, "validate_launch", fake_validate)

    def go(resource: str = f"quiz:{QUIZ}", sub: str = "lms-7", email: str | None = None, **extra):
        claims_box.clear()
        claims_box.update({"sub": sub, lti13.CLAIM_CUSTOM: {"resource": resource}, **extra})
        if email:
            claims_box["email"] = email
        state = uuid.uuid4().hex
        client.cookies.set(lti_state_cookie_name(state), state)
        return client.post("/lti/launch", data={"id_token": "t", "state": state}, follow_redirects=False)

    return go


def _code(resp) -> str:
    location = resp.headers["location"]
    assert "/lti/session#code=" in location, location
    return location.split("#code=", 1)[1]


def _bind(resp) -> str:
    cookie = next(c for c in resp.headers.get_list("set-cookie") if c.startswith(f"{lti_session.HANDOFF_COOKIE}="))
    attrs = cookie.lower()
    assert "httponly" in attrs and "samesite=lax" in attrs and "secure" in attrs and "max-age=120" in attrs
    return cookie.split(";", 1)[0].split("=", 1)[1]


def _exchange(client, code: str, bind: str | None):
    client.cookies.delete(lti_session.HANDOFF_COOKIE)
    if bind is not None:
        client.cookies.set(lti_session.HANDOFF_COOKIE, bind)
    return client.post("/lti/session", json={"code": code})


class TestHandoff:
    def test_a_launched_student_gets_a_session_for_the_quiz_they_launched(self, client, launch, db_session):
        resp = launch()
        assert resp.status_code == 302
        out = _exchange(client, _code(resp), _bind(resp))
        assert out.status_code == 200, out.text
        body = out.json()
        assert body["target"] == f"/quiz-player?quiz={QUIZ}&lti=1"  # gap #3: the route that reads ?quiz=
        assert body["token_type"] == "Bearer" and 0 < body["expires_in"] <= 8 * 3600
        assert body["user"]["role"] == "student"
        claims = jwt.decode(body["access_token"], options={"verify_signature": False})
        assert claims["typ"] == "lti-session" and claims["sub"].startswith("lti:")
        # Only hashes are stored.
        row = db_session.query(LTIHandoff).one()
        assert _code(resp) not in (row.code_hash, row.bind_hash)

    def test_a_code_works_once(self, client, launch):
        resp = launch()
        code, bind = _code(resp), _bind(resp)
        assert _exchange(client, code, bind).status_code == 200
        replay = _exchange(client, code, bind)
        assert replay.status_code == 401 and "spent" in replay.json()["detail"]

    def test_a_code_from_another_browser_is_refused(self, client, launch):
        resp = launch()
        code = _code(resp)
        assert _exchange(client, code, None).status_code == 401  # no binding cookie at all
        assert _exchange(client, code, "attackers-own-cookie").status_code == 401
        assert _exchange(client, code, _bind(resp)).status_code == 200  # still usable by its browser

    def test_an_expired_code_is_refused(self, client, launch, db_session):
        resp = launch()
        db_session.query(LTIHandoff).update({"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        db_session.commit()
        assert _exchange(client, _code(resp), _bind(resp)).status_code == 401

    def test_an_unknown_code_is_refused(self, client):
        assert client.post("/lti/session", json={"code": "made-up"}).status_code == 401

    def test_without_the_state_cookie_rule_the_code_is_unbound(self, client, launch, monkeypatch):
        """LTI_REQUIRE_STATE_COOKIE=false (iframe deployments): the documented trade-off."""
        monkeypatch.setenv("LTI_REQUIRE_STATE_COOKIE", "false")
        resp = launch()
        assert not any(c.startswith(lti_session.HANDOFF_COOKIE) for c in resp.headers.get_list("set-cookie"))
        assert _exchange(client, _code(resp), None).status_code == 200

    def test_a_student_with_a_truenorth_sign_in_is_not_handed_a_session(self, client, launch, db_session, platform):
        """A Student linked by email signs in with Keycloak (so does linked staff, see
        test_lti_staff_link.py): the launch goes straight to the page."""
        person = real_user(db_session, UserRole.student, platform.tenant_id)
        db_session.commit()
        resp = launch(email=person.email)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith(f"/quiz-player?quiz={QUIZ}&lti=1")
        assert db_session.query(LTIHandoff).count() == 0


@pytest.fixture
def as_real_auth(monkeypatch):
    """Turn the AUTH_DISABLED short-circuit off, so bearer tokens are really checked."""
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)


def _session_token(client, launch) -> str:
    resp = launch()
    return _exchange(client, _code(resp), _bind(resp)).json()["access_token"]


class TestSessionToken:
    def test_the_session_signs_the_student_in(self, client, launch, as_real_auth, db_session):
        token = _session_token(client, launch)
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and me.json()["status"] == "registered"
        assert me.json()["user"]["role"] == "student"

    def test_a_student_promoted_to_staff_loses_the_lti_session(self, client, launch, as_real_auth, db_session):
        token = _session_token(client, launch)
        db_session.query(User).filter(User.keycloak_id.like("lti:%")).update({"role": UserRole.instructor},
                                                                             synchronize_session=False)
        db_session.commit()
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_a_deactivated_account_or_platform_ends_it(self, client, launch, as_real_auth, db_session, platform):
        token = _session_token(client, launch)
        platform.is_active = False
        db_session.commit()
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        platform.is_active = True
        db_session.query(User).filter(User.keycloak_id.like("lti:%")).update({"is_active": False},
                                                                             synchronize_session=False)
        db_session.commit()
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_a_token_not_signed_by_truenorth_is_refused(self, client, launch, as_real_auth, db_session):
        token = _session_token(client, launch)
        claims = jwt.decode(token, options={"verify_signature": False})
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        forged = jwt.encode(claims, other, algorithm="RS256", headers={"kid": lti13.get_tool_key(db_session).kid})
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401

    def test_it_cannot_name_an_account_with_a_keycloak_sign_in(self, db_session, platform):
        """Even TrueNorth's own key cannot mint an LTI session for a Keycloak account (staff
        or a Student with a real sign-in): verify() re-checks the account every time."""
        staff = real_user(db_session, UserRole.admin, platform.tenant_id)
        row = db_session.get(User, uuid.UUID(staff.id))
        token = lti_session.mint_session_token(db_session, row, platform, 600)
        with pytest.raises(lti_session.SessionTokenError):
            lti_session.verify(db_session, token)

    def test_another_kind_of_truenorth_token_is_not_a_session(self, db_session):
        key = lti13.get_tool_key(db_session)
        now = int(time.time())
        lab = jwt.encode({"iss": "truenorth", "typ": "lab", "aud": "truenorth-lab", "sub": "x", "exp": now + 60},
                         lti13.signing_pem(key), algorithm="RS256")
        assert lti_session.is_session_token(lab) is False


class TestLaunchTargets:
    def test_an_exercise_launch_records_its_learner_and_opens_their_page(self, client, launch, db_session, platform):
        ex = real_exercise(db_session, platform.tenant_id)
        db_session.commit()
        resp = launch(resource=f"exercise:{ex.id}", **{lti13.CLAIM_RESOURCE_LINK: {"id": "rl-9"}})
        body = _exchange(client, _code(resp), _bind(resp)).json()
        assert body["target"] == f"/exercises/{ex.id}?lti=1"
        [learner] = db_session.query(ExerciseLearner).filter_by(exercise_id=ex.id).all()
        assert str(learner.user_id) == body["user"]["id"] and learner.resource_link_id == "rl-9"
        launch(resource=f"exercise:{ex.id}")  # again: still one link per learner and run
        assert db_session.query(ExerciseLearner).filter_by(exercise_id=ex.id).count() == 1

    def test_another_tenants_exercise_is_refused(self, launch, db_session):
        theirs = real_exercise(db_session, real_tenant(db_session, f"other-{uuid.uuid4().hex[:6]}").id)
        db_session.commit()
        assert launch(resource=f"exercise:{theirs.id}").status_code == 404
        assert db_session.query(ExerciseLearner).count() == 0

    def test_a_course_launch_keeps_its_route(self, client, launch):
        course = uuid.uuid4()
        resp = launch(resource=f"course:{course}")
        assert _exchange(client, _code(resp), _bind(resp)).json()["target"] == f"/training?course={course}&lti=1"


def test_the_tool_config_names_only_real_routes(client):
    body = client.get("/integrations/lti/tool-config").json()
    paths = set(client.get("/openapi.json").json()["paths"])
    base = lti13.TOOL_BASE_URL.rstrip("/")
    for key in ("tool_url", "initiate_login_url", "public_keyset_url", "deep_linking_url", "public_key_pem_url"):
        assert body[key].startswith(base) and body[key][len(base):] in paths, (key, body[key])
    assert body["redirection_uris"] == [body["tool_url"]]
    assert "/v1/" not in str(body) and "deeplink" not in body["deep_linking_url"]


def test_a_student_cannot_read_the_tool_config(client):
    from _shared import acting_as

    with acting_as(UserRole.student):
        assert client.get("/integrations/lti/tool-config").status_code == 403
