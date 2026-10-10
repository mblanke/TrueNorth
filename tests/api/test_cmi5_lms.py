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
from _cmi5_kit import ABANDONED, ADL, AU, BUNDLE, CATALOGUE, CMI5, CROSSWALK, EXT, SATISFIED, WAIVED, MemoryLRS
from app import xapi
from app.auth import CurrentUser, get_current_user
from app.cmi5 import lms
from app.cmi5.models import Cmi5Session
from app.db import get_db
from app.enrollment import ensure_enrollment
from app.lms import NullLMSBackend
from app.main import app as fastapi_app
from app.models import Tenant, User, UserRole

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


AU_CRED = "YXUta2V5OmF1LXNlY3JldA=="  # base64 "au-key:au-secret": the AU traffic's own credential
SERVER_CRED = "c2VydmVyOnNlY3JldA=="  # base64 "server:secret": TrueNorth's LRS_AUTH


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("CMI5_LRS_AUTH", AU_CRED)
    monkeypatch.setenv("LRS_AUTH", SERVER_CRED)


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
    # A Student reaches a module only in a published course (catalogue courses import unpublished).
    from app.models import Course

    db = client.app.dependency_overrides[get_db]().__next__()
    db.get(Course, uuid.UUID(rel["course_id"])).is_published = True
    db.commit()
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
    au = AU(client, _launch(client, student, release)["url"], student)
    first = au.fetch()
    assert set(first) == {"auth-token"}
    assert au.fetch() == {"error-code": "1", "error-text": "The authorization token has already been returned."}
    r = client.post("/cmi5/fetch/" + "x" * 43)
    assert r.status_code == 200 and r.json()["error-code"] == "2"


# -- a whole session ----------------------------------------------------------------------------
def test_a_conformant_session_passes_and_satisfies_its_block(client, lrs, release, student):
    out = _launch(client, student, release)
    au = AU(client, out["url"], student).start()
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
    au = AU(client, _launch(client, student, release)["url"], student).start()
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
    au = AU(client, _launch(client, student, release)["url"], student)
    with acting_as(student):
        r = client.post(f"/cmi5/registrations/{au.registration}/aus/1/waive", json={"reason": "Tested Out"})
    assert r.status_code == 403


def test_relaunch_abandons_the_open_session(client, lrs, release, student):
    first = AU(client, _launch(client, student, release)["url"], student).start()
    assert first.initialized().status_code == 200
    second = AU(client, _launch(client, student, release)["url"], student)
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
    au = AU(client, _launch(client, student, release)["url"], student).start()
    au.initialized(), au.scored(1.0), au.terminated()
    again = _launch(client, student, release)
    assert again["launch_mode"] == "Review"
    review = AU(client, again["url"], student).start()
    assert review.launch_data["launchMode"] == "Review"
    assert review.initialized().status_code == 200
    r = review.completed()
    assert r.status_code == 400 and r.json()["violatedReqId"] == "10.2.2.0-3"


def test_browse_mode_refuses_judgement(client, lrs, release, student):
    au = AU(client, _launch(client, student, release, launch_mode="Browse")["url"], student).start()
    assert au.initialized().status_code == 200
    r = au.scored(1.0)
    assert r.status_code == 400 and r.json()["violatedReqId"] == "10.2.2.0-2"


# -- refusals -----------------------------------------------------------------------------------
def _started(client, student, release):
    return AU(client, _launch(client, student, release)["url"], student).start()


def _refused(r, req, status=400):
    assert r.status_code == status, r.text
    assert r.json()["violatedReqId"] == req, r.text
    assert r.headers["X-Experience-API-Version"] == "1.0.3"


def test_learner_preferences_must_be_read_first(client, lrs, release, student):
    au = AU(client, _launch(client, student, release)["url"], student)
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
            "TN-SCOPE",
        ),
    ],
)
def test_a_malformed_initialized_is_refused(client, lrs, release, student, mutate, req):
    au = _started(client, student, release)
    st = au.statement("initialized")
    mutate(au, st)
    _refused(au.send(st), req, 403 if req.startswith("TN-") else 400)
    assert lrs.by_verb("http://adlnet.gov/expapi/verbs/initialized") == []


