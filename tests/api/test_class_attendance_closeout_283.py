"""Classes 6: automatic attendance and instructor close-out (ADR 0014 §12-13; issue #283).

The rules under test:

- A Student is present at a session when they opened their own class range (their seat, or
  the class's shared range) during it: from its start, inclusive, to its end, exclusive.
  Evidence is the audit row of the Student loading that range's credentials
  (``class_range_open``) or revealing one of its passwords (``range_credential_reveal``).
- The clock records each ended session once per Student; running it again changes nothing,
  and it never overwrites what the lead or an admin set.
- The lead or an admin corrects attendance (present, absent, excused), audited.
- Close-out pre-fills each Student's attendance, course standing and a suggestion. On an
  Unclassified deployment there are no numbers anywhere: pass/fail only.
- Closing needs a result for every Student, sends it to their enrolment, cancels sessions not
  started and makes the class read-only; ranges then come down (``class_closed``). Only an
  admin reopens it, audited. The CSV export defuses formula cells.
- Staff see attendance and results; Students on the roll call do not; another tenant sees
  nothing.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from _shared import act_as, real_tenant, real_user
from app.auth import CurrentUser
from app.classes import attendance, closeout
from app.classes import ranges as class_ranges
from app.classes.models import (
    Class,
    ClassAttendance,
    ClassRange,
    ClassResult,
    ClassSession,
    ClassStaff,
    RollCallEntry,
    RollCallSettings,
)
from app.detections.models import ACHIEVED, MISSED, DetectionSubmission
from app.enrollment import ensure_enrollment
from app.models import (
    AuditLog,
    Course,
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    Exercise,
    ExerciseState,
    ModuleContentType,
    ModuleProgress,
    ModuleProgressStatus,
    Range,
    RangeState,
    Scenario,
    Template,
    User,
    UserRole,
)
from app.scheduler.models import EventState, ScheduledEvent

NOW = datetime.now(UTC).replace(microsecond=0)
D1 = (NOW - timedelta(days=2)).replace(hour=9, minute=0, second=0)
D2 = D1 + timedelta(days=1)
HOURS = timedelta(hours=3)


class Outbox:
    async def send(self, to, subject, body, metadata=None):
        return True


@pytest.fixture(autouse=True)
def quiet_mail():
    with patch("app.notifications.get_channel", return_value=Outbox()):
        yield


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")


@pytest.fixture
def unclassified(monkeypatch):
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")


def _student(db, tenant_id, name: str) -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=f"{name}@corp.test",
             display_name=name.title(), role=UserRole.student, tenant_id=tenant_id, source="ad")
    db.add(u)
    db.flush()
    return u


def _as(u: User) -> CurrentUser:
    return CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                       tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)


def _session(db, w, name: str, start: datetime, state=EventState.completed, hours=HOURS) -> tuple:
    evt = ScheduledEvent(id=uuid.uuid4(), name=name, state=state, tenant_id=w["a"].id, instructor_id=uuid.UUID(
        w["lead"].id), start_time=start, end_time=start + hours, template_id=w["tpl"].id)
    db.add(evt)
    db.flush()
    cs = ClassSession(id=uuid.uuid4(), class_id=w["cls"].id, event_id=evt.id, tenant_id=w["a"].id,
                      range_mode="individual")
    db.add(cs)
    db.flush()
    return cs, evt


@pytest.fixture
def w(db_session):
    db = db_session
    a, b = real_tenant(db, "att-a"), real_tenant(db, "att-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    tpl = Template(id=uuid.uuid4(), name="Seat", yaml="nodes: [{id: ws, os: kali}]\n", tenant_id=a.id)
    db.add_all([course, tpl])
    db.flush()
    module = CourseModule(id=uuid.uuid4(), course_id=course.id, title="Triage", ordinal=1,
                          content_type=ModuleContentType.scenario)
    db.add(module)
    db.add(RollCallSettings(tenant_id=a.id, email_domains=["corp.test"]))
    w = {"a": a, "b": b, "course": course, "tpl": tpl, "module": module,
         "lead": real_user(db, UserRole.instructor, a.id), "assistant": real_user(db, UserRole.instructor, a.id),
         "observer": real_user(db, UserRole.observer, a.id), "admin": real_user(db, UserRole.admin, a.id),
         "stranger": real_user(db, UserRole.instructor, a.id), "b_admin": real_user(db, UserRole.admin, b.id)}
    cls = Class(id=uuid.uuid4(), tenant_id=a.id, name="C201-26-03", course_id=course.id,
                start_date=(NOW - timedelta(days=3)).date(), end_date=(NOW + timedelta(days=3)).date(),
                lead_instructor_id=uuid.UUID(w["lead"].id), status="open")
    db.add(cls)
    db.flush()
    db.add(ClassStaff(class_id=cls.id, user_id=uuid.UUID(w["assistant"].id), role="assistant"))
    db.add(ClassStaff(class_id=cls.id, user_id=uuid.UUID(w["observer"].id), role="observer"))
    w["cls"] = cls
    w["students"], w["entries"], w["seats"] = {}, {}, {}
    joined = NOW - timedelta(days=5)
    for name in ("ann", "bob", "cat"):
        u = _student(db, a.id, name)
        e = RollCallEntry(id=uuid.uuid4(), class_id=cls.id, tenant_id=a.id, email=u.email, user_id=u.id,
                          status="signed_in", joined_at=joined)
        db.add(e)
        db.flush()
        rng = Range(id=uuid.uuid4(), name=f"seat-{name}", template_id=tpl.id, tenant_id=a.id, state=RangeState.ready)
        db.add(rng)
        db.flush()
        db.add(ClassRange(tenant_id=a.id, class_id=cls.id, template_id=tpl.id, kind="seat", entry_id=e.id,
                          seat_key=str(e.id), range_id=rng.id, builds=1, built_at=joined))
        ensure_enrollment(db, user_id=u.id, course_id=course.id, tenant_id=a.id)
        w["students"][name], w["entries"][name], w["seats"][name] = u, e, rng
    shared = Range(id=uuid.uuid4(), name="shared", template_id=tpl.id, tenant_id=a.id, state=RangeState.ready)
    db.add(shared)
    db.flush()
    db.add(ClassRange(tenant_id=a.id, class_id=cls.id, template_id=tpl.id, kind="shared", seat_key="shared",
                      range_id=shared.id, builds=1, built_at=joined))
    w["shared"] = shared
    w["s1"] = _session(db, w, "Day 1", D1)
    w["s2"] = _session(db, w, "Day 2", D2)
    db.commit()
    return w


def _evidence(db, w, name: str, rng: Range, at: datetime, action: str = attendance.RANGE_OPEN) -> None:
    db.add(AuditLog(user_id=w["students"][name].id, tenant_id=w["a"].id, action=action, resource_type="range",
                    resource_id=str(rng.id), timestamp=at))
    db.flush()


def _status(db, w, session_key: str, name: str) -> ClassAttendance | None:
    db.expire_all()
    cs, _ = w[session_key]
    return db.query(ClassAttendance).filter_by(session_id=cs.id, entry_id=w["entries"][name].id).one_or_none()


def _url(w, path: str = "") -> str:
    return f"/classes/{w['cls'].id}{path}"


# ── The attendance rule ──────────────────────────────────────────────────────


def test_present_means_their_own_range_opened_inside_the_window(db_session, w):
    db = db_session
    cs, evt = w["s1"]
    _evidence(db, w, "ann", w["seats"]["ann"], evt.start_time)  # the first second counts
    _evidence(db, w, "bob", w["seats"]["bob"], evt.end_time)  # the end does not
    _evidence(db, w, "bob", w["seats"]["bob"], evt.start_time - timedelta(seconds=1))  # nor before the start
    _evidence(db, w, "cat", w["seats"]["ann"], evt.start_time + timedelta(minutes=5))  # nor someone else's seat
    _evidence(db, w, "cat", w["seats"]["cat"], evt.start_time + timedelta(minutes=5), action="range_update")
    verdicts = attendance.evaluate(db, w["cls"], evt, attendance.roll_call(db, w["cls"]))
    by_name = {n: verdicts[w["entries"][n].id] for n in ("ann", "bob", "cat")}
    assert by_name["ann"] == ("present", D1)
    assert by_name["bob"] == ("absent", None)
    assert by_name["cat"] == ("absent", None)
    # The last second inside the window counts; so does a password reveal; so does the shared range.
    _evidence(db, w, "bob", w["seats"]["bob"], evt.end_time - timedelta(seconds=1), action="range_credential_reveal")
    _evidence(db, w, "cat", w["shared"], evt.start_time + timedelta(hours=1))
    verdicts = attendance.evaluate(db, w["cls"], evt, attendance.roll_call(db, w["cls"]))
    assert verdicts[w["entries"]["bob"].id][0] == "present"
    assert verdicts[w["entries"]["cat"].id][0] == "present"


def test_another_tenants_evidence_never_counts(db_session, w):
    db = db_session
    _, evt = w["s1"]
    db.add(AuditLog(user_id=w["students"]["ann"].id, tenant_id=w["b"].id, action=attendance.RANGE_OPEN,
                    resource_type="range", resource_id=str(w["seats"]["ann"].id),
                    timestamp=evt.start_time + timedelta(minutes=1)))
    db.flush()
    verdicts = attendance.evaluate(db, w["cls"], evt, attendance.roll_call(db, w["cls"]))
    assert verdicts[w["entries"]["ann"].id] == ("absent", None)


def test_opening_the_range_records_the_signal_once_per_ten_minutes(client, db_session, w):
    db = db_session
    live = _session(db, w, "Live", NOW - timedelta(hours=1), state=EventState.active)
    db.commit()
    seat = w["seats"]["ann"]
    act_as(_as(w["students"]["ann"]))
    assert client.get(f"/ranges/{seat.id}/credentials").status_code == 200
    assert client.get(f"/ranges/{seat.id}/credentials").status_code == 200
    rows = db.query(AuditLog).filter_by(action=attendance.RANGE_OPEN, resource_id=str(seat.id)).all()
    assert len(rows) == 1 and rows[0].user_id == w["students"]["ann"].id
    assert "class_seat_id" in rows[0].detail
    # Someone else's seat is not theirs: 404, and nothing is recorded.
    act_as(_as(w["students"]["bob"]))
    assert client.get(f"/ranges/{seat.id}/credentials").status_code == 404
    assert db.query(AuditLog).filter_by(action=attendance.RANGE_OPEN).count() == 1
    # Staff reading a range are not taking attendance.
    act_as(w["lead"])
    assert client.get(f"/ranges/{seat.id}/credentials").status_code == 200
    assert db.query(AuditLog).filter_by(action=attendance.RANGE_OPEN).count() == 1
    # After the live session ends the clock finds ann present, the others absent.
    attendance.tick(db, now=live[1].end_time + timedelta(minutes=1))
    assert _status(db, w, "s1", "ann").status == "absent"  # day 1: no evidence
    got = {n: db.query(ClassAttendance).filter_by(session_id=live[0].id, entry_id=w["entries"][n].id).one()
           for n in ("ann", "bob", "cat")}
    assert got["ann"].status == "present" and got["ann"].evidence_at is not None
    assert got["bob"].status == got["cat"].status == "absent"


def test_the_clock_records_once_and_never_overwrites(db_session, w):
    db = db_session
    _, evt = w["s1"]
    _evidence(db, w, "ann", w["seats"]["ann"], evt.start_time + timedelta(minutes=3))
    db.commit()
    first = attendance.tick(db, now=NOW)
    assert first.recorded == 6 and first.sessions == 2  # 3 Students x 2 ended sessions
    again = attendance.tick(db, now=NOW)
    assert again.recorded == 0
    assert db.query(ClassAttendance).count() == 6
    # Evidence arriving later for an ended session changes nothing already recorded.
    _evidence(db, w, "bob", w["seats"]["bob"], evt.start_time + timedelta(minutes=3))
    db.commit()
    assert attendance.tick(db, now=NOW).recorded == 0
    assert _status(db, w, "s1", "bob").status == "absent"
    # An instructor's correction stands through every later tick.
    attendance.correct(db, w["cls"], *w["s1"], w["entries"]["cat"], "excused", "sick note", w["lead"])
    db.commit()
    attendance.tick(db, now=NOW)
    row = _status(db, w, "s1", "cat")
    assert (row.status, row.source, row.note) == ("excused", "instructor", "sick note")


def test_who_counts_for_a_session(db_session, w):
    db = db_session
    ann, bob, cat = (w["entries"][n] for n in ("ann", "bob", "cat"))
    bob.status = "removed"  # taken off: not counted
    cat.status = "forfeited"  # lost the seat: absent
    dan = _student(db, w["a"].id, "dan")
    late = RollCallEntry(class_id=w["cls"].id, tenant_id=w["a"].id, email=dan.email, user_id=dan.id,
                         status="signed_in", joined_at=w["s1"][1].end_time + timedelta(minutes=1))
    db.add(late)
    db.commit()
    attendance.tick(db, now=NOW)
    on_day_1 = {r.entry_id: r.status for r in db.query(ClassAttendance).filter_by(session_id=w["s1"][0].id)}
    assert on_day_1 == {ann.id: "absent", cat.id: "absent"}  # dan joined after day 1 ended
    on_day_2 = {r.entry_id for r in db.query(ClassAttendance).filter_by(session_id=w["s2"][0].id)}
    assert late.id in on_day_2 and bob.id not in on_day_2


def test_the_clock_skips_unapproved_old_and_closed(db_session, w):
    db = db_session
    _session(db, w, "Draft", D1 + timedelta(hours=4), state=EventState.pending_approval)
    _session(db, w, "Rejected", D1 + timedelta(hours=5), state=EventState.rejected)
    old = _session(db, w, "Long ago", NOW - attendance.LOOKBACK - timedelta(days=1))
    db.commit()
    res = attendance.tick(db, now=NOW)
    assert res.sessions == 2  # day 1 and day 2 only
    assert db.query(ClassAttendance).filter_by(session_id=old[0].id).count() == 0
    # Close-out records what the clock's look-back left behind.
    assert attendance.record_class(db, w["cls"], NOW) == 3
    w["cls"].status = "closed"
    db.commit()
    db.query(ClassAttendance).delete()
    db.commit()
    assert attendance.tick(db, now=NOW).sessions == 0  # a closed class takes no attendance


# ── Corrections ──────────────────────────────────────────────────────────────


def test_the_lead_corrects_attendance_and_it_is_audited(client, db_session, w):
    db = db_session
    attendance.tick(db, now=NOW)
    db.commit()
    cs, _ = w["s1"]
    bob = w["entries"]["bob"]
    act_as(w["lead"])
    r = client.put(_url(w, f"/attendance/{cs.id}/{bob.id}"), json={"status": "excused", "note": "medical"})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["source"]) == ("excused", "instructor")
    act_as(w["admin"])
    r = client.put(_url(w, f"/attendance/{cs.id}/{bob.id}"), json={"status": "present"})
    assert r.status_code == 200
    audits = db.query(AuditLog).filter_by(action="class_attendance_correct").order_by(AuditLog.timestamp).all()
    assert len(audits) == 2
    assert "absent (auto) -> excused" in audits[0].detail and "medical" in audits[0].detail
    assert "excused (instructor) -> present" in audits[1].detail
    assert {a.user_id for a in audits} == {uuid.UUID(w["lead"].id), uuid.UUID(w["admin"].id)}
    # The grid shows it.
    act_as(w["lead"])
    grid = client.get(_url(w, "/attendance")).json()
    row = next(s for s in grid["students"] if s["entry_id"] == str(bob.id))
    cell = next(c for c in row["cells"] if c["session_id"] == str(cs.id))
    assert (cell["status"], cell["source"]) == ("present", "instructor")
    # Not a session that has not started; not a Student off the roll call; not another class's.
    future = _session(db, w, "Friday", NOW + timedelta(days=1), state=EventState.scheduled)
    db.commit()
    assert client.put(_url(w, f"/attendance/{future[0].id}/{bob.id}"), json={"status": "excused"}).status_code == 409
    assert client.put(_url(w, f"/attendance/{cs.id}/{uuid.uuid4()}"), json={"status": "excused"}).status_code == 404
    assert client.put(_url(w, f"/attendance/{uuid.uuid4()}/{bob.id}"), json={"status": "excused"}).status_code == 404
    assert client.put(_url(w, f"/attendance/{cs.id}/{bob.id}"), json={"status": "late"}).status_code == 422


def test_attendance_rbac_and_tenancy(client, db_session, w):
    db = db_session
    attendance.tick(db, now=NOW)
    db.commit()
    cs, _ = w["s1"]
    put = (_url(w, f"/attendance/{cs.id}/{w['entries']['ann'].id}"), {"status": "excused"})
    for who, read, edit, can_edit in (
        (w["lead"], 200, 200, True), (w["admin"], 200, 200, True),
        (w["assistant"], 200, 403, False), (w["observer"], 200, 403, False),
        (_as(w["students"]["ann"]), 403, 403, None),
        (w["stranger"], 404, 404, None), (w["b_admin"], 404, 404, None),
    ):
        act_as(who)
        r = client.get(_url(w, "/attendance"))
        assert r.status_code == read, (who.role, r.text)
        if read == 200:
            assert r.json()["can_edit"] is can_edit
            assert [s["name"] for s in r.json()["sessions"]] == ["Day 1", "Day 2"]
        assert client.put(put[0], json=put[1]).status_code == edit, who.role


# ── Close-out ────────────────────────────────────────────────────────────────


def _progress(db, w, name: str, score: int, max_score: int = 100) -> None:
    enrolment = db.query(Enrollment).filter_by(user_id=w["students"][name].id, course_id=w["course"].id).one()
    row = db.query(ModuleProgress).filter_by(enrollment_id=enrolment.id, module_id=w["module"].id).one()
    row.status, row.score, row.max_score = ModuleProgressStatus.completed, score, max_score
    db.flush()


def _standing(w, body: dict, name: str) -> dict:
    return next(s for s in body["students"] if s["entry_id"] == str(w["entries"][name].id))


@pytest.fixture
def graded(db_session, w):
    """ann attends both days and scores 90; bob both days and 40; cat neither day and 95."""
    db = db_session
    for key in ("s1", "s2"):
        _, evt = w[key]
        for name in ("ann", "bob"):
            _evidence(db, w, name, w["seats"][name], evt.start_time + timedelta(minutes=10))
    _progress(db, w, "ann", 90)
    _progress(db, w, "bob", 40)
    _progress(db, w, "cat", 95)
    sc = Scenario(id=uuid.uuid4(), name="Ransomware", yaml="id: s\n", tenant_id=w["a"].id)
    db.add(sc)
    db.flush()
    ex = Exercise(id=uuid.uuid4(), name="Friday", scenario_id=sc.id, range_id=w["shared"].id, tenant_id=w["a"].id,
                  state=ExerciseState.completed)
    db.add(ex)
    db.flush()
    w["s2"][1].exercise_id = ex.id
    for verdict in (ACHIEVED, ACHIEVED, MISSED):
        db.add(DetectionSubmission(tenant_id=w["a"].id, exercise_id=ex.id, objective_ref="o1",
                                   user_id=w["students"]["ann"].id, query="q", verdict=verdict))
    db.commit()
    return w


def test_closeout_prefills_from_attendance_and_scores(client, secret, graded):
    w = graded
    act_as(w["lead"])
    r = client.get(_url(w, "/closeout"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["numbers_shown"] is True and body["sessions_held"] == 2
    ann, bob, cat = (_standing(w, body, n) for n in ("ann", "bob", "cat"))
    assert (ann["present"], ann["absent"], ann["course_percent"], ann["detections_credited"]) == (2, 0, 90.0, 2)
    assert (ann["course_passed"], ann["suggested"]) == (True, "pass")
    assert (bob["course_passed"], bob["suggested"]) == (False, "fail")
    assert (cat["present"], cat["absent"], cat["suggested"]) == (0, 2, "incomplete")  # scores well; never came
    assert ann["outcome"] is None  # nothing decided yet


def test_unclassified_shows_pass_fail_only_and_stores_no_grade(client, unclassified, graded, db_session):
    w = graded
    act_as(w["lead"])
    body = client.get(_url(w, "/closeout")).json()
    assert body["numbers_shown"] is False
    for s in body["students"]:
        assert s["course_percent"] is None and s["detections_credited"] is None
    assert _standing(w, body, "ann")["course_passed"] is True  # the verdict, not the number
    r = client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"]["ann"].id), "outcome": "pass"},
        {"entry_id": str(w["entries"]["bob"].id), "outcome": "fail"},
        {"entry_id": str(w["entries"]["cat"].id), "outcome": "incomplete"},
    ]})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    ann = db_session.query(Enrollment).filter_by(user_id=w["students"]["ann"].id).one()
    assert ann.status == EnrollmentStatus.completed
    assert (ann.final_grade, ann.final_score, ann.max_score) == (None, 0, 0)
    bob = db_session.query(Enrollment).filter_by(user_id=w["students"]["bob"].id).one()
    assert bob.status == EnrollmentStatus.failed and bob.final_grade is None
    # The results table has nowhere to put a number.
    assert not {"score", "grade", "final_score", "final_grade", "percent"} & set(ClassResult.__table__.columns.keys())


def test_numbers_are_withheld_when_the_classification_cannot_be_read(monkeypatch):
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "not-a-level")
    assert closeout.numbers_allowed() is False


def test_closing_sends_results_to_enrolments_and_makes_the_class_read_only(client, secret, graded, db_session):
    w = graded
    db = db_session
    future = _session(db, w, "Friday", NOW + timedelta(days=1), state=EventState.scheduled)
    db.commit()
    entries = w["entries"]
    act_as(w["lead"])
    # Every Student needs a result.
    r = client.post(_url(w, "/close"), json={"results": [{"entry_id": str(entries["ann"].id), "outcome": "pass"}]})
    assert r.status_code == 422 and "bob@corp.test" in r.json()["detail"]
    r = client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(entries["ann"].id), "outcome": "pass", "comment": "Strong triage"},
        {"entry_id": str(entries["bob"].id), "outcome": "fail", "comment": "Below the mark"},
        {"entry_id": str(entries["cat"].id), "outcome": "incomplete"},
        {"entry_id": str(uuid.uuid4()), "outcome": "pass"},
    ]})
    assert r.status_code == 422  # someone not on the roll call
    r = client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(entries["ann"].id), "outcome": "pass", "comment": "Strong triage"},
        {"entry_id": str(entries["bob"].id), "outcome": "fail", "comment": "Below the mark"},
        {"entry_id": str(entries["cat"].id), "outcome": "incomplete"},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["class_status"] == "closed" and body["closed_at"]
    assert _standing(w, body, "ann")["outcome"] == "pass"
    db.expire_all()
    status = {n: db.query(Enrollment).filter_by(user_id=w["students"][n].id).one() for n in ("ann", "bob", "cat")}
    assert status["ann"].status == EnrollmentStatus.completed and status["ann"].final_grade == "A"  # Secret: kept
    assert status["bob"].status == EnrollmentStatus.failed
    assert status["cat"].status == EnrollmentStatus.enrolled  # incomplete: left open
    results = {r.entry_id: r for r in db.query(ClassResult)}
    assert results[entries["ann"].id].sessions_present == 2 and results[entries["ann"].id].suggested_outcome == "pass"
    assert db.query(ClassAttendance).count() == 6  # the close recorded both days
    assert db.query(AuditLog).filter_by(action="class_close").count() == 1
    assert db.query(AuditLog).filter_by(action="class_result").count() == 3
    assert db.get(ScheduledEvent, future[1].id).state == EventState.cancelled  # not started: cancelled
    # Read-only from now on.
    cid = w["cls"].id
    assert client.put(f"/classes/{cid}", json={
        "name": "Renamed", "course_id": str(w["course"].id), "start_date": str(date.today()),
        "end_date": str(date.today())}).status_code == 409
    assert client.post(f"/classes/{cid}/roll-call", json={"emails": ["new@corp.test"]}).status_code == 409
    assert client.delete(f"/classes/{cid}/roll-call/{entries['ann'].id}").status_code == 409
    assert client.put(_url(w, f"/attendance/{w['s1'][0].id}/{entries['bob'].id}"),
                      json={"status": "excused"}).status_code == 409
    assert client.post(_url(w, "/close"), json={"results": []}).status_code == 409
    assert client.delete(f"/classes/{cid}").status_code == 409
    # Still visible and listed, with its results.
    assert client.get(_url(w)).json()["status"] == "closed"
    assert client.get(_url(w)).json()["can_edit"] is False
    assert str(cid) in {c["id"] for c in client.get("/classes").json()}
    assert client.get(_url(w, "/attendance")).json()["can_edit"] is False
    # Its ranges come down: a class no longer open (ranges._why_done).
    cls = db.get(Class, cid)
    seat = db.query(ClassRange).filter_by(entry_id=entries["ann"].id).one()
    assert class_ranges._why_done(db, cls, seat, NOW, set()) == ("class_closed", None)


def test_only_an_admin_reopens_and_it_is_audited(client, secret, graded, db_session):
    w = graded
    act_as(w["admin"])
    assert client.post(_url(w, "/reopen"), json={"reason": "late evidence"}).status_code == 409  # not closed yet
    act_as(w["lead"])
    client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"][n].id), "outcome": o} for n, o in (("ann", "pass"), ("bob", "pass"),
                                                                         ("cat", "fail"))]})
    for who, code in ((w["lead"], 403), (w["assistant"], 403), (w["observer"], 403),
                      (_as(w["students"]["ann"]), 403), (w["b_admin"], 404)):
        act_as(who)
        assert client.post(_url(w, "/reopen"), json={"reason": "late evidence"}).status_code == code, who.role
    act_as(w["admin"])
    assert client.post(_url(w, "/reopen"), json={"reason": "  "}).status_code == 422
    r = client.post(_url(w, "/reopen"), json={"reason": "Bob's lab was marked late"})
    assert r.status_code == 200, r.text
    assert r.json()["class_status"] == "open"
    assert _standing(w, r.json(), "bob")["outcome"] == "pass"  # kept until the next close-out
    audit = db_session.query(AuditLog).filter_by(action="class_reopen").one()
    assert "marked late" in audit.detail and audit.user_id == uuid.UUID(w["admin"].id)
    # Closing again replaces the results; an incomplete reopens an enrolment this class closed.
    act_as(w["lead"])
    r = client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"][n].id), "outcome": o} for n, o in (("ann", "pass"), ("bob", "incomplete"),
                                                                         ("cat", "fail"))]})
    assert r.status_code == 200
    db_session.expire_all()
    bob = db_session.query(Enrollment).filter_by(user_id=w["students"]["bob"].id).one()
    assert bob.status == EnrollmentStatus.in_progress and bob.completed_at is None


def test_closeout_rbac_and_tenancy(client, secret, graded):
    w = graded
    results = {"results": [{"entry_id": str(w["entries"][n].id), "outcome": "pass"} for n in ("ann", "bob", "cat")]}
    for who, read in ((w["assistant"], 200), (w["observer"], 200), (_as(w["students"]["ann"]), 403),
                      (w["stranger"], 404), (w["b_admin"], 404)):
        act_as(who)
        assert client.get(_url(w, "/closeout")).status_code == read, who.role
        assert client.get(_url(w, "/results.csv")).status_code == read, who.role
        assert client.post(_url(w, "/close"), json=results).status_code in (403, 404), who.role
    act_as(w["admin"])
    assert client.post(_url(w, "/close"), json=results).status_code == 200


def test_the_export_defuses_formula_cells(client, secret, graded, db_session):
    w = graded
    w["students"]["ann"].display_name = '=HYPERLINK("http://evil.test","x")'
    db_session.commit()
    act_as(w["lead"])
    client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"]["ann"].id), "outcome": "pass", "comment": "+SUM(A1:A9)"},
        {"entry_id": str(w["entries"]["bob"].id), "outcome": "fail", "comment": "@cmd"},
        {"entry_id": str(w["entries"]["cat"].id), "outcome": "incomplete", "comment": "-1"},
    ]})
    r = client.get(_url(w, "/results.csv"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    by_email = {row["email"]: row for row in rows}
    assert by_email["ann@corp.test"]["name"].startswith("'=")
    assert by_email["ann@corp.test"]["comment"] == "'+SUM(A1:A9)"
    assert by_email["bob@corp.test"]["comment"] == "'@cmd"
    assert by_email["cat@corp.test"]["comment"] == "'-1"
    assert by_email["ann@corp.test"]["result"] == "pass" and by_email["ann@corp.test"]["present"] == "2"
    # Each session's attendance is a column; no percentage or score is.
    assert any(h.endswith("Day 1") for h in rows[0]) and not any("percent" in h or "score" in h for h in rows[0])
    for ch in ("=", "+", "-", "@", "\t", "\r"):
        assert closeout.csv_cell(f"{ch}x").startswith("'")
    assert closeout.csv_cell(" =x") == "' =x" and closeout.csv_cell("plain") == "plain"


# ── Review follow-ups ────────────────────────────────────────────────────────


def test_a_close_out_never_undoes_an_enrolment_it_did_not_make(client, unclassified, graded, db_session):
    """Completed some other way (self-paced, Moodle): a fail or an incomplete leaves it, and a
    pass does not re-grade it. Only this class's own completion is ever reopened."""
    w, db = graded, db_session
    done_at = NOW - timedelta(days=30)
    for name in ("ann", "bob"):
        e = db.query(Enrollment).filter_by(user_id=w["students"][name].id).one()
        e.status, e.completed_at, e.final_grade, e.final_score, e.max_score = (
            EnrollmentStatus.completed, done_at, "B", 85, 100)
    db.commit()
    act_as(w["lead"])
    r = client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"]["ann"].id), "outcome": "fail"},
        {"entry_id": str(w["entries"]["bob"].id), "outcome": "pass"},
        {"entry_id": str(w["entries"]["cat"].id), "outcome": "pass"},
    ]})
    assert r.status_code == 200, r.text
    db.expire_all()
    for name in ("ann", "bob"):
        e = db.query(Enrollment).filter_by(user_id=w["students"][name].id).one()
        assert (e.status, e.final_grade, e.final_score) == (EnrollmentStatus.completed, "B", 85), name
    assert any("completed elsewhere: kept (was completed)" in a.detail
               for a in db.query(AuditLog).filter_by(action="class_result"))
    # Reopen and close everyone as incomplete: cat's enrolment, which this class completed, is
    # reopened; ann's and bob's, completed elsewhere, are still left alone.
    act_as(w["admin"])
    assert client.post(_url(w, "/reopen"), json={"reason": "recount"}).status_code == 200
    act_as(w["lead"])
    assert client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"][n].id), "outcome": "incomplete"} for n in ("ann", "bob", "cat")]}
    ).status_code == 200
    db.expire_all()
    got = {n: db.query(Enrollment).filter_by(user_id=w["students"][n].id).one().status for n in ("ann", "bob", "cat")}
    assert got == {"ann": EnrollmentStatus.completed, "bob": EnrollmentStatus.completed,
                   "cat": EnrollmentStatus.in_progress}


