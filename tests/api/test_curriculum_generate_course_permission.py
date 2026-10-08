"""``POST /curricula/{id}/generate-course`` needs ``course:author``.

It writes a Course and its modules (authoring) and spends AI-orchestrator time. Until
2026-10-08 any signed-in user could call it; the Curriculum page offered the button to
Students. A Student now gets 403 before anything is looked up; staff get past the
permission check (a not-ready curriculum is 409, never 403).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Curriculum, CurriculumStatus, Tenant, User, UserRole

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c001")
BODY = {"difficulty": "beginner", "module_count": 4}


@pytest.fixture
def tenant(db_session):
    if db_session.get(Tenant, TENANT) is None:
        db_session.add(Tenant(id=TENANT, name="curr-gen", slug="curr-gen"))
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


def _curriculum(db, status=CurriculumStatus.ingesting) -> Curriculum:
    c = Curriculum(name="SOC", tenant_id=TENANT, status=status)
    db.add(c)
    db.flush()
    return c


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


@pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
def test_without_course_author_it_is_403(client, db_session, tenant, role):
    cur = _curriculum(db_session, CurriculumStatus.ready)
    with acting_as(_user(db_session, role)):
        resp = client.post(f"/curricula/{cur.id}/generate-course", json=BODY)
    assert resp.status_code == 403, resp.text
    assert "course:author" in resp.text


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin])
def test_authors_get_past_the_permission_check(client, db_session, tenant, role):
    cur = _curriculum(db_session)  # not ready: refused with 409 before any AI call
    with acting_as(_user(db_session, role)):
        resp = client.post(f"/curricula/{cur.id}/generate-course", json=BODY)
    assert resp.status_code == 409, resp.text
