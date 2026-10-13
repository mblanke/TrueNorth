"""POST /admin/fixture-courses/{code}/withdraw: retire C105 on a host that published it
before #147 (app/course_publishing/withdraw.py), against the in-memory Moodle.

The dry run (the default) lists the course, its quizzes, enrolments, attempts, grades and
Moodle publications and changes nothing. Applying puts the course and its quizzes back to
draft and hides the Moodle course through set_visible(False); every attempt, grade and
enrolment stays; a second run changes nothing; only a platform administrator may run it,
and only the named tenant's course is touched.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app import models as m
from app.auth import CurrentUser, get_current_user
from app.course_publishing.models import FAILED, PUBLISHED, REQUESTED, STAGING, CoursePublication
from app.course_releases.models import ACCEPTED, CourseRelease, CourseReleaseBlob
from app.main import app as fastapi_app
from app.moodle_backends import FakeMoodle, fake

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
URL = "/admin/fixture-courses/C105/withdraw"


@contextmanager
def acting_as(role: m.UserRole, tenant: str = DEV_TENANT):
    # The seeded dev principal's id (tests/conftest.py): the audit log's user_id is a foreign key.
    who = CurrentUser(id=DEV_TENANT, email=f"{role.value}@example.test", display_name=role.value, role=role,
                      tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", DEV_TENANT)
    fake.reset()
    yield
    fake.reset()


def _tenant(db, tid: str) -> uuid.UUID:
    tenant_id = uuid.UUID(tid)
    if db.get(m.Tenant, tenant_id) is None:
        db.add(m.Tenant(id=tenant_id, name=f"t-{tid[-4:]}", slug=f"t-{tid[-4:]}"))
        db.flush()
    return tenant_id


def _course(db, tid: str = DEV_TENANT, code: str = "C105", *, published: bool = True) -> dict:
    """A course as the shipped-content import leaves it, then published: two modules, a quiz
    on each, one Student enrolled with a grade, a submitted attempt and module progress."""
    tenant_id = _tenant(db, tid)
    student = m.User(keycloak_id=f"kc-{uuid.uuid4().hex}", email=f"s-{uuid.uuid4().hex[:8]}@example.test",
                     display_name="Student", role=m.UserRole.student, tenant_id=tenant_id)
    course = m.Course(name=f"{code} — Foundations of Cybersecurity", tenant_id=tenant_id, is_published=published)
    db.add_all([student, course])
    db.flush()
    mods = [m.CourseModule(course_id=course.id, ordinal=i, title=f"Module {i}",
                           content_type=m.ModuleContentType.reading) for i in (1, 2)]
    db.add_all(mods)
    db.flush()
    quizzes = [m.Quiz(title=f"{code} module {i} quiz", module_id=mod.id, tenant_id=tenant_id, is_published=published)
               for i, mod in enumerate(mods, start=1)]
    db.add_all(quizzes)
    db.flush()
    enrol = m.Enrollment(user_id=student.id, course_id=course.id, tenant_id=tenant_id,
                         status=m.EnrollmentStatus.in_progress, final_grade="B", final_score=80, max_score=100)
    db.add(enrol)
    db.flush()
    progress = m.ModuleProgress(enrollment_id=enrol.id, module_id=mods[0].id,
                                status=m.ModuleProgressStatus.completed, score=80, attempts=1)
    attempt = m.QuizAttempt(quiz_id=quizzes[0].id, user_id=student.id, tenant_id=tenant_id, score=8, max_score=10,
                            passed=True, submitted_at=datetime.now(UTC), answers='{"q1": [0]}')
    db.add_all([progress, attempt])
    db.commit()
    return {"course": course, "quizzes": quizzes, "enrol": enrol, "attempt": attempt, "progress": progress,
            "tenant_id": tenant_id}


def _moodle(db, rec: dict, *, state: str = PUBLISHED, visible: bool = True, lease: timedelta | None = None):
    """The course's release published to a (fake) Moodle: the live course exists there."""
    course = rec["course"]
    blob = CourseReleaseBlob(sha256=uuid.uuid4().hex * 2, size=1, data=b"x")
    db.add(blob)
    db.flush()
    release = CourseRelease(
        tenant_id=rec["tenant_id"], course_id=course.id, catalogue_code="C105", arc2_code="ARC2-C105", run_id="r1",
        slug="c105", title="C105", version=1, release_digest=uuid.uuid4().hex, learner_digest="l", platform_digest="p",
        instructor_digest="i", blob_sha256=blob.sha256, meta="{}", state=ACCEPTED,
    )
    platform = m.ExternalPlatform(
        name="Staging Moodle", slug=f"moodle-{uuid.uuid4().hex[:6]}", platform_type="fake",
        base_url=f"http://moodle-{uuid.uuid4().hex[:6]}.invalid", auth_type=m.IntegrationAuthType.lti13,
        tenant_id=rec["tenant_id"],
    )
    platform.lti_issuer = platform.base_url
    db.add_all([release, platform])
    db.flush()
    pub = CoursePublication(
        tenant_id=rec["tenant_id"], release_id=release.id, course_id=course.id, platform_id=platform.id, state=state,
        stage_idnumber=f"tn-stage:{release.id}", live_idnumber=str(course.id),
        lease_until=(datetime.now(UTC) + lease) if lease else None, lease_holder="other" if lease else None,
    )
    db.add(pub)
    db.commit()
    FakeMoodle().upsert_course(platform, {"idnumber": str(course.id), "fullname": course.name, "visible": visible,
                                          "sections": [{"name": "Module 1", "activities": []}]})
    fake._CALLS.clear()  # only the operation's own calls are asserted
    return platform, pub


