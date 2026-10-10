"""cmi5 AUs launched from an LMS over LTI 1.3, with the result in the LMS's gradebook (AGS).

app/cmi5/lti.py (launch, deep-linking items), app/cmi5/ags.py (grade pass-back) and their
seams in routers/integrations.py. The launch's id_token validation is lti13's and is tested
in test_lti13.py; here a verified launch carries the claims each test sets. The platform's
token and score endpoints are a respx fake; the LRS is MemoryLRS (tests/api/_cmi5_kit.py).

Threat cases: another tenant's release or platform, an unpublished course, a Student not
enrolled, a forged pass (a score TrueNorth did not mark), cmi5-allowed statements that
carry no result, a line item off the platform's origin, a launch without the score scope,
and a retry that must not post twice.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import respx
from _cmi5_kit import AU, BUNDLE, CATALOGUE, CROSSWALK, MemoryLRS
from _shared import real_tenant
from app import lti13, net_guard
from app.auth import CurrentUser, get_current_user
from app.cmi5 import ags, lms
from app.cmi5 import content as content_mod
from app.cmi5 import lti as cmi5_lti
from app.cmi5.models import AGS_FAILED, AGS_PENDING, AGS_SENT, Cmi5AgsScore, Cmi5Registration
from app.course_releases.models import SUPERSEDED
from app.db import get_db
from app.enrollment import ensure_enrollment
from app.lti_identity.models import LTIUserLink
from app.main import app as fastapi_app
from app.models import Course, Enrollment, ExternalPlatform, IntegrationAuthType, LTILaunch, User, UserRole
from app.moodle_backends import install_cli
from app.moodle_farm import service as farm
from app.routers.integrations import lti_state_cookie_name
from httpx import ConnectError, Response

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
ISSUER = "https://moodle.example.test"
TOKEN_URL = f"{ISSUER}/mod/lti/token.php"
LINEITEM = f"{ISSUER}/mod/lti/services.php/2/lineitems/7/lineitem?type_id=1"
SCORES = f"{ISSUER}/mod/lti/services.php/2/lineitems/7/lineitem/scores?type_id=1"
AGS_CLAIM = {lti13.CLAIM_AGS: {"lineitem": LINEITEM, "scope": [lti13.AGS_SCORE_SCOPE]}}


# -- fixtures -------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("CMI5_LRS_AUTH", "YXUta2V5OmF1LXNlY3JldA==")
    monkeypatch.setenv("LRS_AUTH", "c2VydmVyOnNlY3JldA==")


@pytest.fixture(autouse=True)
def lrs(monkeypatch):
    mem = MemoryLRS()
    monkeypatch.setattr(lms, "get_lms_backend", lambda: mem)
    return mem


class _Borrowed:
    """The test's session for background delivery; closing it is the test's business."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _delivery_session(monkeypatch, db_session):
    monkeypatch.setattr(ags, "open_session", lambda: _Borrowed(db_session))


PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _platform_dns(monkeypatch):
    """The fake platform's host resolves to a public address (AGS goes through net_guard),
    and no access token is cached from another test."""
    monkeypatch.setattr(
        net_guard, "_resolve", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]
    )
    lti13._TOKENS.clear()
    yield
    lti13._TOKENS.clear()


@pytest.fixture
def release(client):
    """C105, uploaded and accepted by the dev admin (AUTH_DISABLED), its course published."""
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    r = client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    assert r.status_code == 200, r.text
    r = client.post("/course-releases", files={"file": (BUNDLE.name, BUNDLE.read_bytes(), "application/gzip")})
    assert r.status_code in (200, 201), r.text
    rel = r.json()
    if rel["state"] == "candidate":
        acks = [a["id"] for a in rel["open_actions"]]
        r = client.post(f"/course-releases/{rel['id']}/accept", json={"acknowledge_actions": acks})
        assert r.status_code == 200, r.text
        rel = r.json()
    db = client.app.dependency_overrides[get_db]().__next__()
    db.get(Course, uuid.UUID(rel["course_id"])).is_published = True
    db.commit()
    return rel


def _platform(db, tenant_id, issuer=ISSUER) -> ExternalPlatform:
    p = ExternalPlatform(
        id=uuid.uuid4(), name="Moodle", slug=f"moodle-{uuid.uuid4().hex[:6]}", platform_type="moodle",
        base_url=issuer, auth_type=IntegrationAuthType.lti13, tenant_id=tenant_id, lti_issuer=issuer,
        lti_client_id="client-1", lti_deployment_id="dep-1", lti_auth_login_url=f"{issuer}/mod/lti/auth.php",
        lti_jwks_url=f"{issuer}/mod/lti/certs.php", lti_token_url=f"{issuer}/mod/lti/token.php",
    )
    db.add(p)
    db.flush()
    return p


@pytest.fixture
def platform(db_session):
    return _platform(db_session, DEV_TENANT)


@pytest.fixture
def lti(monkeypatch, client):
    """POST /lti/launch as a verified launch from ``platform`` with these claims."""
    box: dict = {}

    async def fake_validate(db, id_token, state):
        return box["platform"], dict(box["claims"])

    monkeypatch.setattr(lti13, "validate_launch", fake_validate)

    def go(platform, resource: str, sub: str = "lms-7", **claims):
        box["platform"] = platform
        box["claims"] = {"sub": sub, lti13.CLAIM_CUSTOM: {"resource": resource}, **claims}
        state = uuid.uuid4().hex
        client.cookies.set(lti_state_cookie_name(state), state)
        return client.post("/lti/launch", data={"id_token": "t", "state": state}, follow_redirects=False)

    return go


