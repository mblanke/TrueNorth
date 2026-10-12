"""The APIs behind the R0-B essential operator screens: team membership and a Student's
course progress and completion (the range recovery and Moodle results screens are tested
in test_range_operations.py and test_moodle_results.py).

Each screen is role-gated in the UI, but the UI is not the boundary: these check the
permission (403), the tenant (404, never 403), and the audit entry for every change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from _shared import DEV_TENANT, OTHER_TENANT, act_as, acting_as, real_tenant, real_user
from app.auth import CurrentUser
from app.models import (
    AuditLog,
    Course,
    CourseModule,
    Enrollment,
    ModuleContentType,
    ModuleProgress,
    ModuleProgressStatus,
    Team,
    TeamMembership,
    User,
    UserRole,
)


def _user(db, tenant: str = DEV_TENANT, role: UserRole = UserRole.student, name: str = "Student") -> User:
    uid = uuid.uuid4()
    u = User(id=uid, keycloak_id=f"kc-{uid}", email=f"{uid.hex[:10]}@example.test", display_name=name, role=role,
             tenant_id=uuid.UUID(tenant))
    db.add(u)
    db.flush()
    return u


def _audits(db, action: str, resource_id: str) -> list[AuditLog]:
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.action == action, AuditLog.resource_id == resource_id).all()


# ── team membership ──────────────────────────────────────────────────
@pytest.fixture
def team(db_session):
    t = Team(id=uuid.uuid4(), name="Blue Cell", tenant_id=uuid.UUID(DEV_TENANT), max_members=2)
    db_session.add(t)
    db_session.commit()
    return t


class TestTeamMembership:
    def test_add_list_remove_with_audit(self, client, db_session, team):
        alice = _user(db_session, name="Alice")
        db_session.commit()
        resp = client.post(f"/teams/{team.id}/members", params={"user_id": str(alice.id), "role": "lead"})
        assert resp.status_code == 201, resp.text
        assert resp.json() == {"user_id": str(alice.id), "team_id": str(team.id), "role": "lead"}
        [added] = _audits(db_session, "add_member", str(team.id))
        assert str(alice.id) in added.detail and "lead" in added.detail

        members = client.get(f"/teams/{team.id}/members").json()
        assert [(m["display_name"], m["role"]) for m in members] == [("Alice", "lead")]
        assert members[0]["joined_at"]

        assert client.post(f"/teams/{team.id}/members", params={"user_id": str(alice.id)}).status_code == 409
        assert client.delete(f"/teams/{team.id}/members/{alice.id}").status_code == 204
        [removed] = _audits(db_session, "remove_member", str(team.id))
        assert str(alice.id) in removed.detail
        assert client.get(f"/teams/{team.id}/members").json() == []
        assert client.delete(f"/teams/{team.id}/members/{alice.id}").status_code == 404

    def test_a_full_team_refuses_another_member(self, client, db_session, team):
        people = [_user(db_session) for _ in range(3)]
        db_session.commit()
        for p in people[:2]:
            assert client.post(f"/teams/{team.id}/members", params={"user_id": str(p.id)}).status_code == 201
        resp = client.post(f"/teams/{team.id}/members", params={"user_id": str(people[2].id)})
        assert resp.status_code == 409 and "full" in resp.json()["detail"]

    def test_a_deleted_member_no_longer_breaks_the_list(self, client, db_session, team):
        """Before: one membership whose account was soft-deleted made the list a 404."""
        gone = _user(db_session, name="Gone")
        here = _user(db_session, name="Here")
        db_session.add_all([TeamMembership(user_id=gone.id, team_id=team.id), TeamMembership(user_id=here.id,
                                                                                              team_id=team.id)])
        gone.deleted_at = datetime.now(UTC)
        db_session.commit()
        resp = client.get(f"/teams/{team.id}/members")
        assert resp.status_code == 200, resp.text
        assert sorted(m["display_name"] for m in resp.json()) == ["Here", "Unknown"]

    def test_an_instructor_reads_but_cannot_change_membership(self, client, db_session, team):
        alice = _user(db_session)
        db_session.commit()
        with acting_as(UserRole.instructor):
            assert client.get(f"/teams/{team.id}/members").status_code == 200
            assert client.post(f"/teams/{team.id}/members", params={"user_id": str(alice.id)}).status_code == 403
            assert client.delete(f"/teams/{team.id}/members/{alice.id}").status_code == 403
        for role in (UserRole.student, UserRole.observer, UserRole.range_ops):
            with acting_as(role):
                assert client.get(f"/teams/{team.id}/members").status_code == 403, role

    def test_another_tenants_user_cannot_be_added_and_their_admin_sees_nothing(self, client, db_session, team):
        other = str(real_tenant(db_session).id)
        stranger = _user(db_session, other)
        mine = _user(db_session)
        db_session.commit()
        resp = client.post(f"/teams/{team.id}/members", params={"user_id": str(stranger.id)})
        assert resp.status_code == 404 and "User not found" in resp.json()["detail"]
        assert client.post(f"/teams/{team.id}/members", params={"user_id": str(mine.id)}).status_code == 201
        with acting_as(UserRole.admin, other):
            assert client.get(f"/teams/{team.id}/members").status_code == 404
            assert client.delete(f"/teams/{team.id}/members/{mine.id}").status_code == 404
            assert client.post(f"/teams/{team.id}/members", params={"user_id": str(stranger.id)}).status_code == 404
        assert db_session.query(TeamMembership).filter_by(team_id=team.id).count() == 1


# ── a Student's course progress and completion ───────────────────────────
@pytest.fixture
def enrolled(db_session):
    course = Course(id=uuid.uuid4(), name="SOC Analyst I", tenant_id=uuid.UUID(DEV_TENANT), is_published=True)
    db_session.add(course)
    db_session.flush()
    mods = [CourseModule(id=uuid.uuid4(), course_id=course.id, ordinal=i, title=f"M{i}",
                         content_type=ModuleContentType.reading) for i in (1, 2)]
    db_session.add_all(mods)
    student = _user(db_session, name="Sam")
    enrollment = Enrollment(id=uuid.uuid4(), user_id=student.id, course_id=course.id)
    db_session.add(enrollment)
    db_session.flush()
    db_session.add_all([
        ModuleProgress(enrollment_id=enrollment.id, module_id=mods[0].id, status=ModuleProgressStatus.completed,
                       score=90, max_score=100, attempts=1),
        ModuleProgress(enrollment_id=enrollment.id, module_id=mods[1].id, status=ModuleProgressStatus.in_progress,
                       score=50, max_score=100, attempts=2),
    ])
    db_session.commit()
    return course, student, enrollment


class TestStudentProgress:
    def test_an_instructor_reads_a_students_progress_and_marks_it_complete_audited(self, client, db_session,
                                                                                  enrolled):
        course, student, enrollment = enrolled
        me = real_user(db_session, UserRole.instructor, DEV_TENANT)
        db_session.commit()
        act_as(me)
        progress = client.get(f"/courses/{course.id}/progress/{student.id}")
        assert progress.status_code == 200
        assert sorted((p["status"], p["score"]) for p in progress.json()) == [("completed", 90), ("in_progress", 50)]
        done = client.post(f"/courses/{course.id}/complete/{student.id}")
        assert done.status_code == 200, done.text
        body = done.json()
        assert body["status"] == "completed" and body["final_score"] == 140 and body["final_grade"] == "C"
        [entry] = _audits(db_session, "course_complete", str(enrollment.id))
        assert str(entry.user_id) == me.id and str(student.id) in entry.detail and "grade C" in entry.detail

    def test_a_student_completing_their_own_course_is_not_staff_audited(self, client, db_session, enrolled):
        course, student, enrollment = enrolled
        act_as(CurrentUser(id=str(student.id), email=student.email, display_name="Sam", role=UserRole.student,
                           tenant_id=DEV_TENANT, keycloak_id=student.keycloak_id))
        assert client.post(f"/courses/{course.id}/complete/{student.id}").status_code == 200
        assert _audits(db_session, "course_complete", str(enrollment.id)) == []

    def test_roles_without_learning_records_are_refused(self, client, db_session, enrolled):
        course, student, enrollment = enrolled
        for role in (UserRole.observer, UserRole.range_ops, UserRole.student):
            with acting_as(role):
                assert client.get(f"/courses/{course.id}/progress/{student.id}").status_code == 403, role
                assert client.post(f"/courses/{course.id}/complete/{student.id}").status_code == 403, role
        db_session.refresh(enrollment)
        assert enrollment.completed_at is None
        assert _audits(db_session, "course_complete", str(enrollment.id)) == []

    def test_another_tenants_staff_get_404(self, client, db_session, enrolled):
        course, student, enrollment = enrolled
        with acting_as(UserRole.admin, OTHER_TENANT):
            assert client.get(f"/courses/{course.id}/progress/{student.id}").status_code == 404
            assert client.post(f"/courses/{course.id}/complete/{student.id}").status_code == 404
        db_session.refresh(enrollment)
        assert enrollment.completed_at is None

    def test_no_enrollment_is_404_not_an_empty_record(self, client, db_session, enrolled):
        course, _, _ = enrolled
        other = _user(db_session)
        db_session.commit()
        assert client.get(f"/courses/{course.id}/progress/{other.id}").status_code == 404
        assert client.post(f"/courses/{course.id}/complete/{other.id}").status_code == 404