def _audits(db) -> list[m.AuditLog]:
    return db.query(m.AuditLog).filter(m.AuditLog.action == "withdraw_fixture_course").all()


def _assert_history_kept(db, rec: dict) -> None:
    for row in (rec["enrol"], rec["attempt"], rec["progress"]):
        db.refresh(row)
    assert (rec["enrol"].final_grade, rec["enrol"].final_score, rec["enrol"].status) == (
        "B", 80, m.EnrollmentStatus.in_progress)
    assert (rec["attempt"].score, rec["attempt"].passed, rec["attempt"].answers) == (8, True, '{"q1": [0]}')
    assert rec["progress"].status == m.ModuleProgressStatus.completed
    assert db.query(m.QuizAttempt).filter(m.QuizAttempt.quiz_id == rec["quizzes"][0].id).count() == 1


def test_the_dry_run_lists_the_records_and_changes_nothing(client, db_session):
    rec = _course(db_session)
    platform, pub = _moodle(db_session, rec)
    with acting_as(m.UserRole.admin):
        r = client.post(URL)  # dry_run defaults to true
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["changed"] is False and body["errors"] == []
    [c] = body["courses"]
    assert c["id"] == str(rec["course"].id) and c["is_published"] is True
    assert sorted(q["id"] for q in c["quizzes"]) == sorted(str(q.id) for q in rec["quizzes"])
    assert {q["title"]: (q["attempts"], q["submitted_attempts"]) for q in c["quizzes"]} == {
        "C105 module 1 quiz": (1, 1), "C105 module 2 quiz": (0, 0)}
    assert c["enrolments_total"] == 1 and c["enrolments_graded"] == 1
    assert c["enrolments"][0]["final_grade"] == "B" and c["enrolments"][0]["id"] == str(rec["enrol"].id)
    assert (c["module_progress"], c["quiz_attempts"]) == (1, 1)
    [md] = c["moodle"]
    assert md["platform_id"] == str(platform.id) and md["visibility"] == "visible"
    assert md["publications"] == [str(pub.id)] and md["live_idnumber"] == str(rec["course"].id)
    assert len(body["changes"]) == 4  # course, two quizzes, the Moodle course
    assert any("visible -> hidden" in line for line in body["changes"])
    assert body["preserved"] == {"enrolments": 1, "graded_enrolments": 1, "module_progress": 1,
                                 "quiz_attempts": 1, "moodle_results": 0}
    # Nothing changed anywhere.
    db_session.refresh(rec["course"])
    assert rec["course"].is_published is True
    assert all(db_session.get(m.Quiz, q.id).is_published for q in rec["quizzes"])
    assert fake.site(platform)[str(rec["course"].id)]["visible"] == 1
    assert [op for op, _ in fake.calls()] == ["describe_course"]
    assert _audits(db_session) == []


