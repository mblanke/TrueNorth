"""A self-enrolment is course content, never a seat in another class's booked exercise.

Review of R0-B (B1): self-enrolment on a shared course made any Student a participant of
every exercise booked for that course, because the rosters (exercise_participants,
telemetry_access) counted any active enrolment. Repro: an unrelated Student enrols
themselves on the shared course S, then reads the live telemetry of another class's
exercise booked on S, may submit detections (credit), and one junk detection made them a
participant of its AAR (other Students' names and evidence).

Now ``enrollments.source`` records ``self`` vs ``assigned`` and rosters count only
``assigned`` (app/enrollment.py). Also covered (S1, S2): a Student reads the AAR of the
run they took part in — by the roster, a team, or a detection submitted in *this* run.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import SCENARIO, FakeStore, act_as, beacon, real_exercise, real_tenant, real_user
from app.detections.models import DetectionSubmission
from app.enrollment import ASSIGNED, SELF, ensure_enrollment
from app.exercise_participants import may_submit
from app.main import app as fastapi_app
from app.models import (
    AfterActionReport,
    Course,
    Enrollment,
    Exercise,
    ExerciseState,
    LearningPath,
    Objective,
    ObjectiveType,
    UserRole,
)
from app.routers.detections import search_backend
from app.scheduler.models import EventState, ScheduledEvent
from app.telemetry_access import readable_telemetry_range
from fastapi import HTTPException

NOW = datetime.now(UTC)
RUN_START = NOW - timedelta(hours=1)


@pytest.fixture
def store():
    s = FakeStore(events=[beacon(), beacon()])
    fastapi_app.dependency_overrides[search_backend] = lambda: s
    yield s
    fastapi_app.dependency_overrides.pop(search_backend, None)


@pytest.fixture
def booked(db_session, monkeypatch):
    """Another class's exercise, running now, booked on the shared (global) course S."""
    monkeypatch.delenv("DETECTION_SUBMIT_PARTICIPANTS_ONLY", raising=False)
    t = real_tenant(db_session, "seat")
    ex = real_exercise(db_session, t.id, SCENARIO, state=ExerciseState.running, started_at=RUN_START,
                       max_score=40, total_score=0)
    db_session.add(Objective(exercise_id=ex.id, ref_id="detect_c2", objective_type=ObjectiveType.detection,
                             description="c2", validator="opensearch_query", points=40))
    shared = Course(id=uuid.uuid4(), name="Shared", tenant_id=None, is_published=True)
    db_session.add(shared)
    db_session.flush()
    db_session.add(ScheduledEvent(name="their lesson", tenant_id=t.id, course_id=shared.id, state=EventState.active,
                                  start_time=NOW - timedelta(hours=2), end_time=NOW + timedelta(hours=2),
                                  exercise_id=ex.id))
    classmate = real_user(db_session, UserRole.student, t.id)  # in the class: assigned
    ensure_enrollment(db_session, user_id=uuid.UUID(classmate.id), course_id=shared.id, tenant_id=t.id)
    outsider = real_user(db_session, UserRole.student, t.id)
    db_session.flush()
    return {"t": t, "ex": ex, "course": shared, "classmate": classmate, "outsider": outsider}


def _self_enrol(client, w) -> dict:
    act_as(w["outsider"])
    r = client.post(f"/courses/{w['course'].id}/enroll",
                    json={"user_id": w["outsider"].id, "course_id": str(w["course"].id)})
    assert r.status_code == 201, r.text
    return r.json()


def _source(db, user, course) -> str:
    db.expire_all()
    return db.query(Enrollment.source).filter(Enrollment.user_id == uuid.UUID(user.id),
                                              Enrollment.course_id == course.id).scalar()


def _telemetry_readable(db, ex, user) -> bool:
    try:
        readable_telemetry_range(db, ex.range_id, user)
        return True
    except HTTPException as exc:
        assert exc.status_code == 404
        return False


def _submit(client, ex):
    return client.post(f"/exercises/{ex.id}/objectives/detect_c2/detections", json={"query": "url.domain:*northwind*"})


def _complete_with_aar(db, ex) -> None:
    ex = db.get(Exercise, ex.id)
    ex.state, ex.completed_at = ExerciseState.completed, NOW
    db.add(AfterActionReport(exercise_id=ex.id, report_json=json.dumps({"participants": [{"name": "Classmate"}]}),
                             report_html=""))
    db.flush()