def test_ordering_and_once_only_rules(client, lrs, release, student):
    au = _started(client, student, release)
    _refused(au.send(au.statement("experienced", defined=False)), "9.3.0.0-4")  # before initialized
    assert au.initialized().status_code == 200
    _refused(au.initialized(), "9.3.0.0-2")
    _refused(au.send(au.statement("initialized", moveon=True)), "9.6.2.2-2")
    assert au.completed().status_code == 200
    _refused(au.completed(), "9.3.0.0-2")
    au.mark(0.6)
    r = au.send(
        au.statement(
            "passed",
            moveon=True,
            result={"success": True, "score": {"scaled": 0.6}, "duration": "PT1S"},
            ext={EXT + "masteryscore": 0.7},
        )
    )
    _refused(r, "9.3.4.0-2")  # passed below the masteryScore
    assert au.scored(0.4).status_code == 200  # failed
    _refused(au.scored(1.0), "9.3.0.0-3")  # passed after failed in the same session
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
    again = AU(client, _launch(client, student, release)["url"], student).start()  # still Normal: not passed yet
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
    au = AU(client, out["url"], student)
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


# -- adversarial review of 2026-10-09: one regression per finding --------------------------
def _quiz_iri() -> str:
    return xapi.activity_iri("quiz", str(uuid.uuid4()))


def test_review_1_a_student_cannot_forge_a_truenorth_quiz_pass(client, lrs, release, student):
    """Repro: an uncategorised `passed` about another TrueNorth activity went through with the
    server's credential, indistinguishable from TrueNorth's own quiz statements."""
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    forged = au.statement(
        "passed",
        defined=False,
        result={"success": True, "score": {"scaled": 1.0}},
        object={"objectType": "Activity", "id": _quiz_iri()},
    )
    _refused(au.send(forged), "TN-SCOPE", 403)
    assert forged["id"] not in lrs.statements


def test_review_1_cmi5_verbs_need_the_cmi5_category(client, lrs, release, student):
    """Repro: `passed` on the AU itself without the category skipped every cmi5 rule."""
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    for verb in ("passed", "completed", "failed", "terminated", "initialized"):
        st = au.statement(verb, defined=False, result={"success": True, "duration": "PT1S"})
        _refused(au.send(st), "TN-DEFINED", 403)
    # Anything else about the AU (or under it) is still a cmi5-allowed statement.
    sub = au.statement(
        "experienced", defined=False, object={"objectType": "Activity", "id": au.activity_id + "/page/1"}
    )
    assert au.send(sub).status_code == 200


def test_review_1_au_traffic_carries_its_own_credential(client, lrs, release, student, monkeypatch):
    out = _launch(client, student, release)
    au = AU(client, out["url"], student).start()
    first = au.statement("initialized")
    assert au.send(first).status_code == 200
    (launched,) = lrs.by_verb("http://adlnet.gov/expapi/verbs/launched")
    assert lrs.authority[launched["id"]] == "server"  # TrueNorth's own statement: LRS_AUTH
    assert lrs.authority[first["id"]] == AU_CRED  # the AU's: CMI5_LRS_AUTH, a different authority
    # Without a credential of its own (or with the server's), cmi5 does not launch at all.
    for value in ("", SERVER_CRED):
        monkeypatch.setenv("CMI5_LRS_AUTH", value)
        with acting_as(student):
            r = client.post(f"/cmi5/releases/{release['id']}/aus/1/launch", json={})
        assert r.status_code == 503 and "CMI5_LRS_AUTH" in r.json()["detail"]


def test_review_2_passed_needs_a_score_and_the_server_marked_one(client, lrs, release, student, db_session):
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    no_score = au.statement(
        "passed", moveon=True, result={"success": True, "duration": "PT1S"}, ext={EXT + "masteryscore": 0.7}
    )
    _refused(au.send(no_score), "TN-SCORE")
    claimed = au.statement(
        "passed",
        moveon=True,
        ext={EXT + "masteryscore": 0.7},
        result={"success": True, "score": {"scaled": 1.0}, "duration": "PT1S"},
    )
    _refused(au.send(claimed), "TN-GRADE", 403)  # nothing marked yet
    au.mark(0.6)
    _refused(au.send(claimed), "TN-GRADE", 403)  # marked 0.6, claims 1.0
    assert au.scored(1.0).status_code == 200  # marked 1.0, reports 1.0


def test_review_2_a_mark_from_before_the_launch_does_not_count(client, lrs, release, student):
    from _cmi5_kit import mark_quiz

    mark_quiz(client, student, release["id"], 0, 1.0)
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    _refused(au.scored(1.0, marked=False), "TN-GRADE", 403)


