"""An instructor sets up their own class without an admin (#200).

The rule (app/class_setup/scope.py): you teach a course, or run an exercise, when a live
booking (not draft, not cancelled) in your tenant names you its instructor and that course
or exercise. Then, with ``enrollment:manage_own`` / ``team:manage_own``, you may enrol and
withdraw Students on the course and create and change the exercise's teams. Everything
else is refused: another course or exercise (403), another tenant's anything (404), staff
as targets (403), Students and observers (403). Admins keep ``user:update`` unchanged.
Each change leaves an audit row naming who made it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_exercise, real_tenant, real_user
from app.enrollment import ASSIGNED, ensure_enrollment
from app.models import AuditLog, Course, Enrollment, EnrollmentStatus, Team, TeamMembership, UserRole
from app.scheduler.models import EventState, ScheduledEvent

NOW = datetime.now(UTC)


def _book(db, tenant_id, instructor, *, course=None, exercise=None, state=EventState.scheduled):
    evt = ScheduledEvent(
        name="class", tenant_id=tenant_id, instructor_id=uuid.UUID(instructor.id), state=state,
        course_id=course.id if course else None, exercise_id=exercise.id if exercise else None,
        range_id=exercise.range_id if exercise else None,
        start_time=NOW + timedelta(days=1), end_time=NOW + timedelta(days=1, hours=4),
    )
    db.add(evt)
    db.flush()
    return evt


def _course(db, tenant_id, name="Course"):
    c = Course(id=uuid.uuid4(), name=name, tenant_id=tenant_id, is_published=True)
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def world(db_session):
    """Tenant A: an instructor who teaches course ``mine`` and runs exercise ``ex``, a second
    instructor, a Student, an observer, an admin, and course ``theirs`` (not taught by the
    first instructor). Tenant B: its own course, exercise, Student and team."""
    db = db_session
    a, b = real_tenant(db, "class-a"), real_tenant(db, "class-b")
    w = {
        "a": a, "b": b,
        "instructor": real_user(db, UserRole.instructor, a.id),
        "colleague": real_user(db, UserRole.instructor, a.id),
        "student": real_user(db, UserRole.student, a.id),
        "student2": real_user(db, UserRole.student, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "b_student": real_user(db, UserRole.student, b.id),
        "b_instructor": real_user(db, UserRole.instructor, b.id),
        "mine": _course(db, a.id, "Mine"),
        "theirs": _course(db, a.id, "Theirs"),
        "b_course": _course(db, b.id, "B course"),
        "ex": real_exercise(db, a.id),
        "other_ex": real_exercise(db, a.id),
        "b_ex": real_exercise(db, b.id),
    }
    _book(db, a.id, w["instructor"], course=w["mine"], exercise=w["ex"])
    _book(db, a.id, w["colleague"], course=w["theirs"], exercise=w["other_ex"])
    _book(db, b.id, w["b_instructor"], course=w["b_course"], exercise=w["b_ex"])
    b_team = Team(id=uuid.uuid4(), name="B team", tenant_id=b.id, exercise_id=w["b_ex"].id)
    db.add(b_team)
    db.flush()
    w["b_team"] = b_team
    return w


def _enrol(client, course, who):
    return client.post(f"/courses/{course.id}/enroll", json={"user_id": who.id, "course_id": str(course.id)})


def _row(db, who, course):
    db.expire_all()
    return db.query(Enrollment).filter(Enrollment.user_id == uuid.UUID(who.id), Enrollment.course_id == course.id).first()


def _audits(db, actor, action):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.user_id == uuid.UUID(actor.id), AuditLog.action == action).all()


# ── Enrolment ────────────────────────────────────────────────────────────────


def test_instructor_enrols_a_student_on_a_course_they_teach_and_it_is_audited(client, db_session, world):
    w = world
    act_as(w["instructor"])
    r = _enrol(client, w["mine"], w["student"])
    assert r.status_code == 201, r.text
    row = _row(db_session, w["student"], w["mine"])
    assert row.status == EnrollmentStatus.enrolled and row.source == ASSIGNED
    [audit] = _audits(db_session, w["instructor"], "enrollment_create")
    assert audit.resource_id == str(row.id) and str(w["student"].id) in audit.detail
    assert audit.tenant_id == w["a"].id


def test_instructor_enrols_on_a_shared_course_through_their_own_booking(client, db_session, world):
    w = world
    shared = _course(db_session, None, "Shared")
    _book(db_session, w["a"].id, w["instructor"], course=shared)
    act_as(w["instructor"])
    assert _enrol(client, shared, w["student"]).status_code == 201


@pytest.mark.parametrize("state", [EventState.draft, EventState.cancelled])
def test_a_draft_or_cancelled_booking_does_not_make_you_its_teacher(client, db_session, world, state):
    w = world
    course = _course(db_session, w["a"].id, "Not live")
    _book(db_session, w["a"].id, w["instructor"], course=course, state=state)
    act_as(w["instructor"])
    assert _enrol(client, course, w["student"]).status_code == 403
    assert _row(db_session, w["student"], course) is None


def test_a_completed_booking_still_counts(client, db_session, world):
    w = world
    course = _course(db_session, w["a"].id, "Taught last week")
    _book(db_session, w["a"].id, w["instructor"], course=course, state=EventState.completed)
    act_as(w["instructor"])
    assert _enrol(client, course, w["student"]).status_code == 201


def test_instructor_cannot_enrol_on_a_course_someone_else_teaches(client, db_session, world):
    w = world
    act_as(w["instructor"])
    r = _enrol(client, w["theirs"], w["student"])
    assert r.status_code == 403
    assert _row(db_session, w["student"], w["theirs"]) is None
    assert _audits(db_session, w["instructor"], "enrollment_create") == []


def test_a_booking_in_another_tenant_naming_you_gives_nothing(client, db_session, world):
    """Defence in depth: only bookings of the caller's own tenant count."""
    w = world
    shared = _course(db_session, None, "Shared B")
    _book(db_session, w["b"].id, w["instructor"], course=shared)
    act_as(w["instructor"])
    assert _enrol(client, shared, w["student"]).status_code == 403


