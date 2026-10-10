"""The statements a quiz submit really emits: registration, language, pass mark, result.

``POST /quizzes/attempts/{id}/submit`` is the busiest xAPI producer. These run the real
route with the emitter captured (nothing reaches an LRS) and check every statement:

* the actor is the Student's account (``users.id``), with no email anywhere;
* a quiz inside a course the Student is enrolled in carries the **enrolment** as its
  registration and the course's locale as its language; a stand-alone quiz carries the
  **attempt**;
* passed/failed is judged against the quiz's own ``pass_pct``, and the result is
  bounded (``min <= raw <= max``, ``scaled`` in [0, 1]).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest
from app import xapi
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Course,
    CourseModule,
    Enrollment,
    ModuleContentType,
    Quiz,
    QuizQuestion,
    QuizQuestionType,
    Tenant,
    User,
    UserRole,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a5a1")


@pytest.fixture
def tenants(db_session):
    if db_session.get(Tenant, TENANT) is None:
        db_session.add(Tenant(id=TENANT, name="xapi-quiz", slug="xapi-quiz"))
    db_session.flush()


def _user(db, role: UserRole) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=TENANT,
    )
    db.add(u)
    db.flush()
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


def _quiz(db, *, pass_pct: int = 70, module_id: uuid.UUID | None = None) -> Quiz:
    quiz = Quiz(
        title="Log triage",
        tenant_id=TENANT,
        is_published=True,
        pass_pct=pass_pct,
        shuffle_questions=False,
        module_id=module_id,
    )
    db.add(quiz)
    db.flush()
    for i, (stem, options, correct, points) in enumerate(
        [
            ("DNS port?", ["22", "53"], [1], 10),
            ("Log sources?", ["Sysmon", "Zeek", "Paint"], [0, 1], 10),
            ("SNI hidden?", ["True", "False"], [1], 5),
        ]
    ):
        db.add(
            QuizQuestion(
                quiz_id=quiz.id,
                ordinal=i,
                question_type=QuizQuestionType.mcq,
                stem=stem,
                options=json.dumps(options),
                correct=json.dumps(correct),
                points=points,
            )
        )
    db.flush()
    db.refresh(quiz)
    return quiz


def _answers_all_right(quiz: Quiz) -> dict[str, list[int]]:
    return {str(q.id): json.loads(q.correct) for q in quiz.questions}


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(xapi, "emit_statement_sync", lambda stmt, timeout=2.0: out.append(stmt) or True)
    from app.routers import quizzes as quizzes_router

    async def _no_grade(*_a, **_k):
        return None

    monkeypatch.setattr(quizzes_router, "_push_lti_grade", _no_grade)
    return out


def _submit(client, student, quiz, answers):
    with acting_as(student):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        r = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": answers})
    assert r.status_code == 200, r.text
    return uuid.UUID(attempt_id)


def _by_verb(statements, key):
    return [s for s in statements if s["verb"]["id"] == xapi.VERBS[key]]


def test_course_quiz_uses_the_enrolment_and_the_course_locale(client, db_session, tenants, sent):
    course = Course(name="SOC 101", tenant_id=TENANT, course_meta=json.dumps({"locale": "fr-CA"}))
    db_session.add(course)
    db_session.flush()
    module = CourseModule(course_id=course.id, title="Quiz", content_type=ModuleContentType.quiz, pass_threshold=70)
    db_session.add(module)
    db_session.flush()
    student = _user(db_session, UserRole.student)
    enrolment = Enrollment(user_id=student.id, course_id=course.id, tenant_id=TENANT)
    db_session.add(enrolment)
    db_session.flush()
    quiz = _quiz(db_session, module_id=module.id)

    _submit(client, student, quiz, _answers_all_right(quiz))

    assert sent, "no statement was emitted"
    for stmt in sent:
        assert stmt["actor"]["account"]["name"] == str(student.id)
        assert stmt["context"]["registration"] == str(enrolment.id)
        assert stmt["context"]["language"] == "fr-CA"
        assert student.email not in json.dumps(stmt)
    passed = _by_verb(sent, "passed")
    assert len(passed) == 1
    assert passed[0]["result"]["score"] == {"raw": 25, "min": 0, "max": 25, "scaled": 1.0}
    assert passed[0]["result"]["success"] is True
    assert passed[0]["result"]["duration"].startswith("PT")


def test_standalone_quiz_uses_the_attempt_and_its_own_pass_mark(client, db_session, tenants, sent):
    student = _user(db_session, UserRole.student)
    quiz = _quiz(db_session, pass_pct=90)
    first = quiz.questions[0]
    attempt_id = _submit(client, student, quiz, {str(first.id): [1]})  # 10 of 25 = 0.4

    assert {s["context"]["registration"] for s in sent} == {str(attempt_id)}
    assert {s["context"]["language"] for s in sent} == {xapi.default_language()}
    failed = _by_verb(sent, "failed")
    assert len(failed) == 1 and not _by_verb(sent, "passed")
    assert failed[0]["result"]["success"] is False
    assert failed[0]["result"]["score"]["scaled"] == 0.4
    assert failed[0]["context"]["extensions"][xapi.extension_iri("pass_threshold")] == 0.9
    for answered in _by_verb(sent, "answered"):
        score = answered["result"]["score"]
        assert score["min"] <= score["raw"] <= score["max"]
