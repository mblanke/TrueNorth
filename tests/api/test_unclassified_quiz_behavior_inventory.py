"""Behavior ratchet for the first Unclassified result-producing journey.

This is evidence of known gaps, not a compliance test. A real route submission is captured
at each output boundary; a new readable answer/score fails, while removing a finding passes
without editing the accepted baseline.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from pathlib import Path

from app import xapi
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    CompetencyAutoAssessment,
    Course,
    CourseModule,
    Enrollment,
    ModuleContentType,
    ModuleProgress,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionType,
    Tenant,
    User,
    UserRole,
)
from app.result_transfer.contract import ResultTransferError

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "docs/security/unclassified-behavior-baseline.json"
TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d1f0")


@contextmanager
def _acting_as(user: User):
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
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _world(db):
    db.add(Tenant(id=TENANT, name="DLP synthetic tenant", slug="dlp-synthetic"))
    db.flush()
    student = User(
        id=uuid.uuid4(),
        keycloak_id=str(uuid.uuid4()),
        email=f"subject-{uuid.uuid4().hex}@subjects.invalid",
        display_name="Learner D1F00001",
        role=UserRole.student,
        tenant_id=TENANT,
        source="subject",
    )
    course = Course(name="Synthetic DLP course", tenant_id=TENANT)
    db.add_all((student, course))
    db.flush()
    module = CourseModule(
        course_id=course.id,
        ordinal=0,
        title="DLP quiz",
        content_type=ModuleContentType.quiz,
        pass_threshold=70,
    )
    enrollment = Enrollment(user_id=student.id, course_id=course.id, tenant_id=TENANT)
    db.add_all((module, enrollment))
    db.flush()
    quiz = Quiz(
        title="Synthetic quiz",
        tenant_id=TENANT,
        module_id=module.id,
        is_published=True,
        pass_pct=70,
        max_attempts=1,
        shuffle_questions=False,
    )
    db.add(quiz)
    db.flush()
    question = QuizQuestion(
        quiz_id=quiz.id,
        ordinal=0,
        question_type=QuizQuestionType.mcq,
        stem="Synthetic question?",
        options=json.dumps(["No", "Yes"]),
        correct=json.dumps([1]),
        competency_code="TEST-ONLY",
        points=10,
    )
    db.add(question)
    db.flush()
    return student, enrollment, quiz, question


def _response_findings(body: dict) -> set[str]:
    findings = {
        f"api:quiz-submit.{field}"
        for field in ("score", "pct")
        if body.get(field) is not None
    }
    results = body.get("results") or []
    for field in ("selected", "correct", "points_earned"):
        if any(row.get(field) not in (None, []) for row in results):
            findings.add(f"api:quiz-submit.results[].{field}")
    return findings


def _xapi_findings(statements: list[dict]) -> set[str]:
    findings: set[str] = set()
    for statement in statements:
        verb = statement["verb"]["id"].rsplit("/", 1)[-1]
        result = statement.get("result") or {}
        if "score" in result:
            findings.add(f"xapi:{verb}.result.score")
        if verb == "answered" and "success" in result:
            findings.add("xapi:answered.result.success")
    return findings


def test_unclassified_quiz_adds_no_unreviewed_readable_result_path(
    client,
    db_session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    student, enrollment, quiz, question = _world(db_session)
    statements: list[dict] = []
    lti: list[tuple[int, int]] = []
    sealed: list[dict] = []
    monkeypatch.setattr(xapi, "emit_statement_sync", lambda value, timeout=2.0: statements.append(value) or True)
    from app.routers import quizzes as quiz_router

    async def capture_lti(_user_id, _quiz_id, score, max_score):
        lti.append((score, max_score))

    monkeypatch.setattr(quiz_router, "_push_lti_grade", capture_lti)
    monkeypatch.setattr(
        quiz_router,
        "queue_live_quiz_attempt",
        lambda _db, **values: sealed.append(values),
    )

    with _acting_as(student):
        started = client.post(f"/quizzes/{quiz.id}/attempts")
        assert started.status_code == 201
        attempt_id = uuid.UUID(started.json()["attempt_id"])
        response = client.post(
            f"/quizzes/attempts/{attempt_id}/submit",
            json={"answers": {str(question.id): [1]}},
        )
    assert response.status_code == 200

    attempt = db_session.get(QuizAttempt, attempt_id)
    progress = db_session.query(ModuleProgress).filter_by(enrollment_id=enrollment.id).one()
    competency = db_session.query(CompetencyAutoAssessment).filter_by(quiz_attempt_id=attempt_id).one_or_none()
    findings = _response_findings(response.json()) | _xapi_findings(statements)
    if json.loads(attempt.answers):
        findings.add("db:quiz_attempts.answers")
    for field in ("score", "max_score"):
        if getattr(attempt, field) is not None:
            findings.add(f"db:quiz_attempts.{field}")
        if getattr(progress, field) is not None:
            findings.add(f"db:module_progress.{field}")
        if competency is not None and getattr(competency, field if field != "score" else "raw_score") is not None:
            target = "raw_score" if field == "score" else field
            findings.add(f"db:competency_auto_assessments.{target}")
    if competency is not None and json.loads(competency.competency_mappings):
        findings.add("db:competency_auto_assessments.competency_mappings")
    if lti:
        findings.update(("lti:quiz-grade.score", "lti:quiz-grade.max_score"))

    accepted = set(json.loads(BASELINE.read_text())["findings"])
    assert len(sealed) == 1
    assert {key: value for key, value in sealed[0].items() if key != "occurred_at"} == {
        "attempt_id": attempt_id,
        "tenant_id": TENANT,
        "user_id": student.id,
        "raw_score": 10,
        "max_score": 10,
        "passed": True,
    }
    assert sealed[0]["occurred_at"].tzinfo is not None
    assert findings == accepted


def test_unclassified_quiz_fails_closed_when_result_cannot_be_sealed(
    client,
    db_session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    student, enrollment, quiz, question = _world(db_session)
    from app.routers import quizzes as quiz_router

    def unavailable(*_args, **_kwargs):
        raise ResultTransferError("test transfer failure")

    monkeypatch.setattr(quiz_router, "queue_live_quiz_attempt", unavailable)
    with _acting_as(student):
        started = client.post(f"/quizzes/{quiz.id}/attempts")
        attempt_id = uuid.UUID(started.json()["attempt_id"])
        response = client.post(
            f"/quizzes/attempts/{attempt_id}/submit",
            json={"answers": {str(question.id): [1]}},
        )

    assert response.status_code == 503
    db_session.expire_all()
    attempt = db_session.get(QuizAttempt, attempt_id)
    assert attempt.submitted_at is None and attempt.passed is None
    assert db_session.query(ModuleProgress).filter_by(enrollment_id=enrollment.id).count() == 0