def test_another_tenants_course_or_student_is_404(client, db_session, world):
    w = world
    act_as(w["instructor"])
    assert _enrol(client, w["b_course"], w["student"]).status_code == 404
    assert _enrol(client, w["mine"], w["b_student"]).status_code == 404
    assert _row(db_session, w["b_student"], w["mine"]) is None


@pytest.mark.parametrize("target", ["colleague", "observer", "admin"])
def test_instructor_enrols_only_students(client, db_session, world, target):
    w = world
    act_as(w["instructor"])
    assert _enrol(client, w["mine"], w[target]).status_code == 403
    assert _row(db_session, w[target], w["mine"]) is None


@pytest.mark.parametrize("caller", ["student", "observer"])
def test_students_and_observers_cannot_enrol_anyone_else(client, db_session, world, caller):
    w = world
    act_as(w[caller])
    assert _enrol(client, w["mine"], w["student2"]).status_code == 403
    assert _row(db_session, w["student2"], w["mine"]) is None


def test_a_student_still_enrols_themselves(client, db_session, world):
    w = world
    act_as(w["student"])
    assert _enrol(client, w["mine"], w["student"]).status_code == 201


def test_admin_unchanged_enrols_anyone_in_the_tenant_without_a_booking(client, db_session, world):
    w = world
    act_as(w["admin"])
    assert _enrol(client, w["theirs"], w["colleague"]).status_code == 201
    assert _audits(db_session, w["admin"], "enrollment_create")
    assert _enrol(client, w["b_course"], w["student"]).status_code == 404
    assert _enrol(client, w["mine"], w["b_student"]).status_code == 404


# ── Withdrawal ───────────────────────────────────────────────────────────────


def _withdraw(client, course, who):
    return client.post(f"/courses/{course.id}/withdraw/{who.id}")