def _lti_student(db, platform, release, sub="lms-7", enrol=True) -> User:
    """The account an earlier launch of ``sub`` created, enrolled in the release's course."""
    u = User(id=uuid.uuid4(), keycloak_id=f"lti:{platform.id}:{sub}", email=f"{sub}-{uuid.uuid4().hex[:6]}@x.test",
             display_name="Student", role=UserRole.student, tenant_id=platform.tenant_id, source="lti")
    db.add(u)
    db.flush()
    if enrol:
        ensure_enrollment(db, user_id=u.id, course_id=uuid.UUID(release["course_id"]), tenant_id=platform.tenant_id)
    db.commit()
    return u


def _exchange(client, resp) -> dict:
    location = resp.headers["location"]
    assert "/lti/session#code=" in location, location
    code = location.split("#code=", 1)[1]
    bind = next(c for c in resp.headers.get_list("set-cookie") if c.startswith("tn_lti_handoff="))
    client.cookies.set("tn_lti_handoff", bind.split(";", 1)[0].split("=", 1)[1])
    out = client.post("/lti/session", json={"code": code})
    assert out.status_code == 200, out.text
    return out.json()


def _as(user: User):
    who = CurrentUser(id=str(user.id), email=user.email, display_name=user.display_name, role=user.role,
                      tenant_id=str(user.tenant_id), keycloak_id=user.keycloak_id)
    fastapi_app.dependency_overrides[get_current_user] = lambda: who


def _spa_launch(client, user, release, index=0) -> AU:
    """What the SPA does at /au/releases/<id>?launch=<n>: launch the AU for the Student."""
    _as(user)
    try:
        r = client.post(f"/cmi5/releases/{release['id']}/aus/{index}/launch", json={})
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200, r.text
    return AU(client, r.json()["url"], user).start()


# -- launch ---------------------------------------------------------------------------------
class TestLaunch:
    def test_an_enrolled_student_lands_on_the_au_and_gets_their_registration(
        self, client, lti, platform, release, db_session
    ):
        student = _lti_student(db_session, platform, release)
        resp = lti(platform, f"cmi5:{release['id']}:2", **AGS_CLAIM)
        assert resp.status_code == 302, resp.text
        body = _exchange(client, resp)  # the #132 hand-off: this Student has no TrueNorth sign-in
        assert body["target"] == f"/au/releases/{release['id']}?launch=2&lti=1"
        assert body["user"]["id"] == str(student.id)

        enrolment = db_session.query(Enrollment).filter_by(user_id=student.id).one()
        reg = db_session.get(Cmi5Registration, enrolment.id)
        assert reg is not None and str(reg.release_id) == release["id"]  # the enrolment IS the registration
        [launch] = db_session.query(LTILaunch).filter_by(user_id=student.id).all()
        assert (launch.resource_kind, launch.resource_id) == ("cmi5", f"{release['id']}:2")
        assert launch.ags_lineitem_url == LINEITEM

        assert lti(platform, f"cmi5:{release['id']}:2").status_code == 302  # again: the same registration
        assert db_session.query(Cmi5Registration).filter_by(user_id=student.id).count() == 1

    def test_a_student_with_a_truenorth_sign_in_goes_straight_to_the_page(
        self, client, lti, platform, release, db_session
    ):
        u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email="ada@example.test", display_name="Ada",
                 role=UserRole.student, tenant_id=DEV_TENANT)
        db_session.add(u)
        db_session.flush()
        ensure_enrollment(db_session, user_id=u.id, course_id=uuid.UUID(release["course_id"]), tenant_id=DEV_TENANT)
        db_session.commit()
        resp = lti(platform, f"cmi5:{release['id']}:0", sub="lms-ada", email="ada@example.test")
        assert resp.status_code == 302
        assert resp.headers["location"].endswith(f"/au/releases/{release['id']}?launch=0&lti=1")
        assert "#code=" not in resp.headers["location"]  # they sign in with Keycloak as usual

    def test_a_student_not_enrolled_is_refused_and_nothing_is_recorded(self, lti, platform, release, db_session):
        student = _lti_student(db_session, platform, release, enrol=False)
        resp = lti(platform, f"cmi5:{release['id']}:0", **AGS_CLAIM)
        assert resp.status_code == 403
        assert "not enrolled" in resp.json()["detail"]
        assert db_session.query(LTILaunch).filter_by(user_id=student.id).count() == 0
        assert db_session.query(Enrollment).filter_by(user_id=student.id).count() == 0  # the launch never enrols
        assert db_session.query(Cmi5Registration).count() == 0

    def test_a_new_lms_account_is_not_enrolled_by_the_launch(self, lti, platform, release, db_session):
        resp = lti(platform, f"cmi5:{release['id']}:0", sub="brand-new")
        assert resp.status_code == 403
        assert db_session.query(Enrollment).count() == 0

    def test_another_tenants_release_is_not_found(self, lti, release, db_session):
        theirs = _platform(db_session, real_tenant(db_session).id, issuer="https://other.example.test")
        db_session.commit()
        resp = lti(theirs, f"cmi5:{release['id']}:0", **AGS_CLAIM)
        assert resp.status_code == 404
        assert db_session.query(LTILaunch).count() == 0

    def test_an_unpublished_course_is_not_found(self, lti, platform, release, db_session):
        _lti_student(db_session, platform, release)
        db_session.get(Course, uuid.UUID(release["course_id"])).is_published = False
        db_session.commit()
        assert lti(platform, f"cmi5:{release['id']}:0").status_code == 404

    @pytest.mark.parametrize("rid", ["{rel}:99", "{rel}:-1", "{rel}:x", "not-a-uuid:0", "{rel}:01", "{rand}:0"])
    def test_a_link_that_names_no_au_is_refused(self, lti, platform, release, db_session, rid):
        _lti_student(db_session, platform, release)
        resp = lti(platform, "cmi5:" + rid.format(rel=release["id"], rand=uuid.uuid4()))
        assert resp.status_code in (400, 404)
        assert db_session.query(LTILaunch).count() == 0

    def test_without_cmi5_configured_the_launch_fails_closed(self, monkeypatch, lti, platform, release, db_session):
        _lti_student(db_session, platform, release)
        monkeypatch.delenv("CMI5_LRS_AUTH")
        assert lti(platform, f"cmi5:{release['id']}:0").status_code == 503


