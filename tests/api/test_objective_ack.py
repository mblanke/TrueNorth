"""POST /exercises/{id}/objectives/{ref_id}/ack — instructor-only and tenant-scoped (ADR 0005 §4).

Acknowledging an objective awards its points. It used to require exercise:complete, which
Students hold, so a Student could award themselves any objective; and the objective was
looked up by exercise id alone, so it reached across tenants.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Exercise, ExerciseState, Objective, ObjectiveType, UserRole
from app.rbac import ROLE_PERMISSIONS, Permission

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT, name: str | None = None):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@example.test",
        display_name=name or f"Test {role.value}",
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _exercise(db, *, tenant: str = DEV_TENANT, state: ExerciseState = ExerciseState.running) -> Exercise:
    ex = Exercise(
        id=uuid.uuid4(),
        name=f"ex-{uuid.uuid4().hex[:6]}",
        range_id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant),
        state=state,
        max_score=100,
        total_score=0,
    )
    db.add(ex)
    for ref, points, achieved in (("obj-a", 60, False), ("obj-b", 25, True)):
        db.add(
            Objective(
                exercise_id=ex.id,
                ref_id=ref,
                objective_type=ObjectiveType.detection,
                description=f"Objective {ref}",
                validator="manual",
                points=points,
                achieved=achieved,
            )
        )
    db.commit()
    return ex


def _ack(client, ex_id, ref="obj-a", evidence: str | None = None):
    params = {"evidence": evidence} if evidence is not None else None
    return client.post(f"/exercises/{ex_id}/objectives/{ref}/ack", params=params)


def _objective(db, ex_id, ref="obj-a") -> Objective:
    db.expire_all()
    return db.query(Objective).filter(Objective.exercise_id == ex_id, Objective.ref_id == ref).one()


class TestPermissionMap:
    def test_only_instructor_and_admin_hold_objective_ack(self):
        holders = {role for role, perms in ROLE_PERMISSIONS.items() if Permission.OBJECTIVE_ACK in perms}
        assert holders == {UserRole.admin, UserRole.instructor}

    def test_students_neither_start_nor_complete_team_exercises(self):
        """Security sweep M3 (2026-10-08) reversed PR #58's choice: staff run and close a
        team exercise; a Student's own lab has its own lifecycle (app/lab_sessions)."""
        assert Permission.EXERCISE_COMPLETE not in ROLE_PERMISSIONS[UserRole.student]
        assert Permission.EXERCISE_START not in ROLE_PERMISSIONS[UserRole.student]


class TestAcknowledgeObjective:
    def test_student_is_refused(self, client, db_session):
        ex = _exercise(db_session)
        with acting_as(UserRole.student):
            resp = _ack(client, ex.id, evidence="I did it")
        assert resp.status_code == 403
        assert "objective:ack" in resp.text
        assert _objective(db_session, ex.id).achieved is False

    @pytest.mark.parametrize("role", [UserRole.observer, UserRole.range_ops])
    def test_other_non_instructor_roles_are_refused(self, client, db_session, role):
        ex = _exercise(db_session)
        with acting_as(role):
            assert _ack(client, ex.id).status_code == 403

    def test_instructor_in_another_tenant_gets_404(self, client, db_session):
        ex = _exercise(db_session, tenant=DEV_TENANT)
        with acting_as(UserRole.instructor, tenant=OTHER_TENANT):
            resp = _ack(client, ex.id)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Exercise not found"
        assert _objective(db_session, ex.id).achieved is False

    def test_instructor_in_tenant_acknowledges_and_score_is_retotalled(self, client, db_session):
        ex = _exercise(db_session)
        with acting_as(UserRole.instructor, name="Sgt Rivera"):
            resp = _ack(client, ex.id, evidence="Saw the beacon in Zeek")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["achieved"] is True
        assert body["evidence"] == "Acknowledged by Sgt Rivera: Saw the beacon in Zeek"
        assert body["achieved_at"]
        db_session.expire_all()
        assert db_session.get(Exercise, ex.id).total_score == 85  # 60 just acked + 25 already achieved

    def test_evidence_is_optional(self, client, db_session):
        ex = _exercise(db_session, state=ExerciseState.paused)
        with acting_as(UserRole.instructor, name="Sgt Rivera"):
            resp = _ack(client, ex.id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["evidence"] == "Acknowledged by Sgt Rivera"

    @pytest.mark.parametrize("state", [ExerciseState.completed, ExerciseState.pending])
    def test_exercise_not_running_or_paused_is_409(self, client, db_session, state):
        ex = _exercise(db_session, state=state)
        with acting_as(UserRole.instructor):
            resp = _ack(client, ex.id)
        assert resp.status_code == 409
        assert _objective(db_session, ex.id).achieved is False

    def test_already_achieved_is_409(self, client, db_session):
        ex = _exercise(db_session)
        with acting_as(UserRole.instructor):
            assert _ack(client, ex.id, ref="obj-b").status_code == 409

    def test_unknown_objective_is_404(self, client, db_session):
        ex = _exercise(db_session)
        with acting_as(UserRole.instructor):
            resp = _ack(client, ex.id, ref="no-such-objective")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Objective not found"