def test_instructor_withdraws_and_re_enrols_a_student_audited(client, db_session, world):
    w = world
    ensure_enrollment(db_session, user_id=uuid.UUID(w["student"].id), course_id=w["mine"].id, tenant_id=w["a"].id)
    act_as(w["instructor"])

    r = _withdraw(client, w["mine"], w["student"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "withdrawn"
    assert _row(db_session, w["student"], w["mine"]).status == EnrollmentStatus.withdrawn
    [audit] = _audits(db_session, w["instructor"], "enrollment_withdraw")
    assert "enrolled -> withdrawn" in audit.detail

    # Again: unchanged, no second audit row.
    assert _withdraw(client, w["mine"], w["student"]).status_code == 200
    assert len(_audits(db_session, w["instructor"], "enrollment_withdraw")) == 1

    # The Student cannot reopen it themselves...
    act_as(w["student"])
    assert _enrol(client, w["mine"], w["student"]).json()["status"] == "withdrawn"
    # ...their instructor can.
    act_as(w["instructor"])
    r = _enrol(client, w["mine"], w["student"])
    assert r.status_code == 200 and r.json()["status"] == "enrolled"
    assert _audits(db_session, w["instructor"], "enrollment_update")


def test_withdraw_refusals(client, db_session, world):
    w = world
    for who, course in ((w["student"], w["theirs"]), (w["student"], w["mine"])):
        ensure_enrollment(db_session, user_id=uuid.UUID(who.id), course_id=course.id, tenant_id=w["a"].id)
    act_as(w["instructor"])
    assert _withdraw(client, w["theirs"], w["student"]).status_code == 403  # not their course
    assert _withdraw(client, w["b_course"], w["student"]).status_code == 404  # other tenant's course
    assert _withdraw(client, w["mine"], w["b_student"]).status_code == 404  # other tenant's Student
    assert _withdraw(client, w["mine"], w["student2"]).status_code == 404  # not enrolled
    assert _withdraw(client, w["mine"], w["colleague"]).status_code == 403  # staff
    assert _row(db_session, w["student"], w["theirs"]).status == EnrollmentStatus.enrolled

    row = _row(db_session, w["student"], w["mine"])
    row.status = EnrollmentStatus.completed
    db_session.flush()
    assert _withdraw(client, w["mine"], w["student"]).status_code == 409

    for caller in ("student", "observer"):
        act_as(w[caller])
        assert _withdraw(client, w["mine"], w["student2"]).status_code == 403


def test_admin_withdraws_anyone_in_the_tenant(client, db_session, world):
    w = world
    ensure_enrollment(db_session, user_id=uuid.UUID(w["colleague"].id), course_id=w["theirs"].id, tenant_id=w["a"].id)
    act_as(w["admin"])
    assert _withdraw(client, w["theirs"], w["colleague"]).status_code == 200
    assert _audits(db_session, w["admin"], "enrollment_withdraw")


# ── What is mine ─────────────────────────────────────────────────────────────


def test_class_setup_mine_lists_only_the_callers_live_classes(client, db_session, world):
    w = world
    _book(db_session, w["a"].id, w["instructor"], course=w["theirs"], state=EventState.cancelled)
    act_as(w["instructor"])
    r = client.get("/class-setup/mine")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["id"] for c in body["courses"]] == [str(w["mine"].id)]
    assert [e["id"] for e in body["exercises"]] == [str(w["ex"].id)]
    for caller in ("student", "observer"):
        act_as(w[caller])
        assert client.get("/class-setup/mine").status_code == 403


# ── Teams ────────────────────────────────────────────────────────────────────


def _team(client, exercise=None, name="Blue"):
    body = {"name": name, "team_type": "blue"}
    if exercise is not None:
        body["exercise_id"] = str(exercise.id)
    return client.post("/teams", json=body)


def _team_audits(db, actor, action, team_id):
    return [a for a in _audits(db, actor, action) if a.resource_type == "team" and a.resource_id == str(team_id)]


def test_instructor_sets_up_the_teams_of_an_exercise_they_run(client, db_session, world):
    w = world
    act_as(w["instructor"])
    r = _team(client, w["ex"])
    assert r.status_code == 201, r.text
    team_id = r.json()["id"]
    assert r.json()["exercise_id"] == str(w["ex"].id)
    assert _team_audits(db_session, w["instructor"], "create", team_id)

    r = client.patch(f"/teams/{team_id}", json={"name": "Blue cell", "max_members": 4})
    assert r.status_code == 200 and r.json()["name"] == "Blue cell"
    [upd] = _team_audits(db_session, w["instructor"], "update", team_id)
    assert upd.detail == "max_members, name"

    r = client.post(f"/teams/{team_id}/members", params={"user_id": w["student"].id})
    assert r.status_code == 201, r.text
    db_session.expire_all()
    assert db_session.query(TeamMembership).filter(TeamMembership.team_id == uuid.UUID(team_id)).count() == 1
    assert _team_audits(db_session, w["instructor"], "add_member", team_id)

    assert client.delete(f"/teams/{team_id}/members/{w['student'].id}").status_code == 204
    assert _team_audits(db_session, w["instructor"], "remove_member", team_id)

    assert client.delete(f"/teams/{team_id}").status_code == 204
    assert _team_audits(db_session, w["instructor"], "delete", team_id)


def test_instructor_team_refusals(client, db_session, world):
    w = world
    act_as(w["instructor"])
    assert _team(client).status_code == 403  # a standing team is an admin's
    assert _team(client, w["other_ex"]).status_code == 403  # someone else's exercise
    assert _team(client, w["b_ex"]).status_code == 404  # another tenant's exercise
    db_session.expire_all()
    assert db_session.query(Team).filter(Team.tenant_id == w["a"].id).count() == 0

    standing = Team(id=uuid.uuid4(), name="Standing", tenant_id=w["a"].id)
    theirs = Team(id=uuid.uuid4(), name="Theirs", tenant_id=w["a"].id, exercise_id=w["other_ex"].id)
    db_session.add_all([standing, theirs])
    db_session.flush()
    for t in (standing, theirs):
        assert client.patch(f"/teams/{t.id}", json={"name": "x"}).status_code == 403
        assert client.post(f"/teams/{t.id}/members", params={"user_id": w["student"].id}).status_code == 403
        assert client.delete(f"/teams/{t.id}").status_code == 403
    b = w["b_team"]
    assert client.patch(f"/teams/{b.id}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/teams/{b.id}").status_code == 404

    mine = _team(client, w["ex"]).json()["id"]
    assert client.post(f"/teams/{mine}/members", params={"user_id": w["colleague"].id}).status_code == 403  # staff
    assert client.post(f"/teams/{mine}/members", params={"user_id": w["b_student"].id}).status_code == 404


def test_instructor_cannot_remove_staff_an_admin_placed(client, db_session, world):
    w = world
    team = Team(id=uuid.uuid4(), name="Mine", tenant_id=w["a"].id, exercise_id=w["ex"].id)
    db_session.add(team)
    db_session.flush()
    db_session.add(TeamMembership(team_id=team.id, user_id=uuid.UUID(w["colleague"].id)))
    db_session.flush()
    act_as(w["instructor"])
    assert client.delete(f"/teams/{team.id}/members/{w['colleague'].id}").status_code == 403


@pytest.mark.parametrize("caller", ["student", "observer"])
def test_students_and_observers_cannot_change_teams(client, db_session, world, caller):
    w = world
    team = Team(id=uuid.uuid4(), name="Mine", tenant_id=w["a"].id, exercise_id=w["ex"].id)
    db_session.add(team)
    db_session.flush()
    act_as(w[caller])
    assert _team(client, w["ex"]).status_code == 403
    assert client.patch(f"/teams/{team.id}", json={"name": "x"}).status_code == 403
    assert client.post(f"/teams/{team.id}/members", params={"user_id": w["student"].id}).status_code == 403
    assert client.delete(f"/teams/{team.id}").status_code == 403


def test_admin_teams_unchanged(client, db_session, world):
    w = world
    act_as(w["admin"])
    r = _team(client)
    assert r.status_code == 201 and r.json()["exercise_id"] is None
    tid = r.json()["id"]
    assert client.post(f"/teams/{tid}/members", params={"user_id": w["colleague"].id}).status_code == 201
    assert _team(client, w["other_ex"]).status_code == 201  # any exercise of the tenant
    assert _team(client, w["b_ex"]).status_code == 404