# -- deep linking ---------------------------------------------------------------------------
def _picker(lti, platform) -> str:
    claims = {
        lti13.CLAIM_MESSAGE_TYPE: "LtiDeepLinkingRequest",
        lti13.CLAIM_DEPLOYMENT: "dep-1",
        lti13.CLAIM_DL_SETTINGS: {"deep_link_return_url": f"{ISSUER}/mod/lti/contentitem_return.php", "data": "d"},
    }
    resp = lti(platform, "", sub="teacher-1", **claims)
    assert resp.status_code == 200, resp.text
    return resp.text


class TestDeepLinking:
    def test_the_picker_offers_one_item_per_au(self, lti, platform, release):
        page = _picker(lti, platform)
        values = re.findall(r'value="(cmi5:[^"]+)"', page)
        assert values == [f"cmi5:{release['id']}:{i}" for i in range(6)]

    def test_another_tenants_platform_sees_none_of_them(self, lti, release, db_session):
        theirs = _platform(db_session, real_tenant(db_session).id, issuer="https://other.example.test")
        db_session.commit()
        assert "cmi5:" not in _picker(lti, theirs)

    def test_the_content_items_are_checked_and_titled_by_truenorth(self, client, lti, platform, release, db_session):
        page = _picker(lti, platform)
        session = html.unescape(re.search(r'name="session" value="([^"]+)"', page).group(1))
        other_tenant_release = uuid.uuid4()
        form = {
            "session": session,
            "item": [
                f"cmi5:{release['id']}:1:Anything the browser says",
                f"cmi5:{release['id']}:5",
                f"cmi5:{release['id']}:6",  # no such AU
                f"cmi5:{other_tenant_release}:0",  # not this tenant's
            ],
        }
        resp = client.post("/lti/deeplink/finish", data=form)
        assert resp.status_code == 200, resp.text
        token = re.search(r'name="JWT" value="([^"]+)"', resp.text).group(1)
        items = jwt.decode(token, options={"verify_signature": False})[lti13.CLAIM_DL_CONTENT_ITEMS]
        assert [i["custom"]["resource"] for i in items] == [f"cmi5:{release['id']}:1", f"cmi5:{release['id']}:5"]
        assert all(i["type"] == "ltiResourceLink" and i["lineItem"]["scoreMaximum"] == 100 for i in items)
        assert "Anything the browser says" not in items[0]["title"]
        assert items[0]["title"].startswith(release["title"])


# -- grade pass-back ----------------------------------------------------------------------------
@pytest.fixture
def moodle():
    """The platform's token and score endpoints; ``scores`` lists what it was sent."""
    with respx.mock(assert_all_called=False) as mock:
        token = mock.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", "expires_in": 3600}))
        route = mock.post(SCORES).mock(return_value=Response(200, json={}))
        route.token_route = token
        route.mock_router = mock
        yield route


