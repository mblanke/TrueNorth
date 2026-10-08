"""Students do not start, close or replay a team exercise (security sweep M3).

Students held exercise:start and exercise:complete. /run on a completed exercise reset
every participant's objectives and score, so one Student could wipe the class's results;
/complete let one close the exercise on everyone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.models import Exercise, ExerciseState, Objective, ObjectiveType, UserRole


@pytest.fixture
def scored(db_session):
    t = real_tenant(db_session, "m3")
    ex = real_exercise(db_session, t.id, state=ExerciseState.completed, started_at=datetime(2026, 10, 1, tzinfo=UTC),
                       completed_at=datetime(2026, 10, 1, 2, tzinfo=UTC), total_score=40, max_score=40)
    db_session.add(Objective(exercise_id=ex.id, ref_id="o", objective_type=ObjectiveType.detection, description="o",
                             validator="opensearch_query", points=40, achieved=True, evidence='{"source": "x"}'))
    db_session.flush()
    return t, ex


def _objective(db, ex) -> Objective:
    db.expire_all()
    return db.query(Objective).filter(Objective.exercise_id == ex.id).one()


@pytest.mark.parametrize("path", ["run", "run?reset=true", "start", "complete"])
def test_a_student_cannot_start_replay_or_close(client, db_session, scored, path):
    t, ex = scored
    act_as(real_user(db_session, UserRole.student, t.id))
    assert client.post(f"/exercises/{ex.id}/{path}").status_code == 403
    assert _objective(db_session, ex).achieved is True
    assert db_session.get(Exercise, ex.id).total_score == 40


def test_run_on_a_completed_exercise_needs_an_explicit_reset(client, db_session, scored):
    t, ex = scored
    act_as(real_user(db_session, UserRole.instructor, t.id))

    r = client.post(f"/exercises/{ex.id}/run")
    assert r.status_code == 409 and "reset=true" in r.json()["detail"]
    assert _objective(db_session, ex).achieved is True

    assert client.post(f"/exercises/{ex.id}/run", params={"reset": "true"}).status_code == 200
    assert _objective(db_session, ex).achieved is False


def test_a_student_cannot_close_a_running_exercise(client, db_session):
    t = real_tenant(db_session, "m3-run")
    ex = real_exercise(db_session, t.id, state=ExerciseState.running, started_at=datetime.now(UTC))
    act_as(real_user(db_session, UserRole.student, t.id))
    assert client.post(f"/exercises/{ex.id}/complete").status_code == 403
    db_session.expire_all()
    assert db_session.get(Exercise, ex.id).state == ExerciseState.running
