"""TrueNorth as the cmi5 LMS for its own releases (app/cmi5): launch, fetch, the AU's LRS,
moveOn, satisfied, abandon, waive, and the refusals the cmi5 spec requires.

The release is the committed C105 bundle (six AUs, one per block, moveOn Passed, mastery
0.7), uploaded and accepted through the API. The LRS is ``MemoryLRS`` (tests/api/
_cmi5_kit.py); the same flow against a real lrsql is tests/integration/test_cmi5_lrs.py.
Each refusal is asserted with the cmi5 requirement id it reports (CATAPULT's numbering).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from _cmi5_kit import ABANDONED, AU, BUNDLE, CATALOGUE, CMI5, CROSSWALK, EXT, SATISFIED, WAIVED, MemoryLRS
from app import xapi
from app.auth import CurrentUser, get_current_user
from app.cmi5 import lms
from app.cmi5.models import Cmi5Session
from app.enrollment import ensure_enrollment
from app.lms import NullLMSBackend
from app.main import app as fastapi_app
from app.models import Tenant, User, UserRole

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def lrs(monkeypatch):
    mem = MemoryLRS()
    monkeypatch.setattr(lms, "get_lms_backend", lambda: mem)
    return mem


@pytest.fixture
def release(client):
    """C105, uploaded and accepted by the dev admin (AUTH_DISABLED)."""
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
    return rel


def _user(db, role=UserRole.student, tenant=DEV_TENANT) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Student",
        role=role,
        tenant_id=tenant,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def student(db_session, release):
    u = _user(db_session)
    ensure_enrollment(db_session, user_id=u.id, course_id=uuid.UUID(release["course_id"]), tenant_id=DEV_TENANT)
    db_session.commit()
    return u


@contextmanager
def acting_as(user: User):
    who = CurrentUser(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
        keycloak_id=user.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _launch(client, user, release, index=0, **body):
    with acting_as(user):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/{index}/launch", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# -- launch -----------------------------------------------------------------------------------
def test_launch_writes_launch_data_and_launched_then_returns_the_au_url(client, lrs, release, student):
    out = _launch(client, student, release)
    url = urlsplit(out["url"])
    assert url.path == f"/au/releases/{release['id']}/0"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert set(q) == {"endpoint", "fetch", "actor", "registration", "activityId"}
    actor = json.loads(q["actor"])
    assert actor == xapi.actor(student.id)  # account IFI, no email
    assert q["registration"] == out["registration"]
    runtime = q["activityId"]
    assert runtime.endswith(f"/cmi5/releases/{release['id']}/au/0")
    assert "ccoe.forces.gc.ca" not in runtime  # 8.1.5.0-3: never the publisher id
    assert q["endpoint"].endswith("/cmi5/lrs/") and "/cmi5/fetch/" in q["fetch"]

    data = lrs.launch_data(runtime, actor, q["registration"])
    assert data["launchMode"] == "Normal" and data["moveOn"] == "Passed" and data["masteryScore"] == 0.7
    assert data["contextTemplate"]["extensions"] == {EXT + "sessionid": out["session_id"]}
    assert data["contextTemplate"]["contextActivities"]["grouping"][0]["id"].endswith("/au/mod_001")
    assert data["returnURL"].endswith(f"/au/releases/{release['id']}")
    assert json.loads(data["launchParameters"])["module"] == "mod_001"

    (launched,) = lrs.by_verb("http://adlnet.gov/expapi/verbs/launched")
    ext = launched["context"]["extensions"]
    assert launched["object"]["id"] == runtime and launched["actor"] == actor
    assert launched["context"]["registration"] == q["registration"]
    assert {"id": CMI5} in launched["context"]["contextActivities"]["category"]
    assert ext[EXT + "sessionid"] == out["session_id"] and ext[EXT + "launchmode"] == "Normal"
    assert ext[EXT + "moveon"] == "Passed" and ext[EXT + "masteryscore"] == 0.7
    assert ext[EXT + "launchurl"] == out["url"].split("?")[0]
    assert ext[EXT + "launchparameters"] == data["launchParameters"]
    assert launched["timestamp"].endswith("Z")


def test_the_registration_is_the_enrolment(client, lrs, release, student, db_session):
    from app.models import Enrollment

    out = _launch(client, student, release)
    enrolment = db_session.query(Enrollment).filter(Enrollment.user_id == student.id).one()
    assert out["registration"] == str(enrolment.id)


def test_launch_needs_an_enrolment_on_this_release(client, lrs, release, db_session):
    stranger = _user(db_session)
    with acting_as(stranger):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={})
    assert r.status_code == 403
    assert lrs.statements == {}


def test_another_tenants_release_is_not_found(client, lrs, release, db_session):
    other = Tenant(id=uuid.uuid4(), name="other", slug=f"o-{uuid.uuid4().hex[:6]}")
    db_session.add(other)
    db_session.flush()
    outsider = _user(db_session, UserRole.admin, tenant=other.id)
    with acting_as(outsider):
        for path in ("structure", "aus/0/content", "cmi5.xml"):
            assert client.get(f"/cmi5/releases/{release['id']}/{path}").status_code == 404, path
        assert client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={}).status_code == 404


def test_without_an_lrs_launch_says_so(client, monkeypatch, release, student):
    monkeypatch.setattr(lms, "get_lms_backend", lambda: NullLMSBackend())
    with acting_as(student):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={})
    assert r.status_code == 503 and "LMS_BACKEND" in r.json()["detail"]


def test_a_refused_launched_statement_fails_the_launch_and_leaves_no_session(client, lrs, release, student, db_session):
    lrs.refuse_statements = True
    with acting_as(student):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={})
    assert r.status_code == 502
    assert db_session.query(Cmi5Session).count() == 0


# -- fetch ------------------------------------------------------------------------------------
def test_fetch_returns_the_token_once(client, lrs, release, student):
    au = AU(client, _launch(client, student, release)["url"])
    first = au.fetch()
    assert set(first) == {"auth-token"}
    assert au.fetch() == {"error-code": "1", "error-text": "The authorization token has already been returned."}
    r = client.post("/cmi5/fetch/" + "x" * 43)
    assert r.status_code == 200 and r.json()["error-code"] == "2"


# -- a whole session ----------------------------------------------------------------------------
def test_a_conformant_session_passes_and_satisfies_its_block(client, lrs, release, student):
    out = _launch(client, student, release)
    au = AU(client, out["url"]).start()
    for step in (au.initialized, au.completed, lambda: au.scored(0.8), au.terminated):
        r = step()
        assert r.status_code == 200, r.text
    assert lrs.verbs(au.registration) == ["launched", "initialized", "completed", "passed", "satisfied", "terminated"]

    (sat,) = lrs.by_verb(SATISFIED)
    assert sat["object"]["definition"]["type"] == "https://w3id.org/xapi/cmi5/activitytype/block"
    assert sat["object"]["id"].endswith(f"/cmi5/releases/{release['id']}/block/0")
    assert sat["context"]["contextActivities"]["grouping"] == [
        {"id": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-c105/block/mod_001"}
    ]
    assert sat["context"]["contextActivities"]["category"] == [{"id": CMI5}]
    assert sat["context"]["extensions"][EXT + "sessionid"] == out["session_id"]
    assert sat["context"]["registration"] == au.registration and sat["actor"] == au.actor

    # The session is over: its token is dead.
    r = au.send(au.statement("experienced", defined=False))
    assert r.status_code == 403

    with acting_as(student):
        s = client.get(f"/cmi5/releases/{release['id']}/structure").json()
    assert s["registration"] == au.registration and s["enrolled"] is True
    assert (s["aus"][0]["completed"], s["aus"][0]["passed"], s["aus"][0]["satisfied"]) == (True, True, True)
    assert not s["aus"][1]["satisfied"] and s["course_satisfied"] is False


def test_waiving_the_rest_satisfies_the_course(client, lrs, release, student, db_session):
    au = AU(client, _launch(client, student, release)["url"]).start()
    assert au.initialized().status_code == 200
    assert au.scored(1.0).status_code == 200
    instructor = _user(db_session, UserRole.instructor)
    with acting_as(instructor):
        for i in range(1, 6):
            r = client.post(f"/cmi5/registrations/{au.registration}/aus/{i}/waive", json={"reason": "Tested Out"})
            assert r.status_code == 200, r.text
        assert r.json()["satisfied"] == ["block:5", "course"]
        again = client.post(f"/cmi5/registrations/{au.registration}/aus/1/waive", json={"reason": "Administrative"})
        assert again.status_code == 409
    waived = lrs.by_verb(WAIVED)
    assert len(waived) == 5
    w = waived[0]
    assert w["result"] == {
        "success": True,
        "completion": True,
        "extensions": {"https://w3id.org/xapi/cmi5/result/extensions/reason": "Tested Out"},
    }
    assert {"id": "https://w3id.org/xapi/cmi5/context/categories/moveon"} in w["context"]["contextActivities"][
        "category"
    ]
    course = [s for s in lrs.by_verb(SATISFIED) if s["object"]["definition"]["type"].endswith("/course")]
    assert len(course) == 1
    assert course[0]["context"]["contextActivities"]["grouping"] == [
        {"id": "https://ccoe.forces.gc.ca/xapi/arc2/arc2-c105"}
    ]
    assert course[0]["object"]["id"].endswith(f"/cmi5/releases/{release['id']}")


def test_a_student_cannot_waive(client, lrs, release, student):
    au = AU(client, _launch(client, student, release)["url"])
    with acting_as(student):
        r = client.post(f"/cmi5/registrations/{au.registration}/aus/1/waive", json={"reason": "Tested Out"})
    assert r.status_code == 403


def test_relaunch_abandons_the_open_session(client, lrs, release, student):
    first = AU(client, _launch(client, student, release)["url"]).start()
    assert first.initialized().status_code == 200
    second = AU(client, _launch(client, student, release)["url"])
    (ab,) = lrs.by_verb(ABANDONED)
    assert (
        ab["context"]["extensions"][EXT + "sessionid"]
        == first.launch_data["contextTemplate"]["extensions"][EXT + "sessionid"]
    )
    assert ab["result"]["duration"].startswith("PT")
    r = first.send(first.statement("experienced", defined=False))
    assert r.status_code == 403 and "abandoned" in r.text
    second.start()
    assert second.initialized().status_code == 200


def test_after_passing_the_next_launch_is_review(client, lrs, release, student):
    au = AU(client, _launch(client, student, release)["url"]).start()
    au.initialized(), au.scored(0.9), au.terminated()
    again = _launch(client, student, release)
    assert again["launch_mode"] == "Review"
    review = AU(client, again["url"]).start()
    assert review.launch_data["launchMode"] == "Review"
    assert review.initialized().status_code == 200
    r = review.completed()
    assert r.status_code == 400 and r.json()["violatedReqId"] == "10.2.2.0-3"


def test_browse_mode_refuses_judgement(client, lrs, release, student):
    au = AU(client, _launch(client, student, release, launch_mode="Browse")["url"]).start()
    assert au.initialized().status_code == 200
    r = au.scored(1.0)
    assert r.status_code == 400 and r.json()["violatedReqId"] == "10.2.2.0-2"


# -- refusals -----------------------------------------------------------------------------------
def _started(client, student, release):
    return AU(client, _launch(client, student, release)["url"]).start()


def _refused(r, req, status=400):
    assert r.status_code == status, r.text
    assert r.json()["violatedReqId"] == req, r.text
    assert r.headers["X-Experience-API-Version"] == "1.0.3"


def test_learner_preferences_must_be_read_first(client, lrs, release, student):
    au = AU(client, _launch(client, student, release)["url"])
    au.fetch()
    au.launch_data = au.lrs("GET", "activities/state", params=au.state_params("LMS.LaunchData")).json()
    _refused(au.initialized(), "11.0.0.0-3")


@pytest.mark.parametrize(
    ("mutate", "req"),
    [
        (lambda au, st: st.update(actor=xapi.actor(uuid.uuid4())), "8.1.3.0-3"),
        (lambda au, st: st["actor"].update(mbox="mailto:x@example.test"), "8.1.3.0-3"),
        (lambda au, st: st["context"].update(registration=str(uuid.uuid4())), "9.6.1.0-1"),
        (lambda au, st: st["context"]["extensions"].update({EXT + "sessionid": str(uuid.uuid4())}), "10.2.1.0-7"),
        (lambda au, st: st["context"]["contextActivities"].update(grouping=[]), "10.2.1.0-6"),
        (lambda au, st: st.pop("timestamp"), "9.7.0.0-1"),
        (lambda au, st: st.update(timestamp="2026-10-09T10:00:00+02:00"), "9.7.0.0-2"),
        (lambda au, st: st.pop("id"), "9.1.0.0-1"),
        (
            lambda au, st: st["object"].update(id="https://ccoe.forces.gc.ca/xapi/arc2/arc2-c105/au/mod_001"),
            "8.1.5.0-6",
        ),
    ],
)
def test_a_malformed_initialized_is_refused(client, lrs, release, student, mutate, req):
    au = _started(client, student, release)
    st = au.statement("initialized")
    mutate(au, st)
    _refused(au.send(st), req)
    assert lrs.by_verb("http://adlnet.gov/expapi/verbs/initialized") == []


def test_ordering_and_once_only_rules(client, lrs, release, student):
    au = _started(client, student, release)
    _refused(au.send(au.statement("experienced", defined=False)), "9.3.0.0-4")  # before initialized
    assert au.initialized().status_code == 200
    _refused(au.initialized(), "9.3.0.0-2")
    _refused(au.send(au.statement("initialized", moveon=True)), "9.6.2.2-2")
    assert au.completed().status_code == 200
    _refused(au.completed(), "9.3.0.0-2")
    r = au.send(
        au.statement(
            "passed",
            moveon=True,
            result={"success": True, "score": {"scaled": 0.5}, "duration": "PT1S"},
            ext={EXT + "masteryscore": 0.7},
        )
    )
    _refused(r, "9.3.4.0-2")  # passed below the masteryScore
    assert au.scored(0.4).status_code == 200  # failed
    _refused(au.scored(0.9), "9.3.0.0-3")  # passed after failed in the same session
    assert au.terminated().status_code == 200


def test_result_rules(client, lrs, release, student):
    au = _started(client, student, release)
    au.initialized()
    _refused(au.send(au.statement("completed", moveon=True, result={"completion": True})), "9.5.4.1-2")
    _refused(au.send(au.statement("completed", result={"completion": True, "duration": "PT1S"})), "9.6.2.2-1")
    _refused(au.send(au.statement("completed", moveon=True)), "9.5.3.0-1")
    _refused(
        au.send(
            au.statement("passed", moveon=True, result={"success": True, "score": {"scaled": 0.9}, "duration": "PT1S"})
        ),
        "9.6.3.2-2",  # judged against a masteryScore without saying so
    )
    _refused(
        au.send(
            au.statement(
                "passed",
                moveon=True,
                result={"success": True, "score": {"raw": 9}, "duration": "PT1S"},
                ext={EXT + "masteryscore": 0.7},
            )
        ),
        "9.5.1.0-3",
    )
    _refused(au.send(au.statement("terminated")), "9.5.4.1-1")
    _refused(au.send(au.statement("http://adlnet.gov/expapi/verbs/experienced")), "9.3.0.0-1")


def test_completed_once_per_registration_across_sessions(client, lrs, release, student):
    au = _started(client, student, release)
    au.initialized(), au.completed(), au.terminated()
    again = AU(client, _launch(client, student, release)["url"]).start()  # still Normal: not passed yet
    assert again.launch_data["launchMode"] == "Normal"
    again.initialized()
    _refused(again.completed(), "9.3.0.0-6")


def test_the_au_cannot_send_lms_verbs_or_void(client, lrs, release, student):
    au = _started(client, student, release)
    au.initialized()
    _refused(au.send(au.statement(SATISFIED)), "9.3.0.0-1", status=403)
    void = au.statement("http://adlnet.gov/expapi/verbs/voided", defined=False)
    void["object"] = {"objectType": "StatementRef", "id": str(uuid.uuid4())}
    _refused(au.send(void), "6.3.0.0-1", status=403)


def test_the_au_credential_is_scoped_to_its_session(client, lrs, release, student):
    au = _started(client, student, release)
    # LMS.LaunchData is read-only
    r = au.lrs("PUT", "activities/state", params=au.state_params("LMS.LaunchData"), json_body={"launchMode": "Normal"})
    _refused(r, "10.2.1.0-5", status=403)
    # its own State document works, another activity's does not
    own = au.state_params("au.status")
    assert (
        au.lrs(
            "PUT", "activities/state", params=own, json_body={"completed": True}, headers={"If-None-Match": "*"}
        ).status_code
        == 204
    )
    assert au.lrs("GET", "activities/state", params=own).json() == {"completed": True}
    other = {**own, "activityId": "https://elsewhere.example/au"}
    _refused(au.lrs("GET", "activities/state", params=other), "10.1.0.0-3", status=403)
    other_agent = {**own, "agent": json.dumps(xapi.actor(uuid.uuid4()))}
    _refused(au.lrs("GET", "activities/state", params=other_agent), "8.1.3.0-3", status=403)
    # learner preferences: readable, not writable
    prefs = {"agent": json.dumps(au.actor), "profileId": "cmi5LearnerPreferences"}
    assert (
        au.lrs(
            "PUT", "agents/profile", params=prefs, json_body={"languagePreference": "fr-CA", "audioPreference": "on"}
        ).status_code
        == 403
    )
    # no reading statements, no other resources, no other credential
    assert au.lrs("GET", "statements").status_code == 403
    assert au.lrs("GET", "agents/profile", params={"profileId": "x"}).status_code == 400
    assert au.lrs("GET", "nonsense").status_code == 404
    assert client.get(f"{au.endpoint}about", headers={"Authorization": "Basic bm9wZTpub3Bl"}).status_code == 401
    assert client.get(f"{au.endpoint}about").status_code == 401
    assert au.lrs("GET", "about").status_code == 200


def test_a_token_is_not_its_fetch_secret(client, lrs, release, student):
    out = _launch(client, student, release)
    au = AU(client, out["url"])
    secret = au.fetch_url.rsplit("/", 1)[1]
    import base64

    forged = base64.b64encode(f"{out['session_id']}:{secret}".encode()).decode()
    assert client.get(f"{au.endpoint}about", headers={"Authorization": f"Basic {forged}"}).status_code == 401


# -- content and the served structure -------------------------------------------------------------
def test_content_has_pages_and_questions_but_no_answers(client, lrs, release, student):
    with acting_as(student):
        r = client.get(f"/cmi5/releases/{release['id']}/aus/0/content")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["pages"]) == 5 and body["pages"][0]["html"].strip()
    assert body["quiz"]["questions"][0]["options"]
    assert '"answer"' not in r.text and "answer" not in body["quiz"]["questions"][0]
    assert (body["move_on"], body["mastery_score"], body["lang"]) == ("Passed", 0.7, "en-CA")


def test_grade_marks_on_the_server(client, lrs, release, student, db_session):
    from app.cmi5 import content as content_mod
    from app.course_releases.models import CourseRelease

    with acting_as(student):
        empty = client.post(f"/cmi5/releases/{release['id']}/aus/0/grade", json={"answers": {}})
    assert empty.json() == {"correct": 0, "total": 5, "scaled": 0.0}
    # The right answers, read from the release the way the server reads them, give full marks.
    bundle, _ = content_mod.package(db_session, db_session.get(CourseRelease, uuid.UUID(release["id"])))
    cfg = json.loads(bundle.files["learner"]["07-bundle/cmi5/mod_001/course-config.json"])
    answers = {q["id"]: q["answer"].lower() for q in cfg["quiz"]["questions"]}
    with acting_as(student):
        full = client.post(f"/cmi5/releases/{release['id']}/aus/0/grade", json={"answers": answers})
    assert full.json() == {"correct": 5, "total": 5, "scaled": 1.0}


def test_a_student_cannot_download_the_structure_file(client, lrs, release, student):
    with acting_as(student):
        assert client.get(f"/cmi5/releases/{release['id']}/cmi5.xml").status_code == 403