def _sent(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


@pytest.fixture
def launched(client, lti, platform, release, db_session):
    """A Student launched AU 0 from Moodle with a line item, and the SPA started it."""
    student = _lti_student(db_session, platform, release)
    assert lti(platform, f"cmi5:{release['id']}:0", **AGS_CLAIM).status_code == 302
    au = _spa_launch(client, student, release)
    assert au.initialized().status_code == 200
    return au


class TestGradePassBack:
    def test_a_pass_sends_truenorths_mark_to_the_line_item(self, launched, moodle, db_session):
        assert launched.scored(0.8).status_code == 200
        [score] = _sent(moodle)
        assert score["scoreGiven"] == 80.0 and score["scoreMaximum"] == 100
        assert (score["activityProgress"], score["gradingProgress"], score["userId"]) == (
            "Completed", "FullyGraded", "lms-7"
        )
        assert moodle.calls[0].request.headers["authorization"] == "Bearer tok"
        assert moodle.calls[0].request.headers["content-type"] == "application/vnd.ims.lis.v1.score+json"
        [row] = db_session.query(Cmi5AgsScore).all()
        assert row.state == AGS_SENT and row.attempts == 0

    def test_a_fail_sends_the_failing_mark(self, launched, moodle):
        assert launched.scored(0.4).status_code == 200
        assert [s["scoreGiven"] for s in _sent(moodle)] == [40.0]

    def test_nothing_is_sent_twice(self, launched, moodle, db_session):
        launched.scored(0.8)
        assert launched.terminated().status_code == 200
        import asyncio

        assert asyncio.run(ags.deliver_due(db_session)) == 0
        assert len(_sent(moodle)) == 1
        assert db_session.query(Cmi5AgsScore).count() == 1

    def test_completed_alone_reports_progress_without_a_score_then_the_pass_adds_it(self, launched, moodle):
        assert launched.completed().status_code == 200
        assert launched.scored(0.8).status_code == 200
        first, second = _sent(moodle)
        assert "scoreGiven" not in first and first["gradingProgress"] == "Pending"
        assert first["activityProgress"] == "Completed"
        assert second["scoreGiven"] == 80.0 and second["gradingProgress"] == "FullyGraded"

    def test_a_forged_pass_sends_nothing(self, launched, moodle, db_session):
        launched.mark(0.4)  # TrueNorth marked 40 %
        forged = launched.statement(
            "passed", moveon=True, result={"success": True, "score": {"scaled": 1.0}, "duration": "PT9S"},
            ext={"https://w3id.org/xapi/cmi5/context/extensions/masteryscore": 0.7},
        )
        r = launched.send(forged)
        assert r.status_code == 403 and r.json()["violatedReqId"] == "TN-GRADE"
        assert _sent(moodle) == [] and db_session.query(Cmi5AgsScore).count() == 0

    def test_statements_without_the_cmi5_category_send_nothing(self, launched, moodle, db_session):
        assert launched.send(launched.statement("experienced", defined=False)).status_code == 200
        r = launched.send(launched.statement("experienced", defined=False, result={"score": {"scaled": 1.0}}))
        assert r.status_code == 403 and r.json()["violatedReqId"] == "TN-RESULT"
        r = launched.send(launched.statement("passed", defined=False, result={"success": True}))
        assert r.status_code == 403
        assert _sent(moodle) == [] and db_session.query(Cmi5AgsScore).count() == 0

    def test_a_transient_failure_is_retried_until_it_lands(self, launched, moodle, db_session):
        import asyncio

        moodle.side_effect = [Response(503), ConnectError("down"), Response(200, json={})]
        launched.scored(0.8)
        [row] = db_session.query(Cmi5AgsScore).all()
        assert (row.state, row.attempts) == (AGS_PENDING, 1) and "503" in row.last_error
        assert asyncio.run(ags.deliver_due(db_session)) == 0  # backing off: not due yet
        for _ in range(2):
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            db_session.commit()
            asyncio.run(ags.deliver_due(db_session))
        db_session.refresh(row)
        assert (row.state, row.attempts, row.last_error) == (AGS_SENT, 2, "")
        assert [s["scoreGiven"] for s in _sent(moodle)] == [80.0] * 3
        assert len({s["timestamp"] for s in _sent(moodle)}) == 1  # the same result, resent

    def test_a_refusal_is_not_retried(self, launched, moodle, db_session):
        moodle.side_effect = [Response(400, json={"error": "bad"})]
        launched.scored(0.8)
        [row] = db_session.query(Cmi5AgsScore).all()
        assert row.state == AGS_FAILED and "400" in row.last_error

    def test_a_platform_that_is_gone_gets_nothing(self, launched, moodle, platform, db_session):
        platform.is_active = False
        db_session.commit()
        launched.scored(0.8)
        assert _sent(moodle) == [] and db_session.query(Cmi5AgsScore).count() == 0


class TestWhichLaunchesGetTheResult:
    def _start(self, client, lti, platform, release, db_session, **claims):
        student = _lti_student(db_session, platform, release)
        assert lti(platform, f"cmi5:{release['id']}:0", **claims).status_code == 302
        au = _spa_launch(client, student, release)
        au.initialized()
        return au, student

    def test_a_launch_without_the_score_scope(self, client, lti, platform, release, db_session, moodle):
        claims = {lti13.CLAIM_AGS: {"lineitem": LINEITEM, "scope": ["https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly"]}}
        au, _ = self._start(client, lti, platform, release, db_session, **claims)
        au.scored(0.8)
        assert _sent(moodle) == []

    def test_a_line_item_off_the_platforms_origin(self, client, lti, platform, release, db_session, moodle):
        claims = {lti13.CLAIM_AGS: {"lineitem": "https://evil.example.test/lineitem", "scope": [lti13.AGS_SCORE_SCOPE]}}
        au, _ = self._start(client, lti, platform, release, db_session, **claims)
        au.scored(0.8)
        assert _sent(moodle) == [] and db_session.query(Cmi5AgsScore).count() == 0

    def test_a_launch_from_another_tenants_platform(self, client, lti, platform, release, db_session, moodle):
        au, student = self._start(client, lti, platform, release, db_session)  # no line item from ours
        theirs = _platform(db_session, real_tenant(db_session).id, issuer="https://other.example.test")
        db_session.add(LTILaunch(platform_id=theirs.id, user_id=student.id, lti_user_sub="x", resource_kind="cmi5",
                                 resource_id=f"{release['id']}:0", ags_lineitem_url="https://other.example.test/li",
                                 ags_scopes=json.dumps([lti13.AGS_SCORE_SCOPE])))
        db_session.commit()
        au.scored(0.8)
        assert db_session.query(Cmi5AgsScore).count() == 0

    def test_deregistering_the_platform_takes_its_results(self, client, launched, moodle, platform, db_session):
        launched.scored(0.8)
        assert db_session.query(Cmi5AgsScore).count() == 1
        r = client.delete(f"/integrations/platforms/{platform.id}")
        assert r.status_code == 204, r.text
        assert db_session.query(Cmi5AgsScore).count() == 0


def test_the_au_url_the_spa_opens_carries_the_registration_of_the_lti_launch(client, launched, db_session):
    q = {k: v[0] for k, v in parse_qs(urlsplit(launched.url).query).items()}
    reg = db_session.get(Cmi5Registration, uuid.UUID(q["registration"]))
    assert reg is not None and db_session.get(Enrollment, reg.enrollment_id) is not None


# -- review of #140 -----------------------------------------------------------------------------
ATTACKER_LINEITEM = f"{ISSUER}/mod/lti/services.php/2/lineitems/8/lineitem?type_id=1"
ATTACKER_SCORES = f"{ISSUER}/mod/lti/services.php/2/lineitems/8/lineitem/scores?type_id=1"


class TestOnlyBoundAccountsGetGrades:
    """An LMS account that asserts a Student's email is signed in as that Student (the
    existing rule), but is not that Student's gradebook: no line item is kept for it, and no
    grade is ever sent to it, by the cmi5 outbox or by push_score_for_resource."""

    def test_two_lms_accounts_one_email_only_the_bound_one_gets_the_grade(
        self, client, lti, platform, release, db_session, moodle
    ):
        attacker_scores = moodle.mock_router.post(ATTACKER_SCORES).mock(return_value=Response(200, json={}))
        victim = _lti_student(db_session, platform, release, sub="victim-sub")
        assert lti(platform, f"cmi5:{release['id']}:0", sub="victim-sub", **AGS_CLAIM).status_code == 302
        attacker_claim = {lti13.CLAIM_AGS: {"lineitem": ATTACKER_LINEITEM, "scope": [lti13.AGS_SCORE_SCOPE]}}
        resp = lti(platform, f"cmi5:{release['id']}:0", sub="attacker", email=victim.email, **attacker_claim)
        assert resp.status_code == 302  # signed in as the Student by email, as before
        attacker_launch = db_session.query(LTILaunch).filter_by(lti_user_sub="attacker").one()
        assert attacker_launch.user_id == victim.id and attacker_launch.ags_lineitem_url == ""
        # A row recorded before this fix still names the attacker's line item: not used either.
        db_session.add(LTILaunch(platform_id=platform.id, user_id=victim.id, lti_user_sub="attacker",
                                 resource_kind="cmi5", resource_id=f"{release['id']}:0",
                                 ags_lineitem_url=ATTACKER_LINEITEM, ags_scopes=json.dumps([lti13.AGS_SCORE_SCOPE])))
        db_session.commit()

        au = _spa_launch(client, victim, release)
        au.initialized()
        assert au.scored(0.8).status_code == 200
        assert [(s["userId"], s["scoreGiven"]) for s in _sent(moodle)] == [("victim-sub", 80.0)]
        assert not attacker_scores.called
        assert db_session.query(Cmi5AgsScore).count() == 1

    def test_push_score_for_resource_skips_a_launch_matched_by_email(self, platform, release, db_session, moodle):
        attacker_scores = moodle.mock_router.post(ATTACKER_SCORES).mock(return_value=Response(200, json={}))
        victim = _lti_student(db_session, platform, release, sub="victim-sub")
        quiz = str(uuid.uuid4())
        now = datetime.now(UTC)
        db_session.add_all([
            LTILaunch(platform_id=platform.id, user_id=victim.id, lti_user_sub="victim-sub", resource_kind="quiz",
                      resource_id=quiz, ags_lineitem_url=LINEITEM, ags_scopes="[]", created_at=now - timedelta(hours=1)),
            LTILaunch(platform_id=platform.id, user_id=victim.id, lti_user_sub="attacker", resource_kind="quiz",
                      resource_id=quiz, ags_lineitem_url=ATTACKER_LINEITEM, ags_scopes="[]", created_at=now),
        ])
        db_session.commit()
        assert asyncio.run(lti13.push_score_for_resource(db_session, victim.id, "quiz", quiz, 8, 10)) is True
        assert not attacker_scores.called and [s["userId"] for s in _sent(moodle)] == ["victim-sub"]
        # Only the email-matched launch left: nothing is sent at all.
        db_session.query(LTILaunch).filter_by(lti_user_sub="victim-sub").delete()
        db_session.commit()
        assert asyncio.run(lti13.push_score_for_resource(db_session, victim.id, "quiz", quiz, 8, 10)) is False
        assert not attacker_scores.called

    def test_an_explicitly_linked_lms_account_is_bound(self, client, lti, platform, release, db_session, moodle):
        student = _lti_student(db_session, platform, release, sub="own-sub")
        db_session.add(LTIUserLink(platform_id=platform.id, lti_sub="linked-sub", user_id=student.id, lms_name="S"))
        db_session.commit()
        assert lti(platform, f"cmi5:{release['id']}:0", sub="linked-sub", **AGS_CLAIM).status_code == 302
        au = _spa_launch(client, student, release)
        au.initialized()
        au.scored(0.8)
        assert [s["userId"] for s in _sent(moodle)] == ["linked-sub"]


async def _no_background_delivery(registration_id):
    return None


class TestDeliveryRobustness:
    def test_an_unforeseen_error_fails_the_row_and_the_batch_goes_on(self, monkeypatch, launched, moodle, db_session):
        monkeypatch.setattr(ags, "deliver_registration", _no_background_delivery)
        launched.scored(0.8)
        [row] = db_session.query(Cmi5AgsScore).all()
        copy = {c.name: getattr(row, c.name) for c in Cmi5AgsScore.__table__.columns if c.name not in ("id", "cell_key")}
        db_session.add(Cmi5AgsScore(**copy, cell_key="0" * 64))
        db_session.commit()
        real_send = lti13.send_score
        calls = []

        async def flaky(db, platform, lineitem, sub, score):
            calls.append(lineitem)
            if len(calls) == 1:
                raise httpx.InvalidURL("bad")  # not one of the errors send_score classifies
            return await real_send(db, platform, lineitem, sub, score)

        monkeypatch.setattr(lti13, "send_score", flaky)
        assert asyncio.run(ags.deliver_due(db_session)) == 1
        states = sorted((r.state, r.last_error) for r in db_session.query(Cmi5AgsScore).all())
        assert states == [(AGS_FAILED, "unexpected failure: InvalidURL"), (AGS_SENT, "")]

    def test_a_row_deleted_while_it_is_sent_is_left_alone(self, monkeypatch, launched, moodle, db_session):
        monkeypatch.setattr(ags, "deliver_registration", _no_background_delivery)
        launched.scored(0.8)
        [row] = db_session.query(Cmi5AgsScore).all()
        row_id = row.id

        async def deregistered_meanwhile(db, platform, lineitem, sub, score):
            db.query(Cmi5AgsScore).filter(Cmi5AgsScore.id == row_id).delete(synchronize_session=False)

        monkeypatch.setattr(lti13, "send_score", deregistered_meanwhile)
        assert asyncio.run(ags.deliver(db_session, row_id)) is None

    def test_a_platform_on_a_private_address_is_refused_without_the_setting(
        self, monkeypatch, launched, moodle, db_session
    ):
        monkeypatch.delenv("INTEGRATION_ALLOW_PRIVATE_URLS", raising=False)
        monkeypatch.setattr(
            net_guard, "_resolve",
            lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))],
        )
        launched.scored(0.8)
        [row] = db_session.query(Cmi5AgsScore).all()
        assert row.state == AGS_FAILED and "address" in row.last_error
        assert not moodle.token_route.called and not moodle.called

    def test_the_access_token_is_reused_until_it_expires(self, launched, moodle):
        launched.completed()
        launched.scored(0.8)
        assert len(_sent(moodle)) == 2 and moodle.token_route.call_count == 1

    def test_a_first_result_racing_another_insert_updates_that_row(self, monkeypatch, launched, moodle, db_session):
        launched.completed()  # the cell exists now
        real_cell = ags._cell
        seen = []

        def racing(db, key):
            seen.append(key)
            return None if len(seen) == 1 else real_cell(db, key)  # as if another insert had won

        monkeypatch.setattr(ags, "_cell", racing)
        assert launched.scored(0.8).status_code == 200
        [row] = db_session.query(Cmi5AgsScore).all()
        assert row.score_given == 80.0 and row.state == AGS_SENT