@pytest.mark.parametrize("state, start", [
    (EventState.active, NOW - timedelta(hours=1)),
    (EventState.provisioning, NOW + timedelta(minutes=20)),  # its ranges are being built
    (EventState.scheduled, NOW - timedelta(minutes=30)),  # under way; the clock has not moved it yet
])
def test_no_close_while_a_session_runs(client, secret, graded, db_session, state, start):
    w = graded
    _session(db_session, w, "Today", start, state=state)
    db_session.commit()
    act_as(w["lead"])
    r = client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"][n].id), "outcome": "pass"} for n in ("ann", "bob", "cat")]})
    assert r.status_code == 409 and "Today" in r.json()["detail"]
    assert db_session.get(Class, w["cls"].id).status == "open"


def test_a_held_session_the_clock_left_scheduled_stays_on_the_record(client, secret, graded, db_session):
    w, db = graded, db_session
    w["s2"][1].state = EventState.scheduled  # held back (a maintenance window), but it is over
    db.commit()
    act_as(w["lead"])
    assert client.post(_url(w, "/close"), json={"results": [
        {"entry_id": str(w["entries"][n].id), "outcome": "pass"} for n in ("ann", "bob", "cat")]}).status_code == 200
    db.expire_all()
    assert db.get(ScheduledEvent, w["s2"][1].id).state == EventState.scheduled  # not cancelled
    assert client.get(_url(w, "/closeout")).json()["sessions_held"] == 2