def test_apply_unpublishes_hides_in_moodle_keeps_history_and_audits(client, db_session):
    rec = _course(db_session)
    platform, _pub = _moodle(db_session, rec)
    with acting_as(m.UserRole.admin):
        r = client.post(URL + "?dry_run=false")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False and body["changed"] is True and body["errors"] == []
    assert len(body["changes"]) == 4
    [c] = body["courses"]
    assert c["is_published"] is False and not any(q["is_published"] for q in c["quizzes"])
    assert c["moodle"][0]["visibility"] == "hidden"  # re-checked in Moodle after hiding
    db_session.expire_all()
    assert db_session.get(m.Course, rec["course"].id).is_published is False
    assert not any(db_session.get(m.Quiz, q.id).is_published for q in rec["quizzes"])
    assert ("set_visible", str(rec["course"].id)) in fake.calls()
    live = fake.site(platform)[str(rec["course"].id)]
    assert live["visible"] == 0  # hidden, not deleted
    _assert_history_kept(db_session, rec)
    audits = sorted(_audits(db_session), key=lambda a: json.loads(a.detail)["system"])
    assert [json.loads(a.detail)["system"] for a in audits] == ["moodle", "truenorth"]
    assert all(a.resource_id == str(rec["course"].id) and str(a.tenant_id) == DEV_TENANT for a in audits)
    assert len(json.loads(audits[1].detail)["changes"]) == 3  # course + two quizzes


def test_a_second_run_changes_nothing_and_says_so(client, db_session):
    rec = _course(db_session)
    _moodle(db_session, rec)
    with acting_as(m.UserRole.admin):
        assert client.post(URL + "?dry_run=false").json()["changed"] is True
        fake._CALLS.clear()
        again = client.post(URL + "?dry_run=false")
    assert again.status_code == 200
    body = again.json()
    assert body["changed"] is False and body["changes"] == [] and body["errors"] == []
    assert "set_visible" not in [op for op, _ in fake.calls()]
    assert len(_audits(db_session)) == 2  # the first run's only
    _assert_history_kept(db_session, rec)


def test_without_a_moodle_publication_only_truenorth_changes(client, db_session):
    rec = _course(db_session)
    with acting_as(m.UserRole.admin):
        body = client.post(URL + "?dry_run=false").json()
    assert body["changed"] is True and body["errors"] == []
    assert body["courses"][0]["moodle"] == [] and len(body["changes"]) == 3
    assert fake.calls() == []
    _assert_history_kept(db_session, rec)


def test_an_already_draft_course_is_nothing_to_do(client, db_session):
    _course(db_session, published=False)
    with acting_as(m.UserRole.admin):
        preview = client.post(URL).json()
        body = client.post(URL + "?dry_run=false").json()
    assert preview["changes"] == [] and body["changed"] is False and _audits(db_session) == []


@pytest.mark.parametrize("role", [m.UserRole.instructor, m.UserRole.range_ops, m.UserRole.student])
def test_only_a_platform_administrator_may_run_it(client, db_session, role):
    rec = _course(db_session)
    with acting_as(role):
        assert client.post(URL).status_code == 403
        assert client.post(URL + "?dry_run=false").status_code == 403
    db_session.refresh(rec["course"])
    assert rec["course"].is_published is True


def test_another_tenants_admin_is_not_the_platform_administrator(client, db_session):
    rec = _course(db_session)
    _tenant(db_session, OTHER_TENANT)
    with acting_as(m.UserRole.admin, tenant=OTHER_TENANT):
        assert client.post(URL + f"?dry_run=false&tenant_id={DEV_TENANT}").status_code == 403
    db_session.refresh(rec["course"])
    assert rec["course"].is_published is True


def test_only_the_named_tenants_course_is_touched(client, db_session):
    ours = _course(db_session)
    theirs = _course(db_session, OTHER_TENANT)
    their_platform, _ = _moodle(db_session, theirs)
    with acting_as(m.UserRole.admin):
        body = client.post(URL + "?dry_run=false").json()  # default: the caller's tenant
    assert [c["id"] for c in body["courses"]] == [str(ours["course"].id)]
    db_session.expire_all()
    assert db_session.get(m.Course, theirs["course"].id).is_published is True
    assert all(db_session.get(m.Quiz, q.id).is_published for q in theirs["quizzes"])
    assert fake.site(their_platform)[str(theirs["course"].id)]["visible"] == 1
    # Named explicitly, the other tenant's is withdrawn, and its audit rows are that tenant's.
    with acting_as(m.UserRole.admin):
        body = client.post(URL + f"?dry_run=false&tenant_id={OTHER_TENANT}").json()
    assert [c["id"] for c in body["courses"]] == [str(theirs["course"].id)] and body["errors"] == []
    assert fake.site(their_platform)[str(theirs["course"].id)]["visible"] == 0
    assert {str(a.tenant_id) for a in _audits(db_session)} == {DEV_TENANT, OTHER_TENANT}