def test_a_later_lower_fail_never_replaces_the_best_mark(client, lti, platform, release, db_session, moodle):
    student = _lti_student(db_session, platform, release)
    assert lti(platform, f"cmi5:{release['id']}:0", **AGS_CLAIM).status_code == 302
    first = _spa_launch(client, student, release)
    first.initialized()
    assert first.scored(0.6).status_code == 200  # failed (mastery 0.7)
    first.terminated()
    second = _spa_launch(client, student, release)
    second.initialized()
    assert second.scored(0.2).status_code == 200  # failed again, lower
    assert [s["scoreGiven"] for s in _sent(moodle)] == [60.0]  # the lower one is not sent
    [row] = db_session.query(Cmi5AgsScore).all()
    assert row.score_given == 60.0


class TestDeepLinkLimits:
    def test_more_than_fifty_items_are_refused(self, client, lti, platform, release):
        page = _picker(lti, platform)
        session = html.unescape(re.search(r'name="session" value="([^"]+)"', page).group(1))
        items = [f"cmi5:{release['id']}:0"] * 51
        assert client.post("/lti/deeplink/finish", data={"session": session, "item": items}).status_code == 422

    def test_a_superseded_release_is_no_new_content_item_but_its_students_still_launch(
        self, lti, platform, release, db_session
    ):
        from app.course_releases.models import CourseRelease

        student = _lti_student(db_session, platform, release)
        db_session.get(CourseRelease, uuid.UUID(release["id"])).state = SUPERSEDED
        db_session.commit()
        assert cmi5_lti.content_item(db_session, platform.tenant_id, f"{release['id']}:0") is None
        assert lti(platform, f"cmi5:{release['id']}:0").status_code == 302
        assert db_session.query(Cmi5Registration).filter_by(user_id=student.id).count() == 1

    def test_releases_without_a_cmi5_package_are_parsed_once_and_skipped(
        self, monkeypatch, platform, release, db_session
    ):
        monkeypatch.setattr(cmi5_lti, "_TITLES", type(cmi5_lti._TITLES)())
        calls = []

        def no_package(db, rel):
            calls.append(rel.id)
            raise content_mod.ContentError("no cmi5")

        monkeypatch.setattr(content_mod, "package", no_package)
        assert cmi5_lti.picker_items(db_session, platform.tenant_id) == []
        assert cmi5_lti.picker_items(db_session, platform.tenant_id) == []
        assert len(calls) == 1


