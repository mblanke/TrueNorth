"""Exercise results credit the Students, never the instructor (#375).

An exercise has no owner column; its Students come from what the class records:
- an individual exercise on a Student's **seat range** (``class_ranges`` kind seat → roll call);
- a team exercise: every member of the **teams** whose ``exercise_id`` is the exercise (owner
  decision 2026-11-03: credit every participant).

Objective sign-off then emits ``passed`` for those Students with the instructor as
``assessed_by`` (and nothing when there are none); completion's participants are those
Students and the detection submitters; an instructor who closes it is not a participant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.classes.models import Class, ClassRange, RollCallEntry
from app.models import Course, ExerciseState, Objective, ObjectiveType, Range, Team, TeamMembership, UserRole

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def recorded(monkeypatch):
    """Capture xAPI statements, worker dispatches and LTI pushes instead of sending them."""
    from app import celery_client, xapi
    from app.routers import exercises

    calls: dict[str, list] = {"xapi": [], "tasks": [], "lti": []}

    def emit(_bt, **kw):
        calls["xapi"].append(kw)

    async def lti(*a):
        calls["lti"].append(a)

    monkeypatch.setattr(exercises, "emit_lifecycle", emit)
    monkeypatch.setattr(xapi, "emit_lifecycle", emit)
    monkeypatch.setattr(celery_client, "dispatch", lambda *a: calls["tasks"].append(a) or "id")
    monkeypatch.setattr(exercises, "_push_exercise_lti_grade", lti)
    return calls


@pytest.fixture
def world(db_session):
    tenant = real_tenant(db_session)
    instructor = real_user(db_session, UserRole.instructor, tenant.id)
    students = [real_user(db_session, UserRole.student, tenant.id) for _ in range(3)]
    course = Course(name="C201", tenant_id=tenant.id)
    db_session.add(course)
    db_session.flush()
    cls = Class(tenant_id=tenant.id, name="C201-26-03", course_id=course.id,
                lead_instructor_id=uuid.UUID(instructor.id), start_date=(NOW - timedelta(days=1)).date(),
                end_date=(NOW + timedelta(days=4)).date(), status="open")
    db_session.add(cls)
    db_session.flush()
    entries = []
    for s in students:
        e = RollCallEntry(class_id=cls.id, tenant_id=tenant.id, email=s.email, user_id=uuid.UUID(s.id),
                          status="signed_in", signed_in_at=NOW)
        db_session.add(e)
        entries.append(e)
    db_session.flush()
    return {"tenant": tenant, "instructor": instructor, "students": students, "cls": cls, "entries": entries}


def _exercise(db, world, state=ExerciseState.running):
    ex = real_exercise(db, world["tenant"].id, state=state, started_at=NOW - timedelta(hours=1))
    db.add(Objective(exercise_id=ex.id, ref_id="OBJ-1", objective_type=ObjectiveType.detection,
                     validator="manual_ack", points=10))
    db.flush()
    return ex


def _seat(db, world, ex, who: int = 0):
    entry = world["entries"][who]
    db.add(ClassRange(tenant_id=world["tenant"].id, class_id=world["cls"].id,
                      template_id=db.get(Range, ex.range_id).template_id, kind="seat", entry_id=entry.id,
                      seat_key=str(entry.id), range_id=ex.range_id, builds=1, built_at=NOW))
    db.flush()


def _team(db, world, ex, members: list[int]):
    team = Team(name=f"team-{uuid.uuid4().hex[:4]}", tenant_id=world["tenant"].id, exercise_id=ex.id)
    db.add(team)
    db.flush()
    for i in members:
        db.add(TeamMembership(user_id=uuid.UUID(world["students"][i].id), team_id=team.id))
    db.flush()


def _ids(world, *idx) -> set[str]:
    return {world["students"][i].id for i in idx}


def _passed(recorded) -> list[dict]:
    return [s for s in recorded["xapi"] if s["verb_key"] == "passed"]


def _completed(recorded) -> set[str]:
    return {str(s["user_id"]) for s in recorded["xapi"] if s["verb_key"] == "completed"}


class TestSignOff:
    def test_a_seat_exercise_credits_its_student_not_the_instructor(self, client, db_session, world, recorded):
        ex = _exercise(db_session, world)
        _seat(db_session, world, ex, who=0)
        db_session.commit()
        act_as(world["instructor"])
        assert client.post(f"/exercises/{ex.id}/objectives/OBJ-1/ack").status_code == 200
        passed = _passed(recorded)
        assert {str(s["user_id"]) for s in passed} == _ids(world, 0)
        assert passed[0]["context_extensions"]["assessed_by"] == world["instructor"].id

    def test_a_team_exercise_credits_every_participant(self, client, db_session, world, recorded):
        ex = _exercise(db_session, world)
        _team(db_session, world, ex, [0, 1])
        _team(db_session, world, ex, [2])
        db_session.commit()
        act_as(world["instructor"])
        client.post(f"/exercises/{ex.id}/objectives/OBJ-1/ack")
        assert {str(s["user_id"]) for s in _passed(recorded)} == _ids(world, 0, 1, 2)

    def test_an_exercise_with_no_students_credits_no_one(self, client, db_session, world, recorded):
        ex = _exercise(db_session, world)
        db_session.commit()
        act_as(world["instructor"])
        assert client.post(f"/exercises/{ex.id}/objectives/OBJ-1/ack").status_code == 200
        assert _passed(recorded) == []

    def test_a_student_taken_off_the_roll_call_is_not_credited(self, client, db_session, world, recorded):
        ex = _exercise(db_session, world)
        _seat(db_session, world, ex, who=0)
        world["entries"][0].status = "removed"
        db_session.commit()
        act_as(world["instructor"])
        client.post(f"/exercises/{ex.id}/objectives/OBJ-1/ack")
        assert _passed(recorded) == []


class TestCompletion:
    def test_an_instructor_completing_a_seat_exercise_credits_the_student(self, client, db_session, world, recorded):
        ex = _exercise(db_session, world)
        _seat(db_session, world, ex, who=1)
        db_session.commit()
        act_as(world["instructor"])
        assert client.post(f"/exercises/{ex.id}/complete").status_code == 200
        assert _completed(recorded) == _ids(world, 1)
        assert {str(a[0]) for a in recorded["lti"]} == _ids(world, 1)
        assert {a[2] for a in recorded["tasks"] if a[0] == "auto_assess_competency"} == _ids(world, 1)

    def test_the_clock_closing_an_exercise_credits_its_students(self, db_session, world, recorded):
        from app.exercise_completion import _RunNow, close

        ex = _exercise(db_session, world)
        _seat(db_session, world, ex, who=2)
        db_session.commit()
        assert close(db_session, ex.id, _RunNow(), None) is not None
        assert _completed(recorded) == _ids(world, 2)

    def test_a_team_exercise_completes_for_every_participant(self, client, db_session, world, recorded):
        ex = _exercise(db_session, world)
        _team(db_session, world, ex, [0, 2])
        db_session.commit()
        act_as(world["instructor"])
        client.post(f"/exercises/{ex.id}/complete")
        assert _completed(recorded) == _ids(world, 0, 2)
