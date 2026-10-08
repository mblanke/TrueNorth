"""Tenant isolation and permissions closed by the 2026-10-07 tenancy follow-up.

- ``GET /exercises/{id}/objectives`` and ``POST .../objectives/{ref}/ack`` read and
  wrote any tenant's objectives by exercise id.
- ``/adaptive/users/{user_id}/*`` returned anyone's scores and competency trend to any
  holder of ``exercise:read`` (every Student).
- ``/audit-log`` returned every tenant's entries to every holder of ``audit:read``.
- QSP spine imports / generators and ``/courses/import-*`` needed only a login.

(The hypervisor-connection fixes are tested in ``test_hypervisors_vsphere.py``.)

Seeding follows ``test_platform_tenancy.py``: a second tenant's rows go straight into
the session; foreign ids are 404, a permission the caller's role lacks is 403.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    AuditLog,
    CompetencyAutoAssessment,
    Exercise,
    ExerciseState,
    Objective,
    ObjectiveType,
    User,
    UserRole,
)

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, *, tenant: str = DEV_TENANT, user_id: uuid.UUID | None = None):
    who = CurrentUser(
        id=str(user_id or uuid.uuid4()),
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _user(db, tenant: str, role: UserRole = UserRole.student) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="someone",
        role=role,
        tenant_id=uuid.UUID(tenant),
    )
    db.add(u)
    db.flush()
    return u


def _exercise(db, tenant: str) -> Exercise:
    ex = Exercise(id=uuid.uuid4(), name=f"ex-{tenant[-2:]}", range_id=uuid.uuid4(), tenant_id=uuid.UUID(tenant))
    db.add(ex)
    db.flush()
    db.add(
        Objective(
            id=uuid.uuid4(),
            exercise_id=ex.id,
            ref_id="OBJ-1",
            objective_type=ObjectiveType.deliverable,
            description="secret objective",
            validator="manual_ack",
            points=10,
        )
    )
    db.flush()
    return ex


def _assessment(db, user_id: uuid.UUID) -> None:
    db.add(
        CompetencyAutoAssessment(
            id=uuid.uuid4(),
            user_id=user_id,
            exercise_id=uuid.uuid4(),
            competency_mappings=json.dumps([{"category": "network_defense", "delta": 5}]),
            raw_score=80,
            max_score=100,
            assessed_at=datetime.now(UTC),
        )
    )
    db.flush()


# ── Exercise objectives ────────────────────────────────────────────────
class TestExerciseObjectives:
    def test_foreign_exercise_objectives_are_404(self, client, db_session):
        theirs = _exercise(db_session, OTHER_TENANT)
        db_session.commit()
        assert client.get(f"/exercises/{theirs.id}/objectives").status_code == 404
        r = client.post(f"/exercises/{theirs.id}/objectives/OBJ-1/ack")
        assert r.status_code == 404
        obj = db_session.query(Objective).filter_by(exercise_id=theirs.id).one()
        db_session.refresh(obj)
        assert obj.achieved is False

    def test_own_exercise_objectives_are_200(self, client, db_session):
        mine = _exercise(db_session, DEV_TENANT)
        mine.state = ExerciseState.running  # objectives are acknowledged on a live exercise (ADR 0005 §4)
        db_session.commit()
        r = client.get(f"/exercises/{mine.id}/objectives")
        assert r.status_code == 200, r.text
        assert [o["ref_id"] for o in r.json()] == ["OBJ-1"]
        assert client.post(f"/exercises/{mine.id}/objectives/OBJ-1/ack").status_code == 200


# ── Adaptive learning records ──────────────────────────────────────────
ADAPTIVE_READS = [
    "/adaptive/users/{uid}/progress",
    "/adaptive/users/{uid}/auto-assessments",
    "/adaptive/users/{uid}/recommendations",
]


class TestAdaptiveRecords:
    @pytest.mark.parametrize("path", ADAPTIVE_READS)
    def test_foreign_tenant_user_is_404_even_for_staff(self, client, db_session, path):
        foreigner = _user(db_session, OTHER_TENANT)
        _assessment(db_session, foreigner.id)
        db_session.commit()
        with acting_as(UserRole.instructor):
            assert client.get(path.format(uid=foreigner.id)).status_code == 404
        # the dev admin too: a learning record is not platform config
        assert client.get(path.format(uid=foreigner.id)).status_code == 404

    @pytest.mark.parametrize("path", ADAPTIVE_READS)
    def test_same_tenant_staff_is_200(self, client, db_session, path):
        student = _user(db_session, DEV_TENANT)
        _assessment(db_session, student.id)
        db_session.commit()
        with acting_as(UserRole.instructor):
            assert client.get(path.format(uid=student.id)).status_code == 200

    def test_student_reads_own_progress_but_not_a_classmates(self, client, db_session):
        me = _user(db_session, DEV_TENANT)
        classmate = _user(db_session, DEV_TENANT)
        _assessment(db_session, me.id)
        _assessment(db_session, classmate.id)
        db_session.commit()
        with acting_as(UserRole.student, user_id=me.id):
            r = client.get(f"/adaptive/users/{me.id}/progress")
            assert r.status_code == 200 and r.json()["total_exercises"] == 1
            assert client.get(f"/adaptive/users/{classmate.id}/progress").status_code == 403

    def test_trigger_recommendation_for_foreign_user_is_404(self, client, db_session):
        foreigner = _user(db_session, OTHER_TENANT)
        db_session.commit()
        with acting_as(UserRole.instructor):
            assert client.post(f"/adaptive/users/{foreigner.id}/recommendations").status_code == 404


# ── Audit log ──────────────────────────────────────────────────────────
class TestAuditLogScope:
    @pytest.fixture
    def entries(self, db_session):
        for tenant, rid in ((DEV_TENANT, "mine"), (OTHER_TENANT, "theirs")):
            db_session.add(
                AuditLog(tenant_id=uuid.UUID(tenant), action="test", resource_type="probe", resource_id=rid)
            )
        db_session.commit()

    def test_holder_of_audit_read_without_platform_rights_sees_own_tenant(self, client, entries, monkeypatch):
        from app import rbac

        # No shipped role has audit:read without tenant:read; model a tenant auditor.
        monkeypatch.setitem(
            rbac.ROLE_PERMISSIONS, UserRole.observer, rbac.ROLE_PERMISSIONS[UserRole.observer] | {rbac.Permission.AUDIT_READ}
        )
        with acting_as(UserRole.observer):
            r = client.get("/audit-log")
        assert r.status_code == 200, r.text
        rids = {e["resource_id"] for e in r.json() if e["resource_type"] == "probe"}
        assert rids == {"mine"}

    def test_platform_admin_sees_every_tenant(self, client, entries):
        rids = {e["resource_id"] for e in client.get("/audit-log").json() if e["resource_type"] == "probe"}
        assert rids == {"mine", "theirs"}

    def test_writers_record_the_tenant(self, client, db_session):
        r = client.post("/teams", json={"name": "Audit Probe Team"})
        assert r.status_code in (200, 201), r.text
        row = db_session.query(AuditLog).filter_by(resource_type="team").order_by(AuditLog.timestamp.desc()).first()
        assert row is not None and str(row.tenant_id) == DEV_TENANT


# ── Import / generation permissions ────────────────────────────────────
AUTHORING_POSTS = [
    "/qsp/import-crosswalk",
    "/qsp/import-competency-crosswalk",
    "/qsp/generate-learning-paths",
    "/qsp/generate-exercises",
    "/courses/import-programme",
    "/courses/import-course-content",
    "/courses/generate-programme-paths",
]


class TestAuthoringNeedsCourseAuthor:
    @pytest.mark.parametrize("path", AUTHORING_POSTS)
    @pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.range_ops])
    def test_non_authors_are_403(self, client, path, role):
        with acting_as(role):
            assert client.post(path).status_code == 403

    @pytest.mark.parametrize("path", ["/qsp/generate-learning-paths", "/courses/generate-programme-paths"])
    def test_instructor_may_generate(self, client, path):
        with acting_as(UserRole.instructor):
            assert client.post(path).status_code == 200

    def test_competency_framework_import_needs_course_author(self, client):
        with acting_as(UserRole.student):
            assert client.post("/competency/frameworks/import-nice").status_code == 403


# ── Shared qualification spine ─────────────────────────────────────────
SPINE_IMPORTS = ["/qsp/import-crosswalk", "/qsp/import-competency-crosswalk"]


class TestSpineImportIsPlatformLevel:
    """``Qualification.qsp_code`` is unique across tenants and the imports upsert by it,
    so one tenant's course author could rewrite every tenant's POs/EOs."""

    @pytest.mark.parametrize("path", SPINE_IMPORTS)
    @pytest.mark.parametrize("role", [UserRole.instructor, UserRole.student])
    def test_tenant_level_roles_are_403(self, client, path, role):
        with acting_as(role):
            assert client.post(path).status_code == 403

    @pytest.mark.parametrize("path", SPINE_IMPORTS)
    def test_platform_admin_gets_past_the_permission_check(self, client, path):
        # No upload attached: 422 (validation), i.e. authorised and handed to the handler.
        assert client.post(path).status_code == 422


# ── Course and learning-path writes ────────────────────────────────────
class TestCatalogueWritesNeedCourseAuthor:
    def test_student_cannot_create_update_or_delete(self, client, db_session):
        # Published: a Student never sees drafts (test_course_catalogue_visibility.py).
        mine = client.post("/courses", json={"name": "Owned Course", "is_published": True})
        assert mine.status_code == 201, mine.text
        cid = mine.json()["id"]
        lp = client.post("/learning-paths", json={"name": "Owned Path"})
        assert lp.status_code == 201, lp.text
        lpid = lp.json()["id"]
        with acting_as(UserRole.student):
            assert client.post("/courses", json={"name": "Student Course"}).status_code == 403
            assert client.patch(f"/courses/{cid}", json={"name": "x"}).status_code == 403
            assert client.delete(f"/courses/{cid}").status_code == 403
            assert client.post("/learning-paths", json={"name": "Student Path"}).status_code == 403
            assert client.patch(f"/learning-paths/{lpid}", json={"name": "x"}).status_code == 403
            assert client.delete(f"/learning-paths/{lpid}").status_code == 403
            # reading the catalogue is unaffected
            assert client.get(f"/courses/{cid}").status_code == 200
        assert client.get(f"/courses/{cid}").json()["name"] == "Owned Course"

    def test_instructor_may_author(self, client):
        with acting_as(UserRole.instructor):
            r = client.post("/courses", json={"name": "Instructor Course"})
            assert r.status_code == 201, r.text
            cid = r.json()["id"]
            assert client.patch(f"/courses/{cid}", json={"name": "Renamed"}).status_code == 200
            assert client.delete(f"/courses/{cid}").status_code == 204
