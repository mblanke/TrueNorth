"""Curriculum Forge (``app.routers.curriculum``): who may touch a curriculum.

What these pin down:

- every curriculum endpoint needs ``course:author``: a Student (and an observer, and
  range ops) gets 403 on create, upload, register-urls, ingest, delete, list, read and
  RAG search — curricula are a tenant's raw source courseware, used only by authoring tools;
- an instructor can do all of it in their own tenant;
- every by-id lookup is tenant-scoped: another tenant's curriculum is 404, never 403,
  and a cross-tenant search never reaches the RAG index;
- the list only shows the caller's tenant.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app import curriculum_ingest, object_store
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Curriculum, CurriculumDocStatus, CurriculumDocument, CurriculumStatus, Tenant, User, UserRole
from app.routers import curriculum as curriculum_router

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c001")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0ff")


@pytest.fixture
def tenants(db_session):
    for tid in (TENANT, OTHER_TENANT):
        if db_session.get(Tenant, tid) is None:
            db_session.add(Tenant(id=tid, name=f"curr-{tid.hex[-4:]}", slug=f"curr-{tid.hex[-4:]}"))
    db_session.flush()


@pytest.fixture
def no_side_effects(monkeypatch):
    """No MinIO, no OpenSearch, no background ingestion: record what would have happened."""
    calls: dict[str, list] = {"put": [], "ingest": [], "search": [], "delete_index": []}
    monkeypatch.setattr(object_store, "put_object", lambda key, data, mime: calls["put"].append(key))

    async def _ingest(curriculum_id, doc_ids):
        calls["ingest"].append((curriculum_id, list(doc_ids)))

    async def _search(curriculum_id, query, k=8):
        calls["search"].append(str(curriculum_id))
        return [{"text": f"chunk of {curriculum_id}", "filename": "a.md", "chunk_ordinal": 0, "score": 1.0}]

    async def _delete_index(curriculum_id):
        calls["delete_index"].append(str(curriculum_id))

    monkeypatch.setattr(curriculum_router, "_ingest_documents", _ingest)
    monkeypatch.setattr(curriculum_ingest, "rag_search", _search)
    monkeypatch.setattr(curriculum_ingest, "delete_index", _delete_index)
    return calls


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


def _curriculum(db, tenant: uuid.UUID = TENANT, *, with_errored_doc: bool = False) -> Curriculum:
    c = Curriculum(name="SOC tier 1", description="", tenant_id=tenant, status=CurriculumStatus.ready)
    db.add(c)
    db.flush()
    if with_errored_doc:
        db.add(
            CurriculumDocument(
                curriculum_id=c.id, filename="notes.md", tenant_id=tenant, status=CurriculumDocStatus.error
            )
        )
        db.flush()
    return c


def _request(client, method: str, path: str):
    """Each curriculum endpoint with a body it would accept."""
    if path.endswith("/documents"):
        return client.post(path, files=[("files", ("notes.md", b"# DNS\nbeacons", "text/markdown"))])
    if path.endswith("/urls"):
        return client.post(path, json={"urls": ["https://example.test/page"]})
    if path.endswith("/search"):
        return client.post(path, json={"query": "dns beacon", "k": 3})
    if method == "post" and path == "/curricula":
        return client.post(path, json={"name": "New"})
    return client.request(method.upper(), path)


BY_ID = [
    ("get", ""),
    ("delete", ""),
    ("post", "/documents"),
    ("post", "/urls"),
    ("post", "/ingest"),
    ("post", "/search"),
]


# ── Students and other non-authors ──────────────────────────────────────


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
@pytest.mark.parametrize(("method", "suffix"), BY_ID)
def test_non_authors_get_403_on_every_by_id_endpoint(
    client, db_session, tenants, no_side_effects, role, method, suffix
):
    cur = _curriculum(db_session, with_errored_doc=True)
    with acting_as(_user(db_session, role)):
        r = _request(client, method, f"/curricula/{cur.id}{suffix}")
    assert r.status_code == 403, r.text
    assert "course:author" in r.text
    db_session.refresh(cur)
    assert cur.deleted_at is None
    assert no_side_effects == {"put": [], "ingest": [], "search": [], "delete_index": []}


@pytest.mark.parametrize("method", ["get", "post"])
def test_student_cannot_list_or_create(client, db_session, tenants, no_side_effects, method):
    _curriculum(db_session)
    with acting_as(_user(db_session, UserRole.student)):
        r = _request(client, method, "/curricula")
    assert r.status_code == 403, r.text
    assert db_session.query(Curriculum).filter(Curriculum.name == "New").count() == 0


# ── Instructors ─────────────────────────────────────────────────────────


def test_instructor_creates_lists_and_reads_in_own_tenant(client, db_session, tenants, no_side_effects):
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post("/curricula", json={"name": "Threat hunting"})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        assert client.get(f"/curricula/{cid}").status_code == 200
        listed = [c["id"] for c in client.get("/curricula").json()]
    assert cid in listed
    assert str(db_session.get(Curriculum, uuid.UUID(cid)).tenant_id) == str(TENANT)


def test_instructor_uploads_registers_ingests_searches_and_deletes(client, db_session, tenants, no_side_effects):
    cur = _curriculum(db_session, with_errored_doc=True)
    with acting_as(_user(db_session, UserRole.instructor)):
        assert _request(client, "post", f"/curricula/{cur.id}/documents").status_code == 202
        assert _request(client, "post", f"/curricula/{cur.id}/urls").status_code == 202
        assert _request(client, "post", f"/curricula/{cur.id}/ingest").status_code == 202
        r = _request(client, "post", f"/curricula/{cur.id}/search")
        assert r.status_code == 200, r.text
        assert client.delete(f"/curricula/{cur.id}").status_code == 204
    assert len(no_side_effects["put"]) == 1
    assert len(no_side_effects["ingest"]) == 3
    assert no_side_effects["search"] == [str(cur.id)]
    assert no_side_effects["delete_index"] == [str(cur.id)]
    db_session.refresh(cur)
    assert cur.deleted_at is not None


# ── Tenant isolation ────────────────────────────────────────────────────


@pytest.mark.parametrize(("method", "suffix"), BY_ID)
def test_another_tenants_curriculum_is_404(client, db_session, tenants, no_side_effects, method, suffix):
    theirs = _curriculum(db_session, tenant=OTHER_TENANT, with_errored_doc=True)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = _request(client, method, f"/curricula/{theirs.id}{suffix}")
    assert r.status_code == 404, r.text
    db_session.refresh(theirs)
    assert theirs.deleted_at is None
    assert no_side_effects == {"put": [], "ingest": [], "search": [], "delete_index": []}


def test_search_only_reaches_the_callers_own_curriculum(client, db_session, tenants, no_side_effects):
    mine = _curriculum(db_session)
    theirs = _curriculum(db_session, tenant=OTHER_TENANT)
    with acting_as(_user(db_session, UserRole.instructor)):
        ok = _request(client, "post", f"/curricula/{mine.id}/search")
        blocked = _request(client, "post", f"/curricula/{theirs.id}/search")
    assert ok.status_code == 200
    assert all(str(mine.id) in hit["text"] for hit in ok.json())
    assert blocked.status_code == 404
    # The other tenant's index was never queried.
    assert no_side_effects["search"] == [str(mine.id)]


def test_list_is_tenant_scoped(client, db_session, tenants, no_side_effects):
    mine = _curriculum(db_session)
    theirs = _curriculum(db_session, tenant=OTHER_TENANT)
    with acting_as(_user(db_session, UserRole.instructor)):
        ids = {c["id"] for c in client.get("/curricula").json()}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids
