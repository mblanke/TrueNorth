"""Who sees which courses and learning paths (``app.routers.courses``).

- Scope: the caller's tenant plus global rows (tenant_id NULL); another tenant's course or
  path is never listed and is 404 by id. GET /courses used to list every tenant's courses.
- Drafts: without ``course:author`` an unpublished course or path is absent from lists
  (even with ``published_only=false``), 404 by id and on the outline, and cannot be
  enrolled on. Authors see their tenant's drafts and can still filter with published_only.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Course, LearningPath, Tenant, User, UserRole

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000e001")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000e0ff")


@pytest.fixture
def tenants(db_session):
    for tid in (TENANT, OTHER_TENANT):
        if db_session.get(Tenant, tid) is None:
            db_session.add(Tenant(id=tid, name=f"cat-{tid.hex[-4:]}", slug=f"cat-{tid.hex[-4:]}"))
    db_session.flush()


@pytest.fixture
def catalogue(db_session, tenants) -> dict[str, uuid.UUID]:
    """One published and one draft course and path in each tenant, plus global ones."""
    ids: dict[str, uuid.UUID] = {}
    for label, tenant in (("mine", TENANT), ("theirs", OTHER_TENANT), ("global", None)):
        for state, published in (("pub", True), ("draft", False)):
            c = Course(name=f"{label}-{state}", tenant_id=tenant, is_published=published)
            p = LearningPath(name=f"{label}-{state}", tenant_id=tenant, is_published=published)
            db_session.add_all([c, p])
            db_session.flush()
            ids[f"course:{label}-{state}"] = c.id
            ids[f"path:{label}-{state}"] = p.id
    return ids


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


def _course_names(client, **params) -> set[str]:
    r = client.get("/courses", params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return {c["name"] for c in r.json()["items"]}


def _path_names(client) -> set[str]:
    r = client.get("/learning-paths")
    assert r.status_code == 200, r.text
    return {p["name"] for p in r.json()}


# ── Tenant isolation ────────────────────────────────────────────────────


@pytest.mark.parametrize("role", [UserRole.student, UserRole.instructor])
def test_lists_never_show_another_tenants_rows(client, db_session, catalogue, role):
    with acting_as(_user(db_session, role)):
        courses, paths = _course_names(client), _path_names(client)
    for names in (courses, paths):
        assert not {n for n in names if n.startswith("theirs-")}
        assert "mine-pub" in names
        assert "global-pub" in names


@pytest.mark.parametrize("role", [UserRole.student, UserRole.instructor])
@pytest.mark.parametrize(
    "url", ["/courses/{course:theirs-pub}", "/courses/{course:theirs-pub}/outline", "/learning-paths/{path:theirs-pub}"]
)
def test_another_tenants_row_is_404_by_id(client, db_session, catalogue, role, url):
    path = url.replace("{course:theirs-pub}", str(catalogue["course:theirs-pub"]))
    path = path.replace("{path:theirs-pub}", str(catalogue["path:theirs-pub"]))
    with acting_as(_user(db_session, role)):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("url", ["/courses/{id}", "/courses/{id}/outline"])
def test_global_published_course_is_readable_by_id(client, db_session, catalogue, url):
    with acting_as(_user(db_session, UserRole.student)):
        r = client.get(url.format(id=catalogue["course:global-pub"]))
    assert r.status_code == 200, r.text


def test_global_published_path_is_readable_by_id(client, db_session, catalogue):
    with acting_as(_user(db_session, UserRole.student)):
        assert client.get(f"/learning-paths/{catalogue['path:global-pub']}").status_code == 200


# ── Drafts ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
def test_non_authors_never_see_drafts_in_lists(client, db_session, catalogue, role):
    with acting_as(_user(db_session, role)):
        # published_only=false is ignored for them, not honoured.
        courses = _course_names(client, published_only="false")
        paths = _path_names(client)
    assert courses == {"mine-pub", "global-pub"}
    assert paths == {"mine-pub", "global-pub"}


@pytest.mark.parametrize(
    "url",
    [
        "/courses/{course:mine-draft}",
        "/courses/{course:mine-draft}/outline",
        "/courses/{course:global-draft}",
        "/learning-paths/{path:mine-draft}",
        "/learning-paths/{path:global-draft}",
    ],
)
def test_student_gets_404_for_a_draft_by_id(client, db_session, catalogue, url):
    key = url[url.index("{") + 1 : url.index("}")]
    with acting_as(_user(db_session, UserRole.student)):
        assert client.get(url.replace("{" + key + "}", str(catalogue[key]))).status_code == 404


def test_student_cannot_enrol_on_a_draft(client, db_session, catalogue):
    student = _user(db_session, UserRole.student)
    with acting_as(student):
        draft = client.post(
            f"/courses/{catalogue['course:mine-draft']}/enroll",
            json={"user_id": str(student.id), "course_id": str(catalogue["course:mine-draft"])},
        )
        published = client.post(
            f"/courses/{catalogue['course:mine-pub']}/enroll",
            json={"user_id": str(student.id), "course_id": str(catalogue["course:mine-pub"])},
        )
    assert draft.status_code == 404
    assert published.status_code == 201, published.text


def test_instructor_sees_own_drafts_and_can_still_filter(client, db_session, catalogue):
    with acting_as(_user(db_session, UserRole.instructor)):
        everything = _course_names(client)
        published = _course_names(client, published_only="true")
        paths = _path_names(client)
        assert client.get(f"/courses/{catalogue['course:mine-draft']}").status_code == 200
        assert client.get(f"/courses/{catalogue['course:mine-draft']}/outline").status_code == 200
        assert client.get(f"/learning-paths/{catalogue['path:mine-draft']}").status_code == 200
    assert everything == {"mine-pub", "mine-draft", "global-pub", "global-draft"}
    assert published == {"mine-pub", "global-pub"}
    assert paths == {"mine-pub", "mine-draft", "global-pub", "global-draft"}