def test_review_2_marking_is_attempt_limited_and_says_only_the_total(client, lrs, release, student, monkeypatch):
    from _cmi5_kit import mark_quiz

    monkeypatch.setenv("CMI5_GRADE_ATTEMPTS", "2")
    first = mark_quiz(client, student, release["id"], 0, 0.4)
    assert set(first.json()) == {"correct", "total", "scaled"}  # no per-question result
    mark_quiz(client, student, release["id"], 0, 0.4)
    with acting_as(student):
        third = client.post(f"/cmi5/releases/{release['id']}/aus/0/grade", json={"answers": {}})
        other_au = client.post(f"/cmi5/releases/{release['id']}/aus/1/grade", json={"answers": {}})
    assert third.status_code == 429
    assert other_au.status_code == 200  # per AU


def test_review_3_content_and_marking_need_a_published_course_and_an_enrolment(
    client, lrs, release, student, db_session
):
    from app.models import Course

    stranger = _user(db_session)
    with acting_as(stranger):
        for r in (
            client.get(f"/cmi5/releases/{release['id']}/aus/0/content"),
            client.post(f"/cmi5/releases/{release['id']}/aus/0/grade", json={"answers": {}}),
        ):
            assert r.status_code == 403, r.text
    db_session.get(Course, uuid.UUID(release["course_id"])).is_published = False
    db_session.commit()
    with acting_as(student):
        for r in (
            client.get(f"/cmi5/releases/{release['id']}/aus/0/content"),
            client.post(f"/cmi5/releases/{release['id']}/aus/0/grade", json={"answers": {}}),
            client.get(f"/cmi5/releases/{release['id']}/structure"),
            client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={}),
        ):
            assert r.status_code == 404, r.text
    author = _user(db_session, UserRole.instructor)
    with acting_as(author):
        assert client.get(f"/cmi5/releases/{release['id']}/aus/0/content").status_code == 200


def test_review_4_lms_ids_are_keyed_reserved_and_never_taken_by_the_au(client, lrs, release, student, monkeypatch):
    first = lms.lms_statement_id("s", "launched")
    assert uuid.UUID(first).version == 8 and lms.lms_statement_id("s", "launched") == first  # stable for a retry
    monkeypatch.setenv("TN_SECRETS_KEY", "another-installation-key-0123456789abcdef")
    assert lms.lms_statement_id("s", "launched") != first  # unguessable without the server's key
    au = _started(client, student, release)
    squat = au.statement("initialized", id=lms.lms_statement_id(au.registration, "satisfied", "x"))
    _refused(au.send(squat), "TN-ID", 403)


def test_review_4_a_conflicting_statement_under_our_id_is_not_taken_as_recorded(client, lrs, release, student):
    out = _launch(client, student, release)
    au = AU(client, out["url"], student).start()
    assert au.initialized().status_code == 200
    planted = lms.lms_statement_id(out["session_id"], "abandoned")
    lrs.statements[planted] = {
        "id": planted,
        "actor": au.actor,
        "verb": {"id": ADL + "experienced"},
        "object": {"id": "x"},
    }
    lrs.order.append(planted)
    with acting_as(student):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/0/launch", json={})
    assert r.status_code == 502 and "taken" in r.json()["detail"]


def test_review_4_a_retried_lms_statement_is_recognised(client, lrs, release, student):
    out = _launch(client, student, release)
    AU(client, out["url"], student).start().initialized()
    sid = lms.lms_statement_id(out["session_id"], "abandoned")
    stored = {
        "id": sid,
        "actor": xapi.actor(student.id),
        "verb": {"id": ABANDONED},
        "object": {
            "id": json.loads(json.dumps(lrs.by_verb("http://adlnet.gov/expapi/verbs/launched")[0]["object"]))["id"]
        },
        "timestamp": "2026-10-09T00:00:00.000Z",
    }
    lrs.statements[sid] = stored
    lrs.order.append(sid)
    assert _launch(client, student, release)["launch_mode"] == "Normal"  # same verb/actor/object: recorded


def test_review_5_profiles_and_state_cannot_be_rewritten_wholesale(client, lrs, release, student):
    au = _started(client, student, release)
    profile = {"activityId": au.activity_id, "profileId": "p"}
    for method in ("PUT", "POST", "DELETE"):
        assert (
            au.lrs(
                method, "activities/profile", params=profile, json_body={} if method != "DELETE" else None
            ).status_code
            == 403
        )
    assert au.lrs("GET", "activities/profile", params=profile).status_code == 404  # reading is fine
    no_id = {k: v for k, v in au.state_params("x").items() if k != "stateId"}
    assert au.lrs("DELETE", "activities/state", params=no_id).status_code == 403
    no_reg = {k: v for k, v in au.state_params("au.status").items() if k != "registration"}
    _refused(au.lrs("GET", "activities/state", params=no_reg), "8.1.4.0-3", 403)
    _refused(au.lrs("DELETE", "activities/state", params=au.state_params("LMS.LaunchData")), "10.2.1.0-5", 403)