def test_an_open_before_a_session_starts_does_not_silence_one_during_it(db_session, w):
    db = db_session
    seat, ann = w["seats"]["ann"], w["students"]["ann"]
    start = NOW + timedelta(minutes=5)
    _session(db, w, "Soon", start, state=EventState.scheduled)
    db.commit()
    seen = {"class_seat_id": "x"}
    assert attendance.note_range_open(db, ann.id, seat, seen, now=start - timedelta(minutes=4))
    db.flush()
    assert not attendance.note_range_open(db, ann.id, seat, seen, now=start - timedelta(minutes=1))
    # Four minutes into the session: within ten minutes of the last open, but a session began since.
    assert attendance.note_range_open(db, ann.id, seat, seen, now=start + timedelta(minutes=4))
    db.flush()
    assert not attendance.note_range_open(db, ann.id, seat, seen, now=start + timedelta(minutes=6))
    assert not attendance.note_range_open(db, ann.id, seat, None)  # not a class range: nothing


def test_a_reinstated_student_still_owes_the_sessions_in_between(db_session, w):
    db = db_session
    cat = w["entries"]["cat"]
    cat.created_at = D1 - timedelta(days=1)
    cat.forfeited_at = D1 + timedelta(hours=2)
    cat.joined_at = NOW - timedelta(hours=1)  # reinstated today
    dan = _student(db, w["a"].id, "dan")
    late = RollCallEntry(class_id=w["cls"].id, tenant_id=w["a"].id, email=dan.email, user_id=dan.id,
                         status="signed_in", joined_at=NOW - timedelta(hours=1))
    db.add(late)
    db.commit()
    attendance.record_class(db, w["cls"], NOW)
    rows = {(r.session_id, r.entry_id): r.status for r in db.query(ClassAttendance)}
    assert rows[(w["s1"][0].id, cat.id)] == rows[(w["s2"][0].id, cat.id)] == "absent"
    assert (w["s1"][0].id, late.id) not in rows  # a genuinely late addition owes nothing


def test_a_pending_addition_on_a_closed_class_is_not_decided(db_session, w):
    from app.classes import service as class_service
    from fastapi import HTTPException

    db = db_session
    pending = RollCallEntry(class_id=w["cls"].id, tenant_id=w["a"].id, email="late@corp.test", status="pending")
    db.add(pending)
    w["cls"].status = "closed"
    db.flush()
    with pytest.raises(HTTPException) as refused:
        class_service.reject_addition(db, w["cls"], pending, w["admin"], "no room")
    assert refused.value.status_code == 409