# -- TrueNorth's own Moodle farm (app/moodle_farm) ---------------------------------------------
def _farm_claims(tn_id, username: str | None = None) -> dict:
    """What Moodle 5.2.3 sends for an account TrueNorth's sign-in made (checked against the
    real Moodle): idnumber as lis.person_sourcedid, username as ext.user_username."""
    return {
        farm.CLAIM_LIS: {"person_sourcedid": str(tn_id), "course_section_sourcedid": "c"},
        farm.CLAIM_EXT: {"user_username": f"tn-{tn_id}" if username is None else username, "lms": "moodle-2"},
    }


def _tn_student(db, release, tenant=DEV_TENANT, role=UserRole.student, enrol=True) -> User:
    """A TrueNorth account with its own sign-in (Keycloak), as farm Students have."""
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=f"farm-{uuid.uuid4().hex[:6]}@x.test",
             display_name="Farm Student", role=role, tenant_id=tenant)
    db.add(u)
    db.flush()
    if enrol:
        ensure_enrollment(db, user_id=u.id, course_id=uuid.UUID(release["course_id"]), tenant_id=tenant)
    db.commit()
    return u


def _links(db, platform) -> list[tuple[str, uuid.UUID]]:
    return [(link.lti_sub, link.user_id) for link in db.query(LTIUserLink).filter_by(platform_id=platform.id)]