def test_review_8_a_registration_stays_on_its_release(client, lrs, release, student, db_session):
    from types import SimpleNamespace

    from app.models import Enrollment

    _launch(client, student, release)
    enrolled = db_session.query(Enrollment).filter(Enrollment.user_id == student.id).one()
    other = SimpleNamespace(id=uuid.uuid4(), tenant_id=DEV_TENANT)
    with pytest.raises(lms.Cmi5Error) as exc:
        lms.registration(db_session, enrolled, other)
    assert exc.value.status == 409


# -- adversarial review 2 of 2026-10-10: one regression per finding ------------------------
def test_review2_1_uncategorised_statements_carry_no_judgement_and_no_lookalike_verbs(client, lrs, release, student):
    """Repro: `experienced` with result.success/score went to the LRS as an uncategorised
    statement a reader could take for a pass; `HTTPS://adlnet.gov/expapi/verbs/Passed/`
    slipped past the exact-match verb rules."""
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    for result in ({"success": True}, {"completion": True}, {"score": {"scaled": 1.0}}):
        st = au.statement("experienced", defined=False, result=result)
        _refused(au.send(st), "TN-RESULT", 403)
        assert st["id"] not in lrs.statements
    for spelled in (
        "HTTPS://adlnet.gov/expapi/verbs/Passed/",
        "https://adlnet.gov/expapi/verbs/passed",
        "http://adlnet.gov/expapi/verbs/completed/",
        "http://ADLNET.gov/expapi/verbs/terminated",
        "http://w3id.org/xapi/adl/verbs/satisfied",
        "https://w3id.org/xapi/adl/verbs/Waived",
    ):
        for defined in (False, True):
            _refused(au.send(au.statement(spelled, defined=defined)), "TN-VERB", 403)
    # A plain verb with only a duration or a response is still a cmi5-allowed statement.
    ok = au.statement("experienced", defined=False, result={"duration": "PT5S", "response": "read"})
    assert au.send(ok).status_code == 200


def test_review2_2_context_activities_stay_in_the_au(client, lrs, release, student):
    """Repro: grouping/parent/other naming another TrueNorth activity (a quiz, another AU)
    attached the AU's statement to it."""
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    elsewhere = (_quiz_iri(), au.activity_id.rsplit("/", 1)[0] + "/mod_002", au.activity_id + "x")
    for key in ("parent", "grouping", "other"):
        for foreign in elsewhere:
            st = au.statement("experienced", defined=False)
            st["context"]["contextActivities"].setdefault(key, []).append({"id": foreign})
            _refused(au.send(st), "TN-SCOPE", 403)
    # The AU's own sub-activities, and activities that are not TrueNorth's, are fine.
    st = au.statement("experienced", defined=False)
    acts = st["context"]["contextActivities"]
    acts.setdefault("grouping", []).append({"id": au.activity_id + "/page/2"})
    acts["other"] = [{"id": "https://example.org/glossary/term"}]
    assert au.send(st).status_code == 200, st


def _raw(au, statement: dict, token: str):
    """POST a statement whose "__N__" placeholder is the raw JSON token (json.dumps would
    never write NaN or 1e999 itself)."""
    body = json.dumps(statement).replace('"__N__"', token)
    return au.client.post(
        f"{au.endpoint}statements", content=body, headers=au.headers(**{"Content-Type": "application/json"})
    )


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999", "1" + "0" * 400])
def test_review2_3_non_finite_numbers_are_refused(client, lrs, release, student, number):
    """Repro: score.scaled NaN passed the server-mark comparison (every comparison with NaN
    is false), so a `passed` with no real score was accepted."""
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    au.mark(1.0)
    st = au.statement(
        "passed",
        moveon=True,
        result={"success": True, "score": {"scaled": "__N__"}, "duration": "PT1S"},
        ext={EXT + "masteryscore": 0.7},
    )
    _refused(_raw(au, st, number), "TN-NUMBER")
    assert lrs.by_verb(ADL + "passed") == []
    # ... anywhere in the statement, not only the score
    other = au.statement("experienced", defined=False, ext={"https://example.org/x": "__N__"})
    _refused(_raw(au, other, number), "TN-NUMBER")


