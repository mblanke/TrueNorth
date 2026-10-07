"""Quiz engine (``app.routers.quizzes``): authoring, the Student attempt flow, grading.

What these pin down:

- the answer key (``GET /quizzes/{id}/questions``) and every authoring action need
  ``course:author``; a Student gets 403, and sees neither drafts nor their existence (404);
- every by-id lookup is tenant-scoped: another tenant's quiz is 404, never 403;
- grading is server-side and exact-set (a partly right multi-answer earns nothing);
- the attempt limit, the "already submitted" guard, and someone else's attempt (404);
- module progress and competency auto-assessment are written on submit;
- the AI generation path maps its failure modes to 404/409/502/503, never 500.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import httpx
import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    CompetencyAutoAssessment,
    Course,
    CourseModule,
    Curriculum,
    CurriculumStatus,
    Enrollment,
    ModuleContentType,
    ModuleProgress,
    ModuleProgressStatus,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionType,
    Tenant,
    User,
    UserRole,
)
from app.routers import quizzes as quizzes_router

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a001")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a0ff")


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def tenants(db_session):
    for tid in (TENANT, OTHER_TENANT):
        if db_session.get(Tenant, tid) is None:
            db_session.add(Tenant(id=tid, name=f"quiz-{tid.hex[-4:]}", slug=f"quiz-{tid.hex[-4:]}"))
    db_session.flush()


def _user(db, role: UserRole, tenant: uuid.UUID = TENANT) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
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


def _quiz(
    db,
    *,
    tenant: uuid.UUID = TENANT,
    published: bool = True,
    max_attempts: int = 0,
    pass_pct: int = 70,
    module_id: uuid.UUID | None = None,
    curriculum_id: uuid.UUID | None = None,
    questions: bool = True,
) -> Quiz:
    quiz = Quiz(
        title="Log triage",
        description="Find the beacon",
        tenant_id=tenant,
        is_published=published,
        max_attempts=max_attempts,
        pass_pct=pass_pct,
        shuffle_questions=False,
        module_id=module_id,
        curriculum_id=curriculum_id,
    )
    db.add(quiz)
    db.flush()
    if questions:
        db.add_all(
            [
                QuizQuestion(
                    quiz_id=quiz.id,
                    ordinal=0,
                    question_type=QuizQuestionType.mcq,
                    stem="Which port does DNS use?",
                    options=json.dumps(["22", "53", "80"]),
                    correct=json.dumps([1]),
                    explanation="DNS is 53.",
                    competency_code="T0023",
                    points=10,
                ),
                QuizQuestion(
                    quiz_id=quiz.id,
                    ordinal=1,
                    question_type=QuizQuestionType.multi,
                    stem="Which are log sources?",
                    options=json.dumps(["Sysmon", "Zeek", "Paint"]),
                    correct=json.dumps([0, 1]),
                    explanation="",
                    competency_code="T0023",
                    points=10,
                ),
                QuizQuestion(
                    quiz_id=quiz.id,
                    ordinal=2,
                    question_type=QuizQuestionType.truefalse,
                    stem="TLS hides SNI by default: true or false?",
                    options=json.dumps(["True", "False"]),
                    correct=json.dumps([1]),
                    points=5,
                ),
            ]
        )
        db.flush()
    db.refresh(quiz)
    return quiz


@pytest.fixture
def xapi(monkeypatch):
    """Capture xAPI statements instead of queueing them."""
    sent: list[tuple[str, str]] = []

    def _capture(_bg, verb, _email, _name, activity_type, activity_id, *_a, **_k):
        sent.append((verb, activity_type))

    monkeypatch.setattr(quizzes_router, "emit_lifecycle", _capture)
    monkeypatch.setattr(quizzes_router, "_push_lti_grade", _noop_grade)
    return sent


async def _noop_grade(*_a, **_k):
    return None


def _questions(quiz: Quiz) -> dict[str, QuizQuestion]:
    return {q.question_type.value: q for q in quiz.questions}


# ── RBAC: Student vs instructor ─────────────────────────────────────────


AUTHOR_ONLY = [
    ("get", "/questions", None),
    ("patch", "", {"title": "renamed"}),
    ("put", "/questions", []),
    ("delete", "", None),
    ("get", "/export", None),
]


@pytest.mark.parametrize("method,suffix,body", AUTHOR_ONLY)
def test_student_cannot_reach_authoring_endpoints(client, db_session, tenants, method, suffix, body):
    quiz = _quiz(db_session)
    student = _user(db_session, UserRole.student)
    with acting_as(student):
        kwargs = {"json": body} if body is not None else {}
        r = client.request(method.upper(), f"/quizzes/{quiz.id}{suffix}", **kwargs)
    assert r.status_code == 403, r.text
    db_session.refresh(quiz)
    assert quiz.deleted_at is None and quiz.title == "Log triage"


def test_student_cannot_generate(client, db_session, tenants):
    student = _user(db_session, UserRole.student)
    with acting_as(student):
        r = client.post("/quizzes/generate", json={"curriculum_id": str(uuid.uuid4()), "topic": "dns"})
    assert r.status_code == 403


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin])
def test_authors_read_the_answer_key(client, db_session, tenants, role):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, role)):
        r = client.get(f"/quizzes/{quiz.id}/questions")
    assert r.status_code == 200
    by_stem = {q["stem"]: q for q in r.json()}
    assert by_stem["Which port does DNS use?"]["correct"] == [1]
    assert by_stem["Which are log sources?"]["correct"] == [0, 1]


def test_student_list_hides_drafts_instructor_sees_them(client, db_session, tenants):
    published = _quiz(db_session)
    draft = _quiz(db_session, published=False)
    with acting_as(_user(db_session, UserRole.student)):
        student_ids = {q["id"] for q in client.get("/quizzes").json()}
    with acting_as(_user(db_session, UserRole.instructor)):
        instructor_ids = {q["id"] for q in client.get("/quizzes").json()}
    assert str(published.id) in student_ids and str(draft.id) not in student_ids
    assert {str(published.id), str(draft.id)} <= instructor_ids


def test_student_get_of_a_draft_is_404(client, db_session, tenants):
    draft = _quiz(db_session, published=False)
    with acting_as(_user(db_session, UserRole.student)):
        assert client.get(f"/quizzes/{draft.id}").status_code == 404
        assert client.post(f"/quizzes/{draft.id}/attempts").status_code == 404


def test_student_view_carries_no_answer_key(client, db_session, tenants, xapi):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.student)):
        r = client.post(f"/quizzes/{quiz.id}/attempts")
    assert r.status_code == 201
    for q in r.json()["questions"]:
        assert "correct" not in q and "explanation" not in q


# ── tenant isolation ────────────────────────────────────────────────────


def test_list_is_tenant_scoped(client, db_session, tenants):
    mine = _quiz(db_session)
    theirs = _quiz(db_session, tenant=OTHER_TENANT)
    with acting_as(_user(db_session, UserRole.instructor)):
        ids = {q["id"] for q in client.get("/quizzes").json()}
    assert str(mine.id) in ids and str(theirs.id) not in ids


@pytest.mark.parametrize(
    "method,suffix,body",
    [("get", "", None), ("post", "/attempts", None), *AUTHOR_ONLY],
)
def test_other_tenants_quiz_is_404_for_an_author(client, db_session, tenants, method, suffix, body):
    theirs = _quiz(db_session, tenant=OTHER_TENANT)
    with acting_as(_user(db_session, UserRole.instructor)):
        kwargs = {"json": body} if body is not None else {}
        r = client.request(method.upper(), f"/quizzes/{theirs.id}{suffix}", **kwargs)
    assert r.status_code == 404, r.text


def test_soft_deleted_quiz_is_gone(client, db_session, tenants):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.instructor)):
        assert client.delete(f"/quizzes/{quiz.id}").status_code == 204
        assert client.get(f"/quizzes/{quiz.id}").status_code == 404
        assert str(quiz.id) not in {q["id"] for q in client.get("/quizzes").json()}


def test_missing_quiz_is_404(client, db_session, tenants):
    with acting_as(_user(db_session, UserRole.instructor)):
        assert client.get(f"/quizzes/{uuid.uuid4()}").status_code == 404
        assert client.post(f"/quizzes/{uuid.uuid4()}/attempts").status_code == 404


# ── authoring ───────────────────────────────────────────────────────────


def test_update_quiz_patches_only_given_fields(client, db_session, tenants):
    quiz = _quiz(db_session, published=False)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.patch(f"/quizzes/{quiz.id}", json={"is_published": True, "pass_pct": 80})
    assert r.status_code == 200
    body = r.json()
    assert body["is_published"] is True and body["pass_pct"] == 80
    assert body["title"] == "Log triage" and body["question_count"] == 3


def test_update_quiz_rejects_out_of_range_pass_pct(client, db_session, tenants):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.instructor)):
        assert client.patch(f"/quizzes/{quiz.id}", json={"pass_pct": 0}).status_code == 422
        assert client.patch(f"/quizzes/{quiz.id}", json={"pass_pct": 101}).status_code == 422


def test_replace_questions_round_trip(client, db_session, tenants):
    quiz = _quiz(db_session)
    body = [
        {"question_type": "mcq", "stem": "Pick B", "options": ["A", "B"], "correct": [1]},
        {"question_type": "multi", "stem": "Pick both", "options": ["A", "B", "C"], "correct": [2, 0, 0]},
    ]
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.put(f"/quizzes/{quiz.id}/questions", json=body)
    assert r.status_code == 200
    out = r.json()
    assert [q["stem"] for q in out] == ["Pick B", "Pick both"]
    assert out[1]["correct"] == [0, 2]  # de-duplicated and sorted


@pytest.mark.parametrize(
    "question,detail",
    [
        ({"question_type": "mcq", "stem": "Out of range", "options": ["A", "B"], "correct": [2]}, "out of range"),
        ({"question_type": "mcq", "stem": "Two right", "options": ["A", "B"], "correct": [0, 1]}, "exactly one"),
        ({"question_type": "truefalse", "stem": "Three way", "options": ["T", "F", "?"], "correct": [0]}, "exactly 2"),
    ],
)
def test_replace_questions_rejects_invalid_answers(client, db_session, tenants, question, detail):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.put(f"/quizzes/{quiz.id}/questions", json=[question])
    assert r.status_code == 422
    assert detail in r.json()["detail"]


def test_replace_questions_schema_validation(client, db_session, tenants):
    quiz = _quiz(db_session)
    bad = {"question_type": "essay", "stem": "Write", "options": ["A", "B"], "correct": [0]}
    with acting_as(_user(db_session, UserRole.instructor)):
        assert client.put(f"/quizzes/{quiz.id}/questions", json=[bad]).status_code == 422


@pytest.mark.parametrize("fmt,marker", [("gift", "::Q1::"), ("moodlexml", "<quiz>")])
def test_export_formats(client, db_session, tenants, fmt, marker):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.get(f"/quizzes/{quiz.id}/export", params={"format": fmt})
    assert r.status_code == 200
    assert marker in r.text
    assert "attachment" in r.headers["content-disposition"]


def test_export_errors(client, db_session, tenants):
    empty = _quiz(db_session, questions=False)
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.instructor)):
        assert client.get(f"/quizzes/{empty.id}/export").status_code == 409
        assert client.get(f"/quizzes/{quiz.id}/export", params={"format": "pdf"}).status_code == 422


def test_gift_escapes_control_characters():
    assert quizzes_router._gift_escape("a=b{c}~d#e:f") == "a\\=b\\{c\\}\\~d\\#e\\:f"


# ── attempts ────────────────────────────────────────────────────────────


def _answers_all_right(quiz: Quiz) -> dict[str, list[int]]:
    return {str(q.id): json.loads(q.correct) for q in quiz.questions}


def test_full_marks_pass_and_emit_xapi(client, db_session, tenants, xapi):
    quiz = _quiz(db_session)
    student = _user(db_session, UserRole.student)
    with acting_as(student):
        start = client.post(f"/quizzes/{quiz.id}/attempts")
        assert start.status_code == 201
        attempt_id = start.json()["attempt_id"]
        r = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": _answers_all_right(quiz)})
    assert r.status_code == 200
    res = r.json()
    assert (res["score"], res["max_score"], res["pct"], res["passed"]) == (25, 25, 100.0, True)
    verbs = [v for v, _ in xapi]
    assert verbs.count("answered") == 3 and "scored" in verbs and "passed" in verbs

    attempt = db_session.get(QuizAttempt, uuid.UUID(attempt_id))
    assert attempt.submitted_at is not None and attempt.passed is True and attempt.score == 25


def test_partial_multi_answer_earns_nothing_and_fails(client, db_session, tenants, xapi):
    quiz = _quiz(db_session)
    q = _questions(quiz)
    answers = {
        str(q["mcq"].id): [1],  # right: 10
        str(q["multi"].id): [0],  # half of the set: 0
        # truefalse left blank: 0
    }
    with acting_as(_user(db_session, UserRole.student)):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        res = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": answers}).json()
    assert (res["score"], res["passed"]) == (10, False)
    by_id = {r["question_id"]: r for r in res["results"]}
    assert by_id[str(q["multi"].id)]["correct"] is False
    assert by_id[str(q["multi"].id)]["correct_options"] == [0, 1]
    assert "failed" in [v for v, _ in xapi]


def test_out_of_range_and_unknown_answers_are_simply_wrong(client, db_session, tenants, xapi):
    quiz = _quiz(db_session)
    q = _questions(quiz)
    answers = {str(q["mcq"].id): [99], str(uuid.uuid4()): [0]}
    with acting_as(_user(db_session, UserRole.student)):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        r = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": answers})
    assert r.status_code == 200
    assert r.json()["score"] == 0
    assert len(r.json()["results"]) == 3  # only the quiz's own questions are graded


def test_submit_body_must_be_an_answer_map(client, db_session, tenants, xapi):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.student)):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        assert client.post(f"/quizzes/attempts/{attempt_id}/submit", json={}).status_code == 422
        bad = {"answers": {"x": "not-a-list"}}
        assert client.post(f"/quizzes/attempts/{attempt_id}/submit", json=bad).status_code == 422


def test_double_submit_is_409(client, db_session, tenants, xapi):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.student)):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        first = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": {}})
        second = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": {}})
    assert first.status_code == 200
    assert second.status_code == 409


def test_someone_elses_attempt_is_404(client, db_session, tenants, xapi):
    quiz = _quiz(db_session)
    with acting_as(_user(db_session, UserRole.student)):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
    with acting_as(_user(db_session, UserRole.student)):
        r = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": {}})
    assert r.status_code == 404
    assert db_session.get(QuizAttempt, uuid.UUID(attempt_id)).submitted_at is None


def test_missing_attempt_is_404(client, db_session, tenants, xapi):
    with acting_as(_user(db_session, UserRole.student)):
        r = client.post(f"/quizzes/attempts/{uuid.uuid4()}/submit", json={"answers": {}})
    assert r.status_code == 404


def test_attempt_limit(client, db_session, tenants, xapi):
    quiz = _quiz(db_session, max_attempts=1)
    with acting_as(_user(db_session, UserRole.student)):
        first = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        # An unsubmitted attempt does not count against the limit.
        assert client.post(f"/quizzes/{quiz.id}/attempts").status_code == 201
        client.post(f"/quizzes/attempts/{first}/submit", json={"answers": {}})
        r = client.post(f"/quizzes/{quiz.id}/attempts")
    assert r.status_code == 409
    assert "Attempt limit reached (1)" in r.json()["detail"]


def test_attempt_limit_is_per_student(client, db_session, tenants, xapi):
    quiz = _quiz(db_session, max_attempts=1)
    with acting_as(_user(db_session, UserRole.student)):
        a = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        client.post(f"/quizzes/attempts/{a}/submit", json={"answers": {}})
    with acting_as(_user(db_session, UserRole.student)):
        assert client.post(f"/quizzes/{quiz.id}/attempts").status_code == 201


def test_unpublished_or_empty_quiz_cannot_be_attempted_by_an_author(client, db_session, tenants):
    draft = _quiz(db_session, published=False)
    empty = _quiz(db_session, questions=False)
    with acting_as(_user(db_session, UserRole.instructor)):
        assert client.post(f"/quizzes/{draft.id}/attempts").status_code == 409
        assert client.post(f"/quizzes/{empty.id}/attempts").status_code == 409


def test_submit_creates_module_progress_and_competency(client, db_session, tenants, xapi):
    course = Course(name="SOC 101", tenant_id=TENANT)
    db_session.add(course)
    db_session.flush()
    module = CourseModule(
        course_id=course.id, ordinal=0, title="Quiz", content_type=ModuleContentType.quiz, pass_threshold=70
    )
    db_session.add(module)
    db_session.flush()
    student = _user(db_session, UserRole.student)
    enrollment = Enrollment(user_id=student.id, course_id=course.id, tenant_id=TENANT)
    db_session.add(enrollment)
    db_session.flush()
    quiz = _quiz(db_session, module_id=module.id)

    with acting_as(student):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        r = client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": _answers_all_right(quiz)})
    assert r.status_code == 200

    progress = (
        db_session.query(ModuleProgress)
        .filter(ModuleProgress.enrollment_id == enrollment.id, ModuleProgress.module_id == module.id)
        .one()
    )
    assert progress.status == ModuleProgressStatus.completed
    assert (progress.score, progress.attempts) == (100, 1)

    auto = (
        db_session.query(CompetencyAutoAssessment)
        .filter(CompetencyAutoAssessment.quiz_attempt_id == uuid.UUID(attempt_id))
        .one()
    )
    mappings = json.loads(auto.competency_mappings)
    assert mappings == [{"competency_code": "T0023", "delta": 1.0, "reason": "Quiz 'Log triage': 20/20 points"}]


def test_failed_attempt_leaves_module_in_progress(client, db_session, tenants, xapi):
    course = Course(name="SOC 102", tenant_id=TENANT)
    db_session.add(course)
    db_session.flush()
    module = CourseModule(course_id=course.id, ordinal=0, title="Quiz", content_type=ModuleContentType.quiz)
    db_session.add(module)
    student = _user(db_session, UserRole.student)
    db_session.flush()
    enrollment = Enrollment(user_id=student.id, course_id=course.id, tenant_id=TENANT)
    db_session.add(enrollment)
    db_session.flush()
    quiz = _quiz(db_session, module_id=module.id)
    with acting_as(student):
        attempt_id = client.post(f"/quizzes/{quiz.id}/attempts").json()["attempt_id"]
        client.post(f"/quizzes/attempts/{attempt_id}/submit", json={"answers": {}})
    progress = db_session.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enrollment.id).one()
    assert progress.status == ModuleProgressStatus.in_progress
    assert progress.completed_at is None


# ── AI generation error paths ───────────────────────────────────────────


def _curriculum(db, status=CurriculumStatus.ready, tenant=TENANT) -> Curriculum:
    c = Curriculum(name="SOC", tenant_id=tenant, status=status)
    db.add(c)
    db.flush()
    return c


def test_generate_unknown_or_foreign_curriculum_is_404(client, db_session, tenants):
    foreign = _curriculum(db_session, tenant=OTHER_TENANT)
    with acting_as(_user(db_session, UserRole.instructor)):
        for cid in (uuid.uuid4(), foreign.id):
            r = client.post("/quizzes/generate", json={"curriculum_id": str(cid), "topic": "dns"})
            assert r.status_code == 404


def test_generate_on_a_curriculum_not_ready_is_409(client, db_session, tenants):
    cur = _curriculum(db_session, status=CurriculumStatus.ingesting)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post("/quizzes/generate", json={"curriculum_id": str(cur.id), "topic": "dns"})
    assert r.status_code == 409


def _fake_rag(chunks):
    async def _search(*_a, **_k):
        return [{"text": c} for c in chunks]

    return _search


def test_generate_without_matching_content_is_409(client, db_session, tenants, monkeypatch):
    cur = _curriculum(db_session)
    monkeypatch.setattr(quizzes_router.curriculum_ingest, "rag_search", _fake_rag([]))
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post("/quizzes/generate", json={"curriculum_id": str(cur.id), "topic": "dns"})
    assert r.status_code == 409


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; ``behaviour`` is a response or an exception."""

    behaviour: object = None

    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None):
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