def test_self_enrolment_during_a_booked_run_gains_no_telemetry_no_submission_no_aar(client, db_session, store, booked):
    w = booked
    _self_enrol(client, w)
    assert _source(db_session, w["outsider"], w["course"]) == SELF

    assert _telemetry_readable(db_session, w["ex"], w["outsider"]) is False
    assert may_submit(db_session, w["outsider"], w["ex"]) is False
    act_as(w["outsider"])
    r = _submit(client, w["ex"])
    assert r.status_code == 403 and "not a participant" in r.json()["detail"]
    assert db_session.query(DetectionSubmission).filter(
        DetectionSubmission.user_id == uuid.UUID(w["outsider"].id)).count() == 0

    _complete_with_aar(db_session, w["ex"])
    act_as(w["outsider"])
    assert client.get(f"/exercises/{w['ex'].id}/aar").status_code == 404


def test_the_assigned_classmate_keeps_their_seat(client, db_session, store, booked):
    w = booked
    assert _telemetry_readable(db_session, w["ex"], w["classmate"]) is True
    act_as(w["classmate"])
    assert _submit(client, w["ex"]).status_code == 201


def test_a_rostered_student_reads_their_aar_without_a_team_or_a_submission(client, db_session, booked):
    """S2: the roster is participation; joining a team or submitting is not required."""
    w = booked
    _complete_with_aar(db_session, w["ex"])
    act_as(w["classmate"])
    assert client.get(f"/exercises/{w['ex'].id}/aar").status_code == 200


def test_staff_assigning_a_self_enrolled_student_gives_them_the_seat(client, db_session, booked):
    w = booked
    first = _self_enrol(client, w)
    act_as(real_user(db_session, UserRole.admin, w["t"].id))
    r = client.post(f"/courses/{w['course'].id}/enroll",
                    json={"user_id": w["outsider"].id, "course_id": str(w["course"].id)})
    assert r.status_code == 200 and r.json()["id"] == first["id"]
    assert _source(db_session, w["outsider"], w["course"]) == ASSIGNED
    assert _telemetry_readable(db_session, w["ex"], w["outsider"]) is True


def test_a_self_enrolment_repeated_never_downgrades_an_assigned_one(client, db_session, booked):
    w = booked
    act_as(w["classmate"])
    r = client.post(f"/courses/{w['course'].id}/enroll",
                    json={"user_id": w["classmate"].id, "course_id": str(w["course"].id)})
    assert r.status_code == 200
    assert _source(db_session, w["classmate"], w["course"]) == ASSIGNED


def test_a_path_picked_in_onboarding_is_a_self_enrolment(client, db_session, booked):
    """The Student's own choice in onboarding is content, not a seat (routers/onboarding.py)."""
    w = booked
    path = LearningPath(id=uuid.uuid4(), name="Mine", tenant_id=None, is_published=True,
                        course_ids=json.dumps([str(w["course"].id)]))
    db_session.add(path)
    db_session.flush()
    act_as(w["outsider"])
    r = client.post("/onboarding/select-path", json={"learning_path_id": str(path.id)})
    assert r.status_code == 200, r.text
    assert _source(db_session, w["outsider"], w["course"]) == SELF
    assert _telemetry_readable(db_session, w["ex"], w["outsider"]) is False


def test_an_unknown_source_is_refused(db_session, booked):
    with pytest.raises(ValueError):
        ensure_enrollment(db_session, user_id=uuid.UUID(booked["outsider"].id), course_id=booked["course"].id,
                          tenant_id=booked["t"].id, source="moodle")


# ── S1: only this run's participation counts ─────────────────────────────────────────
@pytest.fixture
def replayed(db_session):
    """Completed run 2 (after run?reset=true), with a stored AAR and no booking."""
    t = real_tenant(db_session, "replay")
    ex = real_exercise(db_session, t.id, state=ExerciseState.completed, started_at=RUN_START, completed_at=NOW)
    db_session.add(AfterActionReport(exercise_id=ex.id, report_json="{}", report_html=""))
    db_session.flush()
    return t, ex


def _submission(db, ex, user, window_start) -> None:
    db.add(DetectionSubmission(tenant_id=ex.tenant_id, exercise_id=ex.id, objective_ref="o",
                               user_id=uuid.UUID(user.id), query="q", verdict="missed", window_start=window_start))
    db.flush()


def test_a_run_1_submitter_does_not_read_run_2s_aar(client, db_session, replayed):
    t, ex = replayed
    old = real_user(db_session, UserRole.student, t.id)
    _submission(db_session, ex, old, RUN_START - timedelta(days=3))  # run 1
    act_as(old)
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 404


def test_a_run_2_submitter_reads_it(client, db_session, replayed):
    t, ex = replayed
    me = real_user(db_session, UserRole.student, t.id)
    _submission(db_session, ex, me, RUN_START)
    act_as(me)
    assert client.get(f"/exercises/{ex.id}/aar").status_code == 200