def test_review2_4_marking_locks_the_enrolment_before_counting(client, lrs, release, student, db_session):
    """Repro: two concurrent /grade requests both counted N-1 marks and both recorded one.
    The handler now writes the enrolment row (a row lock on PostgreSQL, the write lock on
    SQLite) before it counts."""
    from sqlalchemy import event

    engine = db_session.get_bind()
    seen: list[str] = []

    def capture(conn, cursor, statement, params, context, executemany):
        text = " ".join(statement.split()).upper()
        if "ENROLLMENTS" in text or "CMI5_GRADES" in text:
            seen.append(text)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with acting_as(student):
            r = client.post(f"/cmi5/releases/{release['id']}/aus/0/grade", json={"answers": {}})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert r.status_code == 200, r.text
    lock = next(i for i, s in enumerate(seen) if s.startswith("UPDATE ENROLLMENTS"))
    count = next(i for i, s in enumerate(seen) if s.startswith("SELECT") and "CMI5_GRADES" in s)
    assert lock < count, seen


@pytest.mark.parametrize("suffix", ["/../mod_002", "/./x", "/%2e%2e/x", "/%2E%2E/mod_002", "//x", "/x/", "/a/../../b"])
def test_review2_5_object_ids_are_plain_paths_under_the_au(client, lrs, release, student, suffix):
    """Repro: `<au>/../mod_002` passed the prefix check and resolves to another AU."""
    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    st = au.statement("experienced", defined=False, object={"objectType": "Activity", "id": au.activity_id + suffix})
    _refused(au.send(st), "TN-SCOPE", 403)


def test_review2_6a_agent_profiles_are_read_only(client, lrs, release, student):
    au = _started(client, student, release)
    for profile_id in ("cmi5LearnerPreferences", "anything"):
        params = {"agent": json.dumps(au.actor), "profileId": profile_id}
        for method in ("PUT", "POST", "DELETE"):
            r = au.lrs(method, "agents/profile", params=params, json_body=None if method == "DELETE" else {"a": 1})
            assert r.status_code == 403, (profile_id, method, r.text)
    assert not any(k[0] == "agent" for k in lrs.docs)
    prefs = {"agent": json.dumps(au.actor), "profileId": "cmi5LearnerPreferences"}
    assert au.lrs("GET", "agents/profile", params=prefs).status_code in (200, 404)


def test_review2_6b_the_au_cannot_set_authority_or_stored(client, lrs, release, student):
    au = _started(client, student, release)
    st = au.statement(
        "initialized",
        authority={"objectType": "Agent", "account": {"homePage": "https://lms.example", "name": "admin"}},
        stored="2020-01-01T00:00:00.000Z",
    )
    assert au.send(st).status_code == 200
    kept = lrs.statements[st["id"]]
    assert "authority" not in kept and "stored" not in kept
    assert kept["verb"]["id"] == ADL + "initialized"


def test_review2_6c_au_traffic_stops_when_the_course_or_the_enrolment_does(client, lrs, release, student, db_session):
    from types import SimpleNamespace

    from app.course_releases.models import CourseRelease
    from app.models import Course, Enrollment, EnrollmentStatus

    au = _started(client, student, release)
    assert au.initialized().status_code == 200
    course = db_session.get(Course, uuid.UUID(release["course_id"]))
    course.is_published = False
    db_session.commit()
    assert au.lrs("GET", "about").status_code == 403
    assert au.send(au.statement("experienced", defined=False)).status_code == 403
    course.is_published = True
    db_session.commit()
    assert au.lrs("GET", "about").status_code == 200

    enrolled = db_session.query(Enrollment).filter(Enrollment.user_id == student.id).one()
    enrolled.status = EnrollmentStatus.withdrawn
    db_session.commit()
    assert au.send(au.statement("experienced", defined=False)).status_code == 403
    assert au.lrs("GET", "activities/state", params=au.state_params("LMS.LaunchData")).status_code == 403

    # A course author previews a draft course, as launching allows; their AU keeps working.
    author = _user(db_session, UserRole.instructor)
    ensure_enrollment(db_session, user_id=author.id, course_id=course.id, tenant_id=DEV_TENANT)
    course.is_published = False
    db_session.commit()
    preview = _started(client, author, release)
    assert preview.initialized().status_code == 200
    assert au.lrs("GET", "about").status_code == 403  # the withdrawn Student still is not

    # A registration whose enrolment no longer exists is refused the same way.
    rel = db_session.get(CourseRelease, uuid.UUID(release["id"]))
    with pytest.raises(lms.Cmi5Error) as exc:
        lms._still_entitled(db_session, SimpleNamespace(enrollment_id=uuid.uuid4()), rel)
    assert exc.value.status == 403