def _ai_response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://ai/ai/quiz-generate"))


@pytest.mark.parametrize(
    "behaviour,expected",
    [
        (httpx.ConnectError("refused"), 503),
        (_ai_response(500, {"error": "boom"}), 502),
        (_ai_response(200, {"output": "not json at all"}), 502),
    ],
)
def test_generate_maps_ai_failures(client, db_session, tenants, monkeypatch, behaviour, expected):
    cur = _curriculum(db_session)
    monkeypatch.setattr(quizzes_router.curriculum_ingest, "rag_search", _fake_rag(["DNS uses port 53."]))
    fake = type("FakeClient", (_FakeAsyncClient,), {"behaviour": behaviour})
    monkeypatch.setattr(quizzes_router.httpx, "AsyncClient", fake)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post("/quizzes/generate", json={"curriculum_id": str(cur.id), "topic": "dns"})
    assert r.status_code == expected, r.text


def test_generate_creates_a_draft_quiz(client, db_session, tenants, monkeypatch):
    cur = _curriculum(db_session)
    output = json.dumps(
        [
            {"stem": "Port for DNS?", "options": ["22", "53"], "correct": [1], "question_type": "mcq"},
            {"stem": "Two right but typed mcq", "options": ["a", "b", "c"], "correct": [0, 2]},
            {"stem": "", "options": ["a", "b"], "correct": [0]},  # dropped: no stem
        ]
    )
    monkeypatch.setattr(quizzes_router.curriculum_ingest, "rag_search", _fake_rag(["DNS uses port 53."]))
    fake = type(
        "FakeClient", (_FakeAsyncClient,), {"behaviour": _ai_response(200, {"output": output, "model_used": "m"})}
    )
    monkeypatch.setattr(quizzes_router.httpx, "AsyncClient", fake)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post("/quizzes/generate", json={"curriculum_id": str(cur.id), "topic": "dns"})
        assert r.status_code == 201, r.text
        quiz_id = r.json()["id"]
        assert r.json()["is_published"] is False and r.json()["question_count"] == 2
        types = [q["question_type"] for q in client.get(f"/quizzes/{quiz_id}/questions").json()]
    assert types == ["mcq", "multi"]  # an mcq with two right answers is reclassified


# ── the AI output parser ────────────────────────────────────────────────


def test_parse_questions_tolerates_fences_and_prose():
    raw = 'Sure! ```json\n[{"stem": "Q?", "options": ["a", "b"], "correct": [0]}]\n``` hope that helps'
    parsed = quizzes_router._parse_questions(raw)
    assert len(parsed) == 1 and parsed[0]["question_type"] == QuizQuestionType.mcq


@pytest.mark.parametrize("raw", ["", "{}", "[1, 2]", '[{"stem": "Q", "options": ["a"], "correct": [0]}]'])
def test_parse_questions_rejects_garbage(raw):
    assert quizzes_router._parse_questions(raw) == []


def test_parse_questions_drops_out_of_range_and_clamps_points():
    raw = json.dumps(
        [
            {"stem": "bad", "options": ["a", "b"], "correct": [5]},
            {"stem": "ok", "options": ["a", "b"], "correct": [0], "points": 1000, "question_type": "bogus"},
        ]
    )
    parsed = quizzes_router._parse_questions(raw)
    assert len(parsed) == 1
    assert parsed[0]["points"] == 100 and parsed[0]["question_type"] == QuizQuestionType.mcq
