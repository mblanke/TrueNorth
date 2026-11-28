# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Curriculum Forge (``app.routers.curriculum``): the upload guards and generation.

Complements test_curriculum_authz.py (permissions, tenancy) with the behaviours the
author hits: upload validation (unsupported suffix, NUL byte, empty body, size cap),
URL registration that refuses non-http(s) schemes, reingest that 409s when nothing is
pending, RAG search that reports the indexer's failure as 502, and course generation:
409 before ingest, 409 with no indexed content, a real draft becoming Course + modules
(+ a Quiz per quiz module, unknown content types falling back to reading), and the
orchestrator's failures surfaced as 502/503.

No network: the RAG search and the AI orchestrator are the seams (test_curriculum_authz.py
stubs the same two; here the orchestrator stub answers with a real course draft).
"""

from __future__ import annotations

import json
import uuid

import pytest
from _shared import real_tenant
from app import curriculum_ingest, object_store
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Course,
    CourseModule,
    Curriculum,
    CurriculumDocStatus,
    CurriculumDocument,
    CurriculumStatus,
    Quiz,
    UserRole,
)
from app.routers import curriculum as curriculum_router


@pytest.fixture
def tenant(db_session):
    return real_tenant(db_session, "cov-d-curr")


@pytest.fixture
def instructor(db_session, tenant):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4()}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="Author",
        role=UserRole.instructor,
        tenant_id=tenant.id,
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture(autouse=True)
def author(db_session, instructor):
    """Every request in this module acts as the tenant's instructor (course:author)."""
    who = CurrentUser(
        id=str(instructor.id),
        email=instructor.email,
        display_name=instructor.display_name,
        role=instructor.role,
        tenant_id=str(instructor.tenant_id),
        keycloak_id=instructor.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    yield
    fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def seams(monkeypatch):
    """No MinIO, no OpenSearch: record object-store writes, answer rag_search from a list."""
    state: dict = {"put": [], "search": [], "results": []}
    monkeypatch.setattr(object_store, "put_object", lambda key, data, mime: state["put"].append(key))

    async def rag_search(curriculum_id, query, k=8):
        state["search"].append((str(curriculum_id), query))
        return state["results"]

    monkeypatch.setattr(curriculum_ingest, "rag_search", rag_search)
    state["results"] = [
        {"text": "phishing playbook: check headers", "filename": "a.md", "chunk_ordinal": 0, "score": 1.0}
    ]
    return state


def _curriculum(db, tenant_id, *, status=CurriculumStatus.ready) -> Curriculum:
    c = Curriculum(name="SOC tier 1", description="", tenant_id=tenant_id, status=status)
    db.add(c)
    db.flush()
    return c


# ── Document upload guards ───────────────────────────────────────────────


def test_an_unsupported_suffix_is_415(client, db_session, tenant):
    cur = _curriculum(db_session, tenant.id)
    r = client.post(
        f"/curricula/{cur.id}/documents", files=[("files", ("notes.exe", b"MZ", "application/octet-stream"))]
    )
    assert r.status_code == 415, r.text
    assert "Unsupported file type" in r.text
    assert db_session.query(CurriculumDocument).count() == 0


def test_the_suffix_check_is_case_insensitive(client, db_session, tenant):
    cur = _curriculum(db_session, tenant.id)
    r = client.post(f"/curricula/{cur.id}/documents", files=[("files", ("NOTES.MD", b"# x", "text/markdown"))])
    assert r.status_code == 202, r.text
    assert db_session.query(CurriculumDocument).one().filename == "NOTES.MD"


def test_an_empty_upload_is_422_and_a_cap_exceeded_one_is_413(client, db_session, tenant, monkeypatch):
    cur = _curriculum(db_session, tenant.id)
    empty = client.post(f"/curricula/{cur.id}/documents", files=[("files", ("empty.md", b"", "text/markdown"))])
    assert empty.status_code == 422, empty.text
    assert "empty" in empty.text

    monkeypatch.setattr(curriculum_router, "MAX_UPLOAD_BYTES", 8)
    big = client.post(f"/curricula/{cur.id}/documents", files=[("files", ("big.md", b"x" * 9, "text/markdown"))])
    assert big.status_code == 413, big.text
    assert "exceeds" in big.text
    # neither attempt left a document or moved the curriculum out of ready
    assert db_session.query(CurriculumDocument).count() == 0
    db_session.refresh(cur)
    assert cur.status == CurriculumStatus.ready


def test_a_valid_upload_puts_the_file_and_marks_the_curriculum_ingesting(
    client, db_session, tenant, seams, monkeypatch
):
    cur = _curriculum(db_session, tenant.id)

    async def _no_ingest(curriculum_id, doc_ids):
        seams.setdefault("ingested", []).append((str(curriculum_id), [str(d) for d in doc_ids]))

    monkeypatch.setattr(curriculum_router, "_ingest_documents", _no_ingest)
    r = client.post(
        f"/curricula/{cur.id}/documents",
        files=[("files", ("beacons.md", b"# DNS beacons\nhow to spot them", "text/markdown"))],
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == CurriculumStatus.ingesting.value
    doc = db_session.query(CurriculumDocument).one()
    assert doc.status == CurriculumDocStatus.pending
    assert doc.minio_key == f"{cur.id}/{doc.id}/beacons.md"
    assert seams["put"] == [doc.minio_key]
    assert seams["ingested"] == [(str(cur.id), [str(doc.id)])]


# ── URL registration and reingest ────────────────────────────────────────


def test_register_urls_refuses_a_non_http_scheme(client, db_session, tenant):
    cur = _curriculum(db_session, tenant.id)
    r = client.post(f"/curricula/{cur.id}/urls", json={"urls": ["ftp://example.test/page"]})
    assert r.status_code == 422, r.text
    assert "Not an http(s) URL" in r.text
    assert db_session.query(CurriculumDocument).count() == 0


def test_reingest_without_pending_or_errored_documents_is_409(client, db_session, tenant):
    cur = _curriculum(db_session, tenant.id)
    db_session.add(
        CurriculumDocument(
            curriculum_id=cur.id, filename="done.md", tenant_id=tenant.id, status=CurriculumDocStatus.indexed
        )
    )
    db_session.flush()
    r = client.post(f"/curricula/{cur.id}/ingest")
    assert r.status_code == 409, r.text
    assert "No pending or errored" in r.text


def test_reingest_picks_up_errored_and_pending_documents(client, db_session, tenant, monkeypatch, seams):
    cur = _curriculum(db_session, tenant.id)
    db_session.add_all(
        [
            CurriculumDocument(
                curriculum_id=cur.id, filename="broke.md", tenant_id=tenant.id, status=CurriculumDocStatus.error
            ),
            CurriculumDocument(
                curriculum_id=cur.id, filename="new.md", tenant_id=tenant.id, status=CurriculumDocStatus.pending
            ),
            CurriculumDocument(
                curriculum_id=cur.id, filename="done.md", tenant_id=tenant.id, status=CurriculumDocStatus.indexed
            ),
        ]
    )
    db_session.flush()

    async def _no_ingest(curriculum_id, doc_ids):
        seams["reingested"] = sorted(
            d.filename
            for d in db_session.query(CurriculumDocument).filter(
                CurriculumDocument.id.in_([uuid.UUID(str(i)) for i in doc_ids])
            )
        )

    monkeypatch.setattr(curriculum_router, "_ingest_documents", _no_ingest)
    r = client.post(f"/curricula/{cur.id}/ingest")
    assert r.status_code == 202, r.text
    assert seams["reingested"] == ["broke.md", "new.md"]  # the indexed doc is left alone
    db_session.refresh(cur)
    assert cur.status == CurriculumStatus.ingesting


# ── RAG search ───────────────────────────────────────────────────────────


def test_a_failing_rag_backend_is_a_502_not_a_crash(client, db_session, tenant, seams, monkeypatch):
    cur = _curriculum(db_session, tenant.id)

    async def boom(curriculum_id, query, k=8):
        raise RuntimeError("index shard closed")

    monkeypatch.setattr(curriculum_ingest, "rag_search", boom)
    r = client.post(f"/curricula/{cur.id}/search", json={"query": "phishing"})
    assert r.status_code == 502, r.text
    assert "RAG search failed" in r.text


def test_search_rejects_a_too_short_query(client, db_session, tenant):
    cur = _curriculum(db_session, tenant.id)
    assert client.post(f"/curricula/{cur.id}/search", json={"query": "d"}).status_code == 422


# ── Course generation ────────────────────────────────────────────────────

DRAFT = {
    "name": "Phishing Basics",
    "description": "Spot the lure",
    "difficulty": "beginner",
    "duration_hours": 3,
    "tags": ["email"],
    "nice_work_roles": ["SOC-analyst"],
    "model_used": "unused-here",
    "modules": [
        {"ordinal": 1, "title": "Headers", "content_type": "reading", "duration_minutes": 30},
        {"ordinal": 2, "title": "Quiz time", "content_type": "quiz"},
        {"ordinal": 3, "title": "Unknown type", "content_type": "teleportation"},
    ],
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """What the AI orchestrator answers (the HTTP seam; no orchestrator runs here)."""

    last_payload: dict = {}

    def __init__(self, payload=None, **kwargs):
        # The orchestrator's "output" is the model's raw text; the router parses JSON out of it.
        self._payload = payload if payload is not None else {"output": json.dumps(DRAFT), "model_used": "mock-v2"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.last_payload = {"url": url, "body": json, "headers": dict(headers or {})}
        return _FakeResponse(self._payload)


@pytest.fixture
def orchestrator(monkeypatch):
    """Point the router's httpx at _FakeAsyncClient; tests tweak its payload via `answer`."""

    def answer(payload):
        monkeypatch.setattr(curriculum_router.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload))

    monkeypatch.setattr(curriculum_router.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient())
    return answer


def test_generate_course_refuses_a_curriculum_that_is_not_ready(client, db_session, tenant, orchestrator):
    cur = _curriculum(db_session, tenant.id, status=CurriculumStatus.ingesting)
    r = client.post(f"/curricula/{cur.id}/generate-course", json={})
    assert r.status_code == 409, r.text
    assert "not ready" in r.text


def test_generate_course_refuses_a_curriculum_with_no_indexed_content(client, db_session, tenant, orchestrator, seams):
    cur = _curriculum(db_session, tenant.id)
    seams["results"] = []
    r = client.post(f"/curricula/{cur.id}/generate-course", json={})
    assert r.status_code == 409, r.text
    assert "No indexed curriculum content" in r.text


def test_generate_course_builds_the_course_modules_and_a_quiz_per_quiz_module(
    client, db_session, tenant, orchestrator, seams
):
    cur = _curriculum(db_session, tenant.id)
    r = client.post(f"/curricula/{cur.id}/generate-course", json={"difficulty": "beginner", "module_count": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Phishing Basics"
    assert body["module_count"] == 3
    assert body["quiz_module_count"] == 1

    course = db_session.query(Course).filter(Course.name == "Phishing Basics").one()
    assert course.tenant_id == tenant.id
    assert course.is_published is False
    assert course.difficulty == "beginner"
    meta = json.loads(course.course_meta)
    assert meta["source_curriculum_id"] == str(cur.id)
    assert meta["generated_by_model"] == "mock-v2"

    modules = db_session.query(CourseModule).filter(CourseModule.course_id == course.id).all()
    by_title = {m.title: m for m in modules}
    assert len(modules) == 3
    assert "teleportation" not in [m.content_type.value for m in modules]  # unknown type falls back
    assert by_title["Unknown type"].content_type.value == "reading"
    quiz = db_session.query(Quiz).filter(Quiz.curriculum_id == cur.id).one()
    assert quiz.module_id == by_title["Quiz time"].id
    assert quiz.is_published is False

    # The orchestrator got the curriculum's context and the caller's knobs.
    sent = _FakeAsyncClient.last_payload
    assert sent["url"].endswith("/ai/course-generate")
    assert sent["body"]["difficulty"] == "beginner"
    assert sent["body"]["module_count"] == 3
    assert any("phishing playbook" in chunk for chunk in sent["body"]["context_chunks"])


def test_generate_course_reports_an_unusable_draft_as_502(client, db_session, tenant, orchestrator):
    cur = _curriculum(db_session, tenant.id)
    orchestrator({"output": '{"name": "empty shell"}', "model_used": "mock-v2"})
    r = client.post(f"/curricula/{cur.id}/generate-course", json={})
    assert r.status_code == 502, r.text
    assert "unusable course draft" in r.text


def test_generate_course_reports_an_orchestrator_connection_failure_as_503(client, db_session, tenant, monkeypatch):
    import httpx

    cur = _curriculum(db_session, tenant.id)

    class _Down:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(curriculum_router.httpx, "AsyncClient", lambda **kw: _Down())
    r = client.post(f"/curricula/{cur.id}/generate-course", json={})
    assert r.status_code == 503, r.text
    assert "unavailable" in r.text


def test_parse_ai_json_tolerates_fences_prose_and_refuses_garbage():
    fenced = curriculum_router._parse_ai_json('```json\n{"modules": [1]}\n```')
    assert fenced == {"modules": [1]}
    wrapped = curriculum_router._parse_ai_json('The course draft:\n{"name": "x", "modules": []}\nHope it helps!')
    assert wrapped == {"name": "x", "modules": []}
    with pytest.raises(Exception) as exc:
        curriculum_router._parse_ai_json("no json anywhere here")
    assert "invalid JSON" in str(exc.value)
