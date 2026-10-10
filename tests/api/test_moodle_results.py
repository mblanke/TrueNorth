"""Moodle -> TrueNorth results: completions and quiz grades land on enrolments, module
progress and quiz attempts, idempotently, and only for what is the platform's own business.

The recording rules run against the in-memory Moodle (app.moodle_backends.fake). The trust
rules of the real backend (app.moodle_backends.local_truenorth) run against a mock
transport playing the plugin: the answer must be signed by the Moodle's registered LTI
key, for this tenant, answering this very request.

Threat cases written down here:
- cross-tenant: a Moodle names a user of another tenant, or a course never published to it;
- a Moodle "enrolling" someone TrueNorth never enrolled, or reopening a withdrawn enrolment;
- an unsigned answer, one signed by another key, one for another tenant, a replayed one
  (answering an earlier request), and a Moodle with no registered key set;
- a Student (or another tenant's admin) asking for a pull.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import jwt
import pytest
from _release_kit import build
from _shared import real_tenant
from app import lti13
from app.course_releases.models import CourseRelease
from app.enrollment import ensure_enrollment
from app.models import (
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    ExternalPlatform,
    IntegrationAuthType,
    ModuleProgress,
    ModuleProgressStatus,
    Quiz,
    QuizAttempt,
    User,
    UserRole,
)
from app.moodle_backends import LocalTrueNorthMoodle, MoodleError, fake
from app.moodle_results import service
from app.moodle_results.models import MoodleResultCursor
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_course_publications import DEV_TENANT, accepted, live, publish, ready  # noqa: F401
from test_course_releases import acting_as


@pytest.fixture(autouse=True)
def _clean_fake():
    fake.reset()
    yield
    fake.reset()


def _student(db, tenant=DEV_TENANT) -> User:
    uid = uuid.uuid4()
    user = User(id=uid, keycloak_id=f"kc-{uid}", email=f"{uid.hex[:10]}@example.test", display_name="Student",
                role=UserRole.student, tenant_id=uuid.UUID(str(tenant)))
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def course(ready, tmp_path, db_session):  # noqa: F811 - the imported fixture
    """A release published to the fake Moodle, and an enrolled Student."""
    client, platform = ready
    rid = accepted(client, build(tmp_path))
    assert publish(client, rid, platform).json()["state"] == "published"
    course_id = db_session.get(CourseRelease, uuid.UUID(rid)).course_id
    acts = live(platform, db_session, rid)["activities"]
    student = _student(db_session)
    enrollment = ensure_enrollment(db_session, user_id=student.id, course_id=course_id, tenant_id=DEV_TENANT)
    db_session.commit()

    def first(kind: str, module: str = "mod_001") -> str:
        return next(k for k, a in acts.items() if a["type"] == kind and k.startswith(f"tn:{module}:"))

    class C:
        pass

    c = C()
    c.client, c.platform, c.id, c.student, c.enrollment, c.acts, c.first = (
        client, platform, course_id, student, enrollment, acts, first
    )
    return c


def _grade(c, grade=100.0, module="mod_001", user=None, course=None, **extra):
    fake.record_result(c.platform, kind="quiz_grade", user=str(user or c.student.id), course=str(course or c.id),
                       activity=c.first("quiz", module), modname="quiz", grade=grade, grademax=100.0,
                       gradepass=70.0, attempts=1, **extra)


def _view(c, module="mod_001"):
    fake.record_result(c.platform, kind="completion", user=str(c.student.id), course=str(c.id),
                       activity=c.first("page", module), modname="page", state=1)


def _pull(c, **body):
    return c.client.post(f"/integrations/platforms/{c.platform.id}/moodle-results/pull", json=body)


def _module(db, c, ordinal=1) -> CourseModule:
    return db.query(CourseModule).filter_by(course_id=c.id, ordinal=ordinal).one()


def _progress(db, c, ordinal=1) -> ModuleProgress:
    return db.query(ModuleProgress).filter_by(enrollment_id=c.enrollment.id, module_id=_module(db, c, ordinal).id).one()


def _attempts(db, c, ordinal=1) -> list[QuizAttempt]:
    quizzes = [q.id for q in db.query(Quiz).filter_by(module_id=_module(db, c, ordinal).id)]
    return db.query(QuizAttempt).filter(QuizAttempt.user_id == c.student.id, QuizAttempt.quiz_id.in_(quizzes)).all()


class TestRecording:
    def test_a_passed_quiz_and_a_viewed_page_reach_the_students_record(self, course, db_session):
        _view(course)
        _grade(course)
        resp = _pull(course)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["rows"], body["applied"], body["skipped"], body["more"]) == (2, 2, {}, False)
        progress = _progress(db_session, course)
        assert progress.status == ModuleProgressStatus.completed and progress.score == 100
        [attempt] = _attempts(db_session, course)
        assert attempt.passed is True and attempt.score == 100 and attempt.submitted_at is not None
        db_session.refresh(course.enrollment)
        assert course.enrollment.status == EnrollmentStatus.in_progress  # 1 of 6 modules
        status = course.client.get(f"/integrations/platforms/{course.platform.id}/moodle-results").json()
        assert status["cursor"] == "2" and status["rows_applied"] == 2 and status["last_error"] == ""

    def test_pulling_again_is_idempotent(self, course, db_session):
        _view(course)
        _grade(course)
        _pull(course)
        assert _pull(course).json()["rows"] == 0  # the cursor moved on
        again = _pull(course, reset=True).json()  # from the beginning: seen, nothing new
        assert (again["rows"], again["applied"], again["unchanged"]) == (2, 0, 2)
        assert len(_attempts(db_session, course)) == 1

    def test_a_changed_grade_updates_the_one_attempt_and_never_undoes_a_pass(self, course, db_session):
        _grade(course, grade=100)
        _pull(course)
        _grade(course, grade=40, time=1_900_000_000)  # a later, lower highest-grade (say, regraded)
        assert _pull(course).json()["applied"] == 1
        [attempt] = _attempts(db_session, course)
        assert attempt.score == 40 and attempt.passed is False
        progress = _progress(db_session, course)
        assert progress.status == ModuleProgressStatus.completed and progress.score == 100

    def test_a_failed_quiz_is_progress_not_completion(self, course, db_session):
        _grade(course, grade=50)
        _pull(course)
        progress = _progress(db_session, course)
        assert progress.status == ModuleProgressStatus.in_progress and progress.score == 50
        assert _attempts(db_session, course)[0].passed is False

    def test_completing_every_module_completes_the_enrolment(self, course, db_session):
        for ordinal in range(1, 7):
            _grade(course, module=f"mod_{ordinal:03d}")
        assert _pull(course).json()["applied"] == 6
        db_session.refresh(course.enrollment)
        assert course.enrollment.status == EnrollmentStatus.completed
        assert course.enrollment.final_grade == "A" and course.enrollment.completed_at is not None

    def test_moodle_course_completions_are_not_taken(self, course, db_session):
        """Moodle's cron writes timecompleted earlier than the row, which a time cursor
        steps over; TrueNorth completes an enrolment from its modules instead."""
        fake.record_result(course.platform, kind="course_completion", user=str(course.student.id), course=str(course.id))
        assert _pull(course).json()["skipped"] == {"unknown_kind": 1}
        db_session.refresh(course.enrollment)
        assert course.enrollment.status == EnrollmentStatus.enrolled

    def _complete_without_grade(self, course, module="mod_001"):
        _view(course, module)
        fake.record_result(course.platform, kind="completion", user=str(course.student.id), course=str(course.id),
                           activity=course.first("quiz", module), modname="quiz", state=2)

    def test_pages_alone_complete_a_module_once_every_activity_is_complete(self, course, db_session):
        _view(course)
        _pull(course)
        assert _progress(db_session, course).status == ModuleProgressStatus.in_progress  # the quiz is not done
        fake.record_result(course.platform, kind="completion", user=str(course.student.id), course=str(course.id),
                           activity=course.first("quiz"), modname="quiz", state=2)
        _pull(course)
        assert _progress(db_session, course).status == ModuleProgressStatus.completed

    def test_a_module_completed_without_a_grade_is_not_scored_and_not_a_fail(self, course, db_session):
        from app.qsp_progress import COMPLETED, _resolve_state

        self._complete_without_grade(course)
        _pull(course)
        progress = _progress(db_session, course)
        assert progress.status == ModuleProgressStatus.completed
        assert (progress.score, progress.max_score) == (0, 0)  # not scored, rather than 0/100
        assert _resolve_state(progress, course.enrollment, _module(db_session, course)) == COMPLETED
        _grade(course, grade=90)  # a grade arriving later scores it
        _pull(course)
        progress = _progress(db_session, course)
        assert (progress.score, progress.max_score) == (90, 100)

    def test_ungraded_modules_do_not_drag_the_course_grade(self, course, db_session):
        _grade(course, module="mod_001", grade=95)
        for ordinal in range(2, 7):
            self._complete_without_grade(course, f"mod_{ordinal:03d}")
        _pull(course)
        db_session.refresh(course.enrollment)
        assert course.enrollment.status == EnrollmentStatus.completed
        assert course.enrollment.final_grade == "A"  # 95/100, the readings neither help nor hurt
        assert (course.enrollment.final_score, course.enrollment.max_score) == (95, 100)

    def test_a_course_with_nothing_graded_completes_without_a_letter_grade(self, course, db_session):
        for ordinal in range(1, 7):
            self._complete_without_grade(course, f"mod_{ordinal:03d}")
        _pull(course)
        db_session.refresh(course.enrollment)
        assert course.enrollment.status == EnrollmentStatus.completed and course.enrollment.final_grade is None

    def test_a_mirrored_attempt_does_not_use_up_truenorth_attempts(self, course, db_session):
        from _shared import act_as
        from app.auth import CurrentUser

        _grade(course)
        _pull(course)
        quiz = db_session.query(Quiz).filter_by(module_id=_module(db_session, course).id).first()
        quiz.max_attempts, quiz.is_published = 1, True
        db_session.commit()
        s = course.student
        act_as(CurrentUser(id=str(s.id), email=s.email, display_name="S", role=UserRole.student,
                           tenant_id=str(s.tenant_id), keycloak_id=s.keycloak_id))
        first = course.client.post(f"/quizzes/{quiz.id}/attempts")
        assert first.status_code in (200, 201), first.text  # the Moodle attempt did not count
        assert course.client.post(f"/quizzes/{quiz.id}/attempts").status_code == 409  # this one did

    def test_pages_are_paged_and_the_cursor_persists_between_pages(self, course, db_session, monkeypatch):
        monkeypatch.setattr(service, "PAGE_SIZE", 1)
        _view(course)
        _grade(course)
        body = _pull(course).json()
        assert (body["pages"], body["rows"], body["cursor"], body["applied"]) == (2, 2, "2", 2)
        status = course.client.get(f"/integrations/platforms/{course.platform.id}/moodle-results").json()
        assert status["rows_seen"] == 2 and status["rows_applied"] == 2


class TestTwoCourses:
    """Release module ids are mod_NNN in every course, so activity idnumbers repeat across
    courses: every fact is keyed by its course too (review blocker, 2026-10-09)."""

    @pytest.fixture
    def two(self, course, tmp_path_factory, db_session):
        rid = accepted(course.client, build(tmp_path_factory.mktemp("b"), catalogue_code="C101", slug="arc2-b"))
        assert publish(course.client, rid, course.platform).json()["state"] == "published"
        other = db_session.get(CourseRelease, uuid.UUID(rid)).course_id
        assert other != course.id
        acts_b = live(course.platform, db_session, rid)["activities"]
        assert set(acts_b) & set(course.acts)  # the collision this guards against is real
        enrollment_b = ensure_enrollment(db_session, user_id=course.student.id, course_id=other, tenant_id=DEV_TENANT)
        db_session.commit()
        return other, enrollment_b

    def _progress_in(self, db, enrollment, course_id, ordinal=1):
        module = db.query(CourseModule).filter_by(course_id=course_id, ordinal=ordinal).one()
        return db.query(ModuleProgress).filter_by(enrollment_id=enrollment.id, module_id=module.id).one()

    def test_a_page_viewed_in_one_course_completes_nothing_in_the_other(self, course, two, db_session):
        other, enrollment_b = two
        fake.record_result(course.platform, kind="completion", user=str(course.student.id), course=str(course.id),
                           activity=course.first("page"), modname="page", state=1)
        fake.record_result(course.platform, kind="completion", user=str(course.student.id), course=str(course.id),
                           activity=course.first("quiz"), modname="quiz", state=2)
        assert _pull(course).json()["applied"] == 2
        assert _progress(db_session, course).status == ModuleProgressStatus.completed
        assert self._progress_in(db_session, enrollment_b, other).status == ModuleProgressStatus.not_started
        # The same activity idnumber in course B is its own fact, applied, not "unchanged".
        fake.record_result(course.platform, kind="completion", user=str(course.student.id), course=str(other),
                           activity=course.first("page"), modname="page", state=1)
        body = _pull(course).json()
        assert (body["applied"], body["unchanged"]) == (1, 0)
        assert self._progress_in(db_session, enrollment_b, other).status == ModuleProgressStatus.in_progress

    def test_the_same_quiz_idnumber_in_two_courses_keeps_two_grades(self, course, two, db_session):
        other, enrollment_b = two
        _grade(course, grade=100)
        _grade(course, grade=40, course=other)
        body = _pull(course).json()
        assert (body["applied"], body["unchanged"]) == (2, 0)
        assert _progress(db_session, course).score == 100
        assert self._progress_in(db_session, enrollment_b, other).score == 40
        assert db_session.query(QuizAttempt).filter_by(user_id=course.student.id).count() == 2

    def test_completing_one_course_does_not_complete_the_other(self, course, two, db_session):
        other, enrollment_b = two
        for ordinal in range(1, 7):
            _grade(course, module=f"mod_{ordinal:03d}")
        _pull(course)
        db_session.refresh(course.enrollment)
        db_session.refresh(enrollment_b)
        assert course.enrollment.status == EnrollmentStatus.completed
        assert enrollment_b.status == EnrollmentStatus.enrolled


class TestOnlyThePlatformsOwnBusiness:
    def test_a_user_of_another_tenant_is_not_recorded(self, course, db_session):
        other = real_tenant(db_session, "results-other")
        outsider = _student(db_session, tenant=other.id)
        db_session.add(Enrollment(user_id=outsider.id, course_id=course.id, tenant_id=other.id))
        db_session.commit()
        _grade(course, user=outsider.id)
        body = _pull(course).json()
        assert body["applied"] == 0 and body["skipped"] == {"not_this_tenants_user": 1}
        assert db_session.query(QuizAttempt).filter_by(user_id=outsider.id).count() == 0

    def test_a_course_never_published_to_this_moodle_is_not_recorded(self, course, db_session):
        other_course = db_session.query(CourseModule.course_id).filter(CourseModule.course_id != course.id).first()
        _grade(course, course=other_course[0] if other_course else uuid.uuid4())
        assert _pull(course).json()["skipped"] == {"course_not_published_here": 1}

    def test_moodle_does_not_enrol_anyone(self, course, db_session):
        stranger = _student(db_session)
        db_session.commit()
        _grade(course, user=stranger.id)
        assert _pull(course).json()["skipped"] == {"not_enrolled": 1}
        assert db_session.query(Enrollment).filter_by(user_id=stranger.id).count() == 0

    def test_a_withdrawn_enrolment_is_not_reopened(self, course, db_session):
        course.enrollment.status = EnrollmentStatus.withdrawn
        db_session.commit()
        _grade(course)
        assert _pull(course).json()["skipped"] == {"withdrawn": 1}
        db_session.refresh(course.enrollment)
        assert course.enrollment.status == EnrollmentStatus.withdrawn

    def test_malformed_and_unknown_rows_are_dropped(self, course, db_session):
        fake.record_result(course.platform, kind="quiz_grade", user="not-a-uuid", course=str(course.id))
        fake.record_result(course.platform, kind="sql", user=str(course.student.id), course=str(course.id))
        fake.record_result(course.platform, kind="completion", user=str(course.student.id), course=str(course.id),
                           activity="tn:mod_999:page:01", state=1)
        assert _pull(course).json()["skipped"] == {"malformed": 1, "unknown_kind": 1, "unknown_activity": 1}


class TestTheEndpoint:
    def test_a_student_cannot_pull(self, course):
        with acting_as(UserRole.student):
            assert _pull(course).status_code == 403

    def test_another_tenants_admin_gets_404(self, course):
        with acting_as(UserRole.admin, tenant="00000000-0000-0000-0000-0000000000ff"):
            assert _pull(course).status_code == 404
            assert course.client.get(f"/integrations/platforms/{course.platform.id}/moodle-results").status_code == 404

    def test_a_platform_that_is_not_a_moodle_is_422(self, course, db_session):
        course.platform.platform_type = "offsec"
        db_session.commit()
        assert _pull(course).status_code == 422

    def test_a_refusing_moodle_is_502_and_the_error_is_kept(self, course):
        fake.fail_next("pull_results")
        resp = _pull(course)
        assert resp.status_code == 502
        status = course.client.get(f"/integrations/platforms/{course.platform.id}/moodle-results").json()
        assert "injected failure" in status["last_error"] and status["running"] is False

    def test_a_pull_already_running_is_409(self, course, db_session):
        service.cursor_row(db_session, course.platform)
        service._claim(db_session, course.platform)
        assert _pull(course).status_code == 409


# -- the real backend's trust rules, against a mock plugin -------------------------------
ISSUER = "https://moodle.example.test"
JWKS_URL = f"{ISSUER}/mod/lti/certs.php"


def _rsa() -> tuple[str, dict]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return pem, {**jwk, "kid": "moodle-site", "alg": "RS256", "use": "sig"}


MOODLE_PEM, MOODLE_JWK = _rsa()
OTHER_PEM, _ = _rsa()


class Plugin:
    """Plays local_truenorth: checks the sync ticket, answers pull_results signed."""

    def __init__(self, db, platform, *, sign_with=MOODLE_PEM, tid=None, req=None, signed=True, rows=None):
        self.db, self.platform = db, platform
        self.sign_with, self.tid, self.req, self.signed = sign_with, tid, req, signed
        self.rows = rows if rows is not None else [{"kind": "completion", "user": "u", "course": "c"}]
        self.tickets: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mod/lti/certs.php":
            return httpx.Response(200, json={"keys": [MOODLE_JWK]})
        ticket = request.headers["authorization"].split(" ", 1)[1]
        claims = jwt.decode(ticket, lti13.get_tool_key(self.db).public_key_pem, algorithms=["RS256"],
                            audience=ISSUER)
        self.tickets.append(claims)
        if not self.signed:
            return httpx.Response(200, json={"ok": True, "rows": self.rows, "cursor": "9", "more": False})
        now = int(time.time())
        answer = jwt.encode(
            {"iss": ISSUER, "aud": "truenorth", "typ": "results", "tid": self.tid or str(self.platform.tenant_id),
             "req": self.req or claims["jti"], "iat": now, "exp": now + 60,
             "results": {"rows": self.rows, "cursor": "9", "more": False}},
            self.sign_with, algorithm="RS256", headers={"kid": "moodle-site"},
        )
        return httpx.Response(200, json={"ok": True, "signed": answer})


@pytest.fixture
def moodle(db_session):
    p = ExternalPlatform(id=uuid.uuid4(), name="Moodle", slug=f"m-{uuid.uuid4().hex[:6]}", platform_type="moodle",
                         base_url=ISSUER, lti_issuer=ISSUER, lti_jwks_url=JWKS_URL,
                         auth_type=IntegrationAuthType.lti13, tenant_id=DEV_TENANT)
    db_session.add(p)
    db_session.flush()
    return p


def _backend(db, plugin) -> LocalTrueNorthMoodle:
    key = lti13.get_tool_key(db)
    return LocalTrueNorthMoodle(key_provider=lambda: (lti13.signing_pem(key), key.kid),
                                transport=httpx.MockTransport(plugin))


class TestSignedAnswers:
    def test_a_signed_answer_to_this_request_is_accepted(self, db_session, moodle):
        plugin = Plugin(db_session, moodle)
        page = _backend(db_session, plugin).pull_results(moodle, "", 50)
        assert page == {"rows": plugin.rows, "cursor": "9", "more": False}
        [ticket] = plugin.tickets
        assert ticket["typ"] == "sync" and ticket["tid"] == str(DEV_TENANT)

    @pytest.mark.parametrize(
        ("kw", "why"),
        [
            ({"signed": False}, "not signed"),
            ({"sign_with": OTHER_PEM}, "not signed by its registered key"),
            ({"tid": "00000000-0000-0000-0000-0000000000ff"}, "another purpose or tenant"),
            ({"req": "an-earlier-request"}, "replayed"),
        ],
        ids=["unsigned", "forged", "other-tenant", "replayed"],
    )
    def test_an_answer_that_is_not_moodles_own_reply_is_refused(self, db_session, moodle, kw, why):
        with pytest.raises(MoodleError, match=why):
            _backend(db_session, Plugin(db_session, moodle, **kw)).pull_results(moodle, "", 50)

    def test_a_moodle_without_a_registered_key_set_cannot_report(self, db_session, moodle):
        moodle.lti_jwks_url = None
        with pytest.raises(MoodleError, match="cannot be verified"):
            _backend(db_session, Plugin(db_session, moodle)).pull_results(moodle, "", 50)

    def test_a_refused_answer_records_nothing_and_keeps_the_cursor(self, db_session, moodle):
        service.cursor_row(db_session, moodle)
        db_session.get(MoodleResultCursor, moodle.id).cursor = "5"
        db_session.commit()
        plugin = Plugin(db_session, moodle, sign_with=OTHER_PEM)
        with pytest.raises(MoodleError):
            service.pull(db_session, moodle, backend=_backend(db_session, plugin))
        row = db_session.get(MoodleResultCursor, moodle.id)
        db_session.refresh(row)
        assert row.cursor == "5" and "registered key" in row.last_error and row.lease_holder is None


def test_the_schedule_is_off_when_its_interval_is_zero(monkeypatch):
    import asyncio

    from app.moodle_results import runner

    monkeypatch.setenv("MOODLE_RESULTS_PULL_SECONDS", "0")
    asyncio.run(asyncio.wait_for(runner.loop(), 1))  # returns at once rather than sleeping
    monkeypatch.setenv("MOODLE_RESULTS_PULL_SECONDS", "junk")
    assert runner.interval() == 600.0
