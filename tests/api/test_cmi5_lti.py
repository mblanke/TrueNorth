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

import html
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import respx
from _cmi5_kit import AU, BUNDLE, CATALOGUE, CROSSWALK, MemoryLRS
from _shared import real_tenant
from app import lti13
from app.auth import CurrentUser, get_current_user
from app.cmi5 import ags, lms
from app.cmi5.models import AGS_FAILED, AGS_PENDING, AGS_SENT, Cmi5AgsScore, Cmi5Registration
from app.db import get_db
from app.enrollment import ensure_enrollment
from app.main import app as fastapi_app
from app.models import Course, Enrollment, ExternalPlatform, IntegrationAuthType, LTILaunch, User, UserRole
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
        mock.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", "expires_in": 3600}))
        route = mock.post(SCORES).mock(return_value=Response(200, json={}))
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