class TestFarmAccounts:
    def test_a_farm_account_is_bound_by_its_locked_id_and_gets_the_grade(
        self, client, lti, platform, release, db_session, moodle
    ):
        farm.mark(db_session, platform, "default", "test")
        db_session.commit()
        student = _tn_student(db_session, release)
        # The email is not the Student's: the binding is the locked id and username, not email.
        resp = lti(platform, f"cmi5:{release['id']}:0", sub="4", email="someone@else.test",
                   **AGS_CLAIM, **_farm_claims(student.id))
        assert resp.status_code == 302
        assert resp.headers["location"].endswith(f"/au/releases/{release['id']}?launch=0&lti=1")
        assert _links(db_session, platform) == [("4", student.id)]
        launch = db_session.query(LTILaunch).filter_by(lti_user_sub="4").one()
        assert launch.user_id == student.id and launch.ags_lineitem_url == LINEITEM

        au = _spa_launch(client, student, release)
        au.initialized()
        assert au.scored(0.8).status_code == 200
        assert [(s["userId"], s["scoreGiven"]) for s in _sent(moodle)] == [("4", 80.0)]
        assert lti(platform, f"cmi5:{release['id']}:0", sub="4", **_farm_claims(student.id)).status_code == 302
        assert _links(db_session, platform) == [("4", student.id)]  # once

    def test_the_same_claims_from_a_platform_that_is_not_a_farm_node_bind_nothing(
        self, client, lti, platform, release, db_session, moodle
    ):
        student = _tn_student(db_session, release)
        resp = lti(platform, f"cmi5:{release['id']}:0", sub="4", email=student.email,
                   **AGS_CLAIM, **_farm_claims(student.id))
        assert resp.status_code == 302  # signed in by the email match, as before
        assert _links(db_session, platform) == []
        assert db_session.query(LTILaunch).filter_by(lti_user_sub="4").one().ags_lineitem_url == ""
        au = _spa_launch(client, student, release)
        au.initialized()
        au.scored(0.8)
        assert _sent(moodle) == []

    @pytest.mark.parametrize("case", ["other username", "no username", "other id", "no id", "id not a uuid"])
    def test_the_locked_id_and_the_username_must_agree(self, lti, platform, release, db_session, case):
        farm.mark(db_session, platform, "default", "test")
        db_session.commit()
        student = _tn_student(db_session, release)
        claims = _farm_claims(student.id)
        if case == "other username":  # a self-chosen username names another Student
            claims[farm.CLAIM_EXT]["user_username"] = f"tn-{uuid.uuid4()}"
        elif case == "no username":
            claims[farm.CLAIM_EXT].pop("user_username")
        elif case == "other id":  # the username is this Student's, the locked id someone else's
            claims[farm.CLAIM_LIS]["person_sourcedid"] = str(uuid.uuid4())
        elif case == "no id":  # self-registered as tn-<id>: no idnumber, and the Student cannot set it
            claims[farm.CLAIM_LIS]["person_sourcedid"] = ""
        else:
            claims[farm.CLAIM_LIS]["person_sourcedid"] = "not-a-uuid"
            claims[farm.CLAIM_EXT]["user_username"] = "tn-not-a-uuid"
        lti(platform, f"cmi5:{release['id']}:0", sub="4", **AGS_CLAIM, **claims)
        assert _links(db_session, platform) == []
        assert db_session.query(LTILaunch).filter_by(user_id=student.id).count() == 0

    def test_another_tenants_student_is_not_bound(self, lti, platform, release, db_session):
        farm.mark(db_session, platform, "default", "test")
        theirs = _tn_student(db_session, release, tenant=real_tenant(db_session).id, enrol=False)
        lti(platform, f"cmi5:{release['id']}:0", sub="4", **AGS_CLAIM, **_farm_claims(theirs.id))
        assert _links(db_session, platform) == []

    def test_staff_are_not_bound_by_it(self, lti, platform, release, db_session):
        farm.mark(db_session, platform, "default", "test")
        teacher = _tn_student(db_session, release, role=UserRole.instructor, enrol=False)
        lti(platform, f"course:{release['course_id']}", sub="4", **_farm_claims(teacher.id))
        assert _links(db_session, platform) == []  # staff link explicitly, signed in

    def test_an_existing_link_is_never_repointed(self, lti, platform, release, db_session):
        farm.mark(db_session, platform, "default", "test")
        student = _tn_student(db_session, release)
        db_session.add(LTIUserLink(platform_id=platform.id, lti_sub="old", user_id=student.id, lms_name="x"))
        db_session.commit()
        lti(platform, f"cmi5:{release['id']}:0", sub="4", **AGS_CLAIM, **_farm_claims(student.id))
        assert _links(db_session, platform) == [("old", student.id)]

    def test_a_farm_node_on_a_private_address_gets_its_grade(self, monkeypatch, launched, moodle, platform, db_session):
        monkeypatch.delenv("INTEGRATION_ALLOW_PRIVATE_URLS", raising=False)
        monkeypatch.setattr(
            net_guard, "_resolve",
            lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))],
        )
        farm.mark(db_session, platform, "default", "test")
        db_session.commit()
        launched.scored(0.8)
        [row] = db_session.query(Cmi5AgsScore).all()
        assert row.state == AGS_SENT and moodle.called

    def test_not_even_a_farm_node_reaches_loopback(self, monkeypatch, launched, moodle, platform, db_session):
        monkeypatch.setattr(
            net_guard, "_resolve",
            lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
        )
        farm.mark(db_session, platform, "default", "test")
        db_session.commit()
        launched.scored(0.8)
        [row] = db_session.query(Cmi5AgsScore).all()
        assert row.state == AGS_FAILED and not moodle.called


