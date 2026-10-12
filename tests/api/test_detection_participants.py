"""Only an exercise's participants submit detections (ADR 0005 open question, sweep M3).

Any Student of the tenant holding detection:submit could submit on any running exercise
and earn (or spend) its detection credit. Now (app/exercise_participants.py) a Student
submits only as a participant: enrolled in the course of a live booking that ties the
exercise or its range to a course, or on their own lab session's range. An exercise with
no roster has no Student participants (default); DETECTION_SUBMIT_PARTICIPANTS_ONLY=false
lets any Student of the tenant submit on such an exercise. Staff always may.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import SCENARIO, STARTED, FakeStore, act_as, beacon, real_exercise, real_tenant, real_user
from app.detections.models import DetectionSubmission
from app.main import app as fastapi_app
from app.models import Course, Enrollment, EnrollmentStatus, ExerciseState, Objective, ObjectiveType, UserRole
from app.routers.detections import search_backend
from app.scheduler.models import EventState, ScheduledEvent


@pytest.fixture
def store():
    s = FakeStore(events=[beacon(), beacon()])
    fastapi_app.dependency_overrides[search_backend] = lambda: s
    yield s
    fastapi_app.dependency_overrides.pop(search_backend, None)


@pytest.fixture
def world(db_session, monkeypatch):
    monkeypatch.delenv("DETECTION_SUBMIT_PARTICIPANTS_ONLY", raising=False)  # the default
    a = real_tenant(db_session, "dp-a")
    ex = real_exercise(db_session, a.id, SCENARIO, state=ExerciseState.running, started_at=STARTED,
                       max_score=40, total_score=0)
    db_session.add(Objective(exercise_id=ex.id, ref_id="detect_c2", objective_type=ObjectiveType.detection,
                             description="c2", validator="opensearch_query", points=40))
    db_session.flush()
    return {"a": a, "ex": ex,
            "student": real_user(db_session, UserRole.student, a.id),
            "classmate": real_user(db_session, UserRole.student, a.id),
            "instructor": real_user(db_session, UserRole.instructor, a.id)}


def _submit(client, ex):
    return client.post(f"/exercises/{ex.id}/objectives/detect_c2/detections", json={"query": "url.domain:*northwind*"})


def _book(db, world, *, enrol=("student",), state=EventState.active, by_range=False, status=EnrollmentStatus.enrolled,
          when=None):
    course = Course(id=uuid.uuid4(), name="Class", tenant_id=world["a"].id)
    db.add(course)
    db.flush()
    now = datetime.now(UTC)
    start, end = when or (now, now + timedelta(hours=2))
    link = {"range_id": world["ex"].range_id} if by_range else {"exercise_id": world["ex"].id}
    db.add(ScheduledEvent(name="lesson", tenant_id=world["a"].id, course_id=course.id, state=state, start_time=start,
                          end_time=end, **link))
    for who in enrol:
        db.add(Enrollment(user_id=uuid.UUID(world[who].id), course_id=course.id, tenant_id=world["a"].id,
                          status=status))
    db.flush()
    return course


def _attempts(db) -> int:
    db.expire_all()
    return db.query(DetectionSubmission).count()


def test_without_a_roster_no_student_may_submit(client, db_session, store, world):
    act_as(world["student"])
    r = _submit(client, world["ex"])
    assert r.status_code == 403 and "not a participant" in r.json()["detail"]
    assert _attempts(db_session) == 0  # refused before an attempt is reserved
    db_session.expire_all()
    assert db_session.get(Objective, db_session.query(Objective.id).filter(
        Objective.exercise_id == world["ex"].id).scalar()).achieved is False


@pytest.mark.parametrize("by_range", [False, True], ids=["booking-names-exercise", "booking-names-range"])
def test_the_booked_courses_students_submit_and_classmates_do_not(client, db_session, store, world, by_range):
    _book(db_session, world, by_range=by_range)
    act_as(world["classmate"])  # same tenant, not on the course
    assert _submit(client, world["ex"]).status_code == 403
    act_as(world["student"])
    r = _submit(client, world["ex"])
    assert r.status_code == 201, r.text
    assert r.json()["verdict"] == "achieved"


def test_a_cancelled_booking_or_a_finished_enrolment_is_no_roster_seat(client, db_session, store, world):
    _book(db_session, world, state=EventState.cancelled)
    act_as(world["student"])
    assert _submit(client, world["ex"]).status_code == 403  # cancelled: no roster at all -> default refuses
    _book(db_session, world, status=EnrollmentStatus.withdrawn)
    assert _submit(client, world["ex"]).status_code == 403


def test_staff_may_always_submit(client, db_session, store, world):
    act_as(world["instructor"])
    assert _submit(client, world["ex"]).status_code == 201


def test_opting_out_restores_any_tenant_student_only_where_there_is_no_roster(client, db_session, store, world,
                                                                               monkeypatch):
    monkeypatch.setenv("DETECTION_SUBMIT_PARTICIPANTS_ONLY", "false")
    act_as(world["classmate"])
    assert _submit(client, world["ex"]).status_code == 201  # no roster: open to the tenant's Students
    other = real_exercise(db_session, world["a"].id, SCENARIO, state=ExerciseState.running, started_at=STARTED,
                          max_score=40, total_score=0)
    db_session.add(Objective(exercise_id=other.id, ref_id="detect_c2", objective_type=ObjectiveType.detection,
                             description="c2", validator="opensearch_query", points=40))
    db_session.flush()
    _book(db_session, {**world, "ex": other})
    assert _submit(client, other).status_code == 403  # a roster still applies


def test_a_student_submits_on_their_own_labs_range(client, db_session, store, world, monkeypatch):
    from app import exercise_participants, telemetry_access

    monkeypatch.setattr(telemetry_access, "_own_lab",
                        lambda db, user_id, range_id: str(user_id) == world["student"].id
                        and range_id == world["ex"].range_id)
    act_as(world["classmate"])
    assert _submit(client, world["ex"]).status_code == 403
    act_as(world["student"])
    assert _submit(client, world["ex"]).status_code == 201
    assert exercise_participants.participants_only() is True


# ── bookings count only while they overlap the exercise (review of #133, 2026-10-11) ──

PAST = (STARTED - timedelta(days=30), STARTED - timedelta(days=30) + timedelta(hours=2))  # ended before the start


def _future():
    now = datetime.now(UTC)
    return now + timedelta(days=7), now + timedelta(days=7, hours=2)


@pytest.mark.parametrize("by_range", [False, True], ids=["booking-names-exercise", "booking-names-range"])
@pytest.mark.parametrize("when", ["past", "future"])
def test_a_booking_at_another_time_is_no_seat(client, db_session, store, world, by_range, when):
    """A range is reused class after class: last month's class (or next week's) is not a
    participant of the exercise running now."""
    _book(db_session, world, by_range=by_range, when=PAST if when == "past" else _future())
    act_as(world["student"])
    r = _submit(client, world["ex"])
    assert r.status_code == 403 and "not a participant" in r.json()["detail"]
    assert _attempts(db_session) == 0


def test_a_booking_overlapping_the_running_exercise_is_a_seat(client, db_session, store, world):
    _book(db_session, world, by_range=True, when=PAST)  # an old class on the same range
    _book(db_session, world, by_range=True, enrol=("classmate",),
          when=(STARTED - timedelta(hours=1), datetime.now(UTC) + timedelta(hours=1)))
    act_as(world["student"])  # enrolled only in the old class
    assert _submit(client, world["ex"]).status_code == 403
    act_as(world["classmate"])  # enrolled in the class booked now
    assert _submit(client, world["ex"]).status_code == 201


def test_opting_out_does_not_open_an_exercise_booked_at_another_time(client, db_session, store, world, monkeypatch):
    """An exercise whose range was booked only at other times has a roster with no seat
    now, not "no roster": DETECTION_SUBMIT_PARTICIPANTS_ONLY=false must not open it."""
    monkeypatch.setenv("DETECTION_SUBMIT_PARTICIPANTS_ONLY", "false")
    _book(db_session, world, by_range=True, when=PAST)
    for who in ("student", "classmate"):
        act_as(world[who])
        assert _submit(client, world["ex"]).status_code == 403, who


def test_the_window_of_a_completed_exercise_is_start_to_completion(db_session, world):
    from app.exercise_participants import exercise_window, roster_courses

    ex = world["ex"]
    ex.completed_at = STARTED + timedelta(hours=3)
    db_session.flush()
    assert exercise_window(ex) == (STARTED, STARTED + timedelta(hours=3))
    during = _book(db_session, world, when=(STARTED + timedelta(hours=2), STARTED + timedelta(hours=4)))
    after = _book(db_session, world, when=(STARTED + timedelta(hours=5), STARTED + timedelta(hours=6)))
    before = _book(db_session, world, by_range=True, when=PAST)
    courses = roster_courses(db_session, ex)
    assert during.id in courses and after.id not in courses and before.id not in courses
