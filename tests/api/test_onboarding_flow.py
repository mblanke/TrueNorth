"""Post-approval first-run flow, and the self-service profile endpoint.

The reason `/onboarding/profile` exists at all is covered here: a `student`
holds no `USER_UPDATE` permission, so a trainee genuinely cannot complete their
own profile through the admin router.
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Course,
    CourseModule,
    Enrollment,
    LearningPath,
    ModuleContentType,
    ModuleProgress,
    Tenant,
    User,
    UserRole,
)
from app.rbac import ROLE_PERMISSIONS, Permission


@pytest.fixture
def tenant(db_session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Default Org", slug="default")
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture
def trainee(db_session, tenant):
    """A freshly approved trainee, mid-onboarding."""
    row = User(
        id=uuid.uuid4(),
        keycloak_id="ad-sub-trainee",
        email="trainee@corp.tnrange.lab",
        display_name="Joe Bloggs",
        role=UserRole.student,
        tenant_id=tenant.id,
        source="ad",
        onboarding_state="not_started",
    )
    db_session.add(row)
    db_session.flush()
    cu = CurrentUser(
        id=str(row.id),
        email=row.email,
        display_name=row.display_name,
        role=UserRole.student,
        tenant_id=str(tenant.id),
        keycloak_id=row.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: cu
    yield row
    fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_student_role_cannot_update_users_which_is_why_this_router_exists():
    assert Permission.USER_UPDATE not in ROLE_PERMISSIONS[UserRole.student]


def test_initial_state_is_not_started(client, trainee):
    body = client.get("/onboarding/state").json()
    assert body["state"] == "not_started"
    assert body["steps_done"] == []
    assert "rank" in body["missing_profile_fields"]


def test_profile_completion_updates_only_whitelisted_fields(client, trainee, db_session):
    r = client.post(
        "/onboarding/profile",
        json={"rank": "Cpl", "unit": "2 CMBG", "callsign": "BLOGGS", "timezone": "America/Toronto"},
    )
    assert r.status_code == 200, r.text
    db_session.refresh(trainee)
    assert trainee.rank == "Cpl"
    assert trainee.unit == "2 CMBG"
    assert trainee.timezone == "America/Toronto"
    assert "profile" in r.json()["steps_done"]


def test_profile_cannot_escalate_role_or_tenant(client, trainee, db_session):
    """Unknown keys are ignored by the schema — a trainee may describe, not promote."""
    original_role, original_tenant = trainee.role, trainee.tenant_id
    r = client.post(
        "/onboarding/profile",
        json={"rank": "Cpl", "role": "admin", "tenant_id": str(uuid.uuid4()), "is_active": False},
    )
    assert r.status_code == 200
    db_session.refresh(trainee)
    assert trainee.role == original_role
    assert trainee.tenant_id == original_tenant
    assert trainee.is_active is True


def test_select_path_enrolls_and_is_idempotent(client, trainee, db_session, tenant):
    course = Course(id=uuid.uuid4(), name="C101", tenant_id=tenant.id)
    db_session.add(course)
    db_session.flush()
    db_session.add(
        CourseModule(
            id=uuid.uuid4(),
            course_id=course.id,
            ordinal=0,
            title="M1",
            content_type=ModuleContentType.reading,
        )
    )
    path = LearningPath(
        id=uuid.uuid4(),
        name="DP1",
        tenant_id=tenant.id,
        course_ids=json.dumps([str(course.id)]),
    )
    db_session.add(path)
    db_session.flush()

    first = client.post("/onboarding/select-path", json={"learning_path_id": str(path.id)})
    assert first.status_code == 200, first.text
    assert first.json()["enrolled_course_count"] == 1

    # Re-running must not double-enroll: ensure_enrollment returns the existing row.
    second = client.post("/onboarding/select-path", json={"learning_path_id": str(path.id)})
    assert second.json()["enrolled_course_count"] == 1
    assert db_session.query(Enrollment).filter(Enrollment.user_id == trainee.id).count() == 1
    assert db_session.query(ModuleProgress).count() == 1


def test_select_path_requires_a_target(client, trainee):
    assert client.post("/onboarding/select-path", json={}).status_code == 400


def test_select_path_rejects_unknown_path(client, trainee):
    r = client.post("/onboarding/select-path", json={"learning_path_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_complete_marks_onboarded(client, trainee, db_session):
    r = client.post("/onboarding/complete")
    assert r.status_code == 200
    assert r.json()["state"] == "complete"
    assert r.json()["onboarded_at"] is not None
    db_session.refresh(trainee)
    assert trainee.onboarding_state == "complete"


def test_skip_exists_so_an_admin_is_not_trapped(client, trainee, db_session):
    r = client.post("/onboarding/skip")
    assert r.status_code == 200
    assert r.json()["state"] == "complete"
    db_session.refresh(trainee)
    assert json.loads(trainee.onboarding_data)["skipped"] is True