class TestFarmMarker:
    REG = {
        "lti_issuer": "https://farm.example.test", "lti_client_id": "c1", "lti_deployment_id": "1",
        "lti_auth_login_url": "https://farm.example.test/mod/lti/auth.php",
        "lti_token_url": "https://farm.example.test/mod/lti/token.php",
        "lti_jwks_url": "https://farm.example.test/mod/lti/certs.php",
    }

    def test_the_installer_marks_the_node_it_registers(self, db_session):
        out = install_cli.register(db_session, "default", "farm1", "http://moodle-farm1:8080", self.REG)
        p = db_session.get(ExternalPlatform, uuid.UUID(out["platform_id"]))
        assert farm.is_managed(db_session, p)
        again = install_cli.register(db_session, "default", "farm1", "http://moodle-farm1:8080", self.REG)
        assert again["action"] == "unchanged"

    def test_manage_marks_a_node_the_farm_script_registered(self, db_session):
        p = _platform(db_session, DEV_TENANT, issuer="https://farm2.example.test")
        p.slug = "moodle-farm2"
        db_session.commit()
        assert not farm.is_managed(db_session, p)
        assert install_cli.manage(db_session, str(DEV_TENANT), "farm2")["action"] == "marked"
        assert install_cli.manage(db_session, "default", "farm2")["action"] == "unchanged"
        assert farm.is_managed(db_session, p)
        with pytest.raises(install_cli.InstallError) as e:
            install_cli.manage(db_session, "default", "nope")
        assert e.value.code == 3

    def test_the_api_cannot_mark_one_and_a_changed_address_ends_it(self, client, platform, db_session):
        from app.schemas import ExternalPlatformIn, ExternalPlatformUpdate

        assert not {"managed", "farm"} & (set(ExternalPlatformIn.model_fields) | set(ExternalPlatformUpdate.model_fields))
        farm.mark(db_session, platform, "default", "test")
        db_session.commit()
        assert client.patch(f"/integrations/platforms/{platform.id}", json={"name": "Renamed"}).status_code == 200
        assert client.patch(f"/integrations/platforms/{platform.id}", json={"base_url": ISSUER}).status_code == 200
        assert farm.is_managed(db_session, platform)  # same address: still the installer's
        r = client.patch(f"/integrations/platforms/{platform.id}", json={"base_url": "http://10.9.9.9:8080"})
        assert r.status_code == 200
        assert not farm.is_managed(db_session, platform)

    def test_deregistering_the_platform_removes_the_marker(self, client, platform, db_session):
        from app.moodle_farm.models import ManagedMoodleNode

        farm.mark(db_session, platform, "default", "test")
        db_session.commit()
        assert client.delete(f"/integrations/platforms/{platform.id}").status_code == 204
        assert db_session.query(ManagedMoodleNode).count() == 0