def test_an_unknown_tenant_is_404_and_a_real_course_is_not_a_fixture(client, db_session):
    other = _course(db_session, code="C101")
    with acting_as(m.UserRole.admin):
        assert client.post(URL + f"?tenant_id={uuid.uuid4()}").status_code == 404
        r = client.post("/admin/fixture-courses/C101/withdraw?dry_run=false")
    assert r.status_code == 422 and "not a fixture course" in r.json()["detail"]
    db_session.refresh(other["course"])
    assert other["course"].is_published is True


def test_a_running_publication_blocks_apply_and_a_stalled_one_is_stopped(client, db_session):
    rec = _course(db_session)
    _platform, pub = _moodle(db_session, rec, state=STAGING, lease=timedelta(minutes=10))
    with acting_as(m.UserRole.admin):
        preview = client.post(URL).json()
        refused = client.post(URL + "?dry_run=false")
    assert preview["courses"][0]["running_publications"] == [str(pub.id)] and preview["errors"]
    assert refused.status_code == 409 and "running now" in refused.json()["detail"]
    db_session.refresh(rec["course"])
    assert rec["course"].is_published is True  # refused before anything changed

    # The job never started, or its process died (lease lapsed): stop it, so the startup
    # resume never runs it and shows the course again.
    pub.state, pub.lease_until, pub.lease_holder = REQUESTED, None, None
    db_session.commit()
    with acting_as(m.UserRole.admin):
        body = client.post(URL + "?dry_run=false").json()
    assert body["errors"] == [], body
    db_session.refresh(pub)
    assert pub.state == FAILED and "withdrawn" in pub.error and pub.lease_until is None
    assert any(f"publication {pub.id}: requested -> failed" in line for line in body["changes"])


def test_an_unreachable_moodle_is_reported_and_a_rerun_finishes(client, db_session):
    rec = _course(db_session)
    platform, _ = _moodle(db_session, rec)
    fake.fail_next("set_visible")
    with acting_as(m.UserRole.admin):
        first = client.post(URL + "?dry_run=false").json()
    assert first["changed"] is True  # TrueNorth is done
    assert len(first["errors"]) == 1 and "hiding failed" in first["errors"][0]
    assert fake.site(platform)[str(rec["course"].id)]["visible"] == 1
    with acting_as(m.UserRole.admin):
        second = client.post(URL + "?dry_run=false").json()
    assert second["errors"] == [] and second["changes"] == [
        f"moodle Staging Moodle ({platform.id}) course {rec['course'].id}: visible -> hidden"]
    assert fake.site(platform)[str(rec["course"].id)]["visible"] == 0


def _script():
    import importlib.util
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("withdraw_fixture_course", scripts / "withdraw_fixture_course.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from _inprocess_api import InProcessApi

    return mod, InProcessApi


def test_the_script_previews_then_applies_then_reports_nothing_to_do(client, db_session, capsys):
    rec = _course(db_session)
    _moodle(db_session, rec)
    script, in_process_api = _script()
    api = in_process_api(client)
    with acting_as(m.UserRole.admin):
        assert script.run(api, "C105", apply=False, tenant=None, as_json=False) == 0
        preview = capsys.readouterr().out
        assert script.run(api, "C105", apply=True, tenant=None, as_json=False) == 0
        applied = capsys.readouterr().out
        assert script.run(api, "C105", apply=True, tenant=None, as_json=False) == 0
        again = capsys.readouterr().out
        assert script.run(api, "C101", apply=False, tenant=None, as_json=False) == 2
    assert "DRY RUN: nothing changed" in preview and "would change: 4" in preview and "--apply" in preview
    assert "moodle Staging Moodle course" in preview and ": visible" in preview
    assert "APPLIED" in applied and "changed: 4" in applied and ": hidden" in applied
    assert "nothing to change" in again and "changed: 0" in again
    assert "kept as they are: enrolments 1, graded_enrolments 1, module_progress 1, quiz_attempts 1" in again
    db_session.refresh(rec["course"])
    assert rec["course"].is_published is False
