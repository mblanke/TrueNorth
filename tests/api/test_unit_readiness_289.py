"""Observers 12: the unit readiness page (ADR 0014 decision 7; issue #289).

The rules under test:

- An observer (the commander), an instructor or an admin reads the readiness of their own
  tenant: its classes in a date range, each Student's enrolment, recorded attendance, result
  (pass, fail or incomplete, with the instructor's comment) and the qualification the result
  earned. A Student is refused (403), and range ops too.
- Strict tenant isolation: another tenant's classes, Students and results never appear,
  whoever asks, and no query parameter widens it.
- "Unit" is the Student's ``users.unit`` (from the directory or registration); the filter
  keeps only the Students of that unit, case and spaces ignored, and the classes they are on.
- Classes are filtered by date overlap; cancelled classes are left out.
- The page only reads: nothing in the database changes, except an audit row for an export.
- CSV and PDF exports carry the same rows; CSV cells a spreadsheet would run are defused.
- The number of queries does not grow with the number of classes or Students (no N+1).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app.classes.models import (
    Class,
    ClassAttendance,
    ClassCloseout,
    ClassResult,
    ClassSession,
    RollCallEntry,
)
from app.models import (
    AuditLog,
    Course,
    Enrollment,
    EnrollmentStatus,
    Qualification,
    User,
    UserRole,
)
from app.scheduler.models import EventState, ScheduledEvent
from sqlalchemy import event

NOW = datetime.now(UTC).replace(microsecond=0)
TODAY = NOW.date()


def _student(db, tenant_id, name: str, unit: str | None) -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=f"{name}-{uuid.uuid4().hex[:6]}@corp.test",
             display_name=name.title(), role=UserRole.student, tenant_id=tenant_id, unit=unit)
    db.add(u)
    db.flush()
    return u


def _class(db, tenant_id, course, lead, name: str, start: date, end: date, status: str = "open") -> Class:
    cls = Class(id=uuid.uuid4(), tenant_id=tenant_id, name=name, course_id=course.id, start_date=start, end_date=end,
                lead_instructor_id=uuid.UUID(lead.id), status=status)
    db.add(cls)
    db.flush()
    return cls


def _session(db, cls: Class, lead, start: datetime) -> ClassSession:
    evt = ScheduledEvent(id=uuid.uuid4(), name=f"{cls.name} day", state=EventState.completed, tenant_id=cls.tenant_id,
                         instructor_id=uuid.UUID(lead.id), start_time=start, end_time=start + timedelta(hours=3))
    db.add(evt)
    db.flush()
    cs = ClassSession(id=uuid.uuid4(), class_id=cls.id, event_id=evt.id, tenant_id=cls.tenant_id,
                      range_mode="individual")
    db.add(cs)
    db.flush()
    return cs


def _seat(db, cls: Class, u: User | None, email: str | None = None, status: str = "signed_in") -> RollCallEntry:
    e = RollCallEntry(id=uuid.uuid4(), class_id=cls.id, tenant_id=cls.tenant_id, email=(u.email if u else email),
                      user_id=u.id if u else None, status=status)
    db.add(e)
    db.flush()
    if u is not None and db.query(Enrollment).filter_by(user_id=u.id, course_id=cls.course_id).first() is None:
        db.add(Enrollment(id=uuid.uuid4(), user_id=u.id, course_id=cls.course_id, tenant_id=cls.tenant_id,
                          status=EnrollmentStatus.enrolled))
        db.flush()
    return e


def _attend(db, cls: Class, cs: ClassSession, e: RollCallEntry, status: str) -> None:
    db.add(ClassAttendance(tenant_id=cls.tenant_id, class_id=cls.id, session_id=cs.id, entry_id=e.id,
                           user_id=e.user_id, status=status, source="auto"))
    db.flush()


def _result(db, cls: Class, e: RollCallEntry, outcome: str, comment: str | None = None) -> None:
    db.add(ClassResult(tenant_id=cls.tenant_id, class_id=cls.id, entry_id=e.id, user_id=e.user_id, outcome=outcome,
                       suggested_outcome=outcome, comment=comment, decided_at=NOW))
    db.flush()


@pytest.fixture
def w(db_session):
    """Tenant A: a closed class (Ann passed, Bob failed with a comment, an invited Student
    who never signed in), an open class, a cancelled one and one long ago. Tenant B: a class
    whose Student is in the same unit name, which must never show up for A."""
    db = db_session
    a, b = real_tenant(db, "rd-a"), real_tenant(db, "rd-b")
    qual = Qualification(id=uuid.uuid4(), qsp_code=f"RD{uuid.uuid4().hex[:6]}", nqual="ALJQ",
                         title="Cyber operator, junior")
    db.add(qual)
    db.flush()
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True, qualification_id=qual.id)
    plain = Course(id=uuid.uuid4(), name="Linux basics", tenant_id=a.id, is_published=True)
    b_course = Course(id=uuid.uuid4(), name="B course", tenant_id=b.id, is_published=True)
    db.add_all([course, plain, b_course])
    db.flush()
    w = {"a": a, "b": b, "qual": qual,
         "observer": real_user(db, UserRole.observer, a.id), "instructor": real_user(db, UserRole.instructor, a.id),
         "admin": real_user(db, UserRole.admin, a.id), "range_ops": real_user(db, UserRole.range_ops, a.id),
         "student_user": real_user(db, UserRole.student, a.id),
         "b_observer": real_user(db, UserRole.observer, b.id), "b_admin": real_user(db, UserRole.admin, b.id)}
    lead = w["instructor"]
    ann = _student(db, a.id, "ann", "2 Sig Regt")
    bob = _student(db, a.id, "bob", " 2 sig regt ")
    cat = _student(db, a.id, "cat", "Kingston Det")
    closed = _class(db, a.id, course, lead, "C201-26-03", TODAY - timedelta(days=20), TODAY - timedelta(days=16),
                    status="closed")
    db.add(ClassCloseout(class_id=closed.id, tenant_id=a.id, closed_at=NOW - timedelta(days=15),
                         closed_by=uuid.UUID(lead.id)))
    d1 = _session(db, closed, lead, NOW - timedelta(days=20))
    d2 = _session(db, closed, lead, NOW - timedelta(days=19))
    e_ann, e_bob = _seat(db, closed, ann), _seat(db, closed, bob)
    e_inv = _seat(db, closed, None, email="nobody@corp.test", status="forfeited")
    _seat(db, closed, None, email="removed@corp.test", status="removed")
    for cs in (d1, d2):
        _attend(db, closed, cs, e_ann, "present")
    _attend(db, closed, d1, e_bob, "present")
    _attend(db, closed, d2, e_bob, "excused")
    _attend(db, closed, d1, e_inv, "absent")
    _result(db, closed, e_ann, "pass", "Solid triage")
    _result(db, closed, e_bob, "fail", "=HYPERLINK(\"http://x\")")
    _result(db, closed, e_inv, "incomplete")
    db.query(Enrollment).filter_by(user_id=ann.id, course_id=course.id).update({"status": EnrollmentStatus.completed})
    upcoming = _class(db, a.id, plain, lead, "LX-26-07", TODAY + timedelta(days=5), TODAY + timedelta(days=9))
    _seat(db, upcoming, cat)
    _seat(db, upcoming, ann)
    cancelled = _class(db, a.id, plain, lead, "LX-26-06", TODAY - timedelta(days=3), TODAY - timedelta(days=1),
                       status="cancelled")
    _seat(db, cancelled, cat)
    old = _class(db, a.id, plain, lead, "LX-24-01", TODAY - timedelta(days=900), TODAY - timedelta(days=896))
    _seat(db, old, cat)
    b_lead = real_user(db, UserRole.instructor, b.id)
    b_cls = _class(db, b.id, b_course, b_lead, "B-SECRET", TODAY - timedelta(days=20), TODAY - timedelta(days=16))
    b_stu = _student(db, b.id, "zed", "2 Sig Regt")
    e_zed = _seat(db, b_cls, b_stu)
    _result(db, b_cls, e_zed, "pass", "other tenant")
    db.commit()
    w.update(course=course, plain=plain, closed=closed, upcoming=upcoming, cancelled=cancelled, old=old,
             b_cls=b_cls, ann=ann, bob=bob, cat=cat, e_ann=e_ann, e_bob=e_bob)
    return w


def _get(client, who, path: str = "/readiness", **params):
    act_as(who)
    return client.get(path, params=params)


# ── Who may read it ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["observer", "instructor", "admin"])
def test_staff_who_hold_readiness_read_see_their_tenant(client, w, role):
    r = _get(client, w[role])
    assert r.status_code == 200, r.text
    names = [c["name"] for c in r.json()["classes"]]
    assert names == ["C201-26-03", "LX-26-07"]


@pytest.mark.parametrize("role", ["student_user", "range_ops"])
def test_students_and_range_ops_are_refused(client, w, role):
    for path in ("/readiness", "/readiness/export.csv", "/readiness/export.pdf"):
        assert _get(client, w[role], path).status_code == 403, path


@pytest.mark.parametrize("role", ["b_observer", "b_admin"])
def test_another_tenant_sees_only_its_own(client, w, role):
    body = _get(client, w[role], date_from=str(TODAY - timedelta(days=60))).json()
    assert [c["name"] for c in body["classes"]] == ["B-SECRET"]
    flat = str(body)
    for leaked in ("C201-26-03", "LX-26-07", "Solid triage", w["ann"].email):
        assert leaked not in flat
    csv_text = _get(client, w[role], "/readiness/export.csv").text
    assert "C201-26-03" not in csv_text and "Solid triage" not in csv_text


def test_tenant_a_never_sees_tenant_b_even_by_unit(client, w):
    body = _get(client, w["observer"], unit="2 Sig Regt").json()
    flat = str(body)
    assert "B-SECRET" not in flat and "other tenant" not in flat
    assert {s["display_name"] for c in body["classes"] for s in c["students"]} == {"Ann", "Bob"}


# ── What it shows ────────────────────────────────────────────────────────────


def test_classes_enrolment_attendance_results_and_qualifications(client, w):
    body = _get(client, w["observer"]).json()
    closed = next(c for c in body["classes"] if c["name"] == "C201-26-03")
    assert closed["status"] == "closed" and closed["closed_at"]
    assert closed["course_name"] == "SOC Analyst"
    assert closed["qualification"] == "ALJQ"
    assert closed["sessions_held"] == 2
    # The removed entry was never on the roll call; the forfeited one was expected and stays.
    assert closed["enrolled"] == 3
    rows = {s["email"]: s for s in closed["students"]}
    assert "removed@corp.test" not in rows
    ann, bob, inv = rows[w["ann"].email], rows[w["bob"].email], rows["nobody@corp.test"]
    assert (ann["present"], ann["excused"], ann["absent"]) == (2, 0, 0)
    assert (bob["present"], bob["excused"], bob["absent"]) == (1, 1, 0)
    assert (inv["present"], inv["excused"], inv["absent"]) == (0, 0, 1)
    assert ann["outcome"] == "pass" and ann["comment"] == "Solid triage"
    assert bob["outcome"] == "fail"
    assert inv["outcome"] == "incomplete" and inv["display_name"] is None and inv["unit"] is None
    assert ann["enrolment_status"] == "completed" and bob["enrolment_status"] == "enrolled"
    assert inv["enrolment_status"] is None
    assert ann["qualification_earned"] == "ALJQ"
    assert bob["qualification_earned"] is None
    assert ann["unit"] == "2 Sig Regt"
    assert (closed["passed"], closed["failed"], closed["incomplete"], closed["awaiting_result"]) == (1, 1, 1, 0)
    upcoming = next(c for c in body["classes"] if c["name"] == "LX-26-07")
    assert upcoming["qualification"] is None and upcoming["sessions_held"] == 0
    assert upcoming["awaiting_result"] == 2
    assert all(s["outcome"] is None and s["qualification_earned"] is None for s in upcoming["students"])
    assert body["qualifications"] == [
        {"qualification_id": str(w["qual"].id), "code": "ALJQ", "title": "Cyber operator, junior", "earned": 1}]
    s = body["summary"]
    assert s["classes"] == 2 and s["enrolments"] == 5 and s["students"] == 4
    assert (s["passed"], s["failed"], s["incomplete"], s["awaiting_result"]) == (1, 1, 1, 2)
    assert (s["present"], s["excused"], s["absent"]) == (3, 1, 1)
    assert s["qualifications_earned"] == 1


def test_results_carry_no_numeric_grade(client, w):
    body = _get(client, w["observer"]).json()
    student = body["classes"][0]["students"][0]
    for numeric in ("score", "final_score", "percent", "course_percent", "final_grade", "detections_credited"):
        assert numeric not in student


def test_units_are_listed_and_filter_case_and_space_blind(client, w):
    body = _get(client, w["observer"]).json()
    assert body["units"] == ["2 Sig Regt", "Kingston Det"]
    only = _get(client, w["observer"], unit="  2 SIG REGT").json()
    assert [c["name"] for c in only["classes"]] == ["C201-26-03", "LX-26-07"]
    assert {s["display_name"] for c in only["classes"] for s in c["students"]} == {"Ann", "Bob"}
    assert only["units"] == ["2 Sig Regt", "Kingston Det"]  # the choices stay all of them
    kingston = _get(client, w["observer"], unit="Kingston Det").json()
    assert [c["name"] for c in kingston["classes"]] == ["LX-26-07"]
    assert kingston["summary"]["qualifications_earned"] == 0 and kingston["qualifications"] == []


def test_dates_filter_by_overlap_and_cancelled_classes_are_left_out(client, w):
    body = _get(client, w["observer"], date_from=str(TODAY - timedelta(days=1000)),
                date_to=str(TODAY + timedelta(days=30))).json()
    assert [c["name"] for c in body["classes"]] == ["LX-24-01", "C201-26-03", "LX-26-07"]
    edge = _get(client, w["observer"], date_from=str(TODAY - timedelta(days=16)),
                date_to=str(TODAY - timedelta(days=16))).json()
    assert [c["name"] for c in edge["classes"]] == ["C201-26-03"]
    assert _get(client, w["observer"], date_from=str(TODAY), date_to=str(TODAY - timedelta(days=1))).status_code == 422
    assert _get(client, w["observer"], date_from=str(TODAY - timedelta(days=4000)),
                date_to=str(TODAY)).status_code == 422


def test_reading_changes_nothing(client, db_session, w):
    def snapshot():
        db_session.expire_all()
        return (db_session.query(ClassResult).count(), db_session.query(ClassAttendance).count(),
                db_session.query(Enrollment).count(), db_session.query(AuditLog).count(),
                [(c.id, c.status) for c in db_session.query(Class).order_by(Class.id)])

    before = snapshot()
    assert _get(client, w["observer"]).status_code == 200
    assert snapshot() == before


# ── Exports ──────────────────────────────────────────────────────────────────


def test_csv_export_has_a_row_per_student_and_defuses_formulas(client, db_session, w):
    r = _get(client, w["observer"], "/readiness/export.csv", unit="2 Sig Regt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 3  # Ann and Bob on C201, Ann on LX-26-07
    bob = next(x for x in rows if x["name"] == "Bob")
    assert bob["result"] == "fail"
    assert bob["comment"].startswith("'=")
    assert bob["class"] == "C201-26-03" and bob["unit"] == "2 sig regt"
    ann = next(x for x in rows if x["name"] == "Ann" and x["class"] == "C201-26-03")
    assert ann["qualification_earned"] == "ALJQ" and ann["present"] == "2"
    audit = db_session.query(AuditLog).filter_by(action="readiness_export").one()
    assert audit.tenant_id == w["a"].id and "csv" in audit.detail


def test_pdf_export(client, db_session, w):
    r = _get(client, w["admin"], "/readiness/export.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert db_session.query(AuditLog).filter_by(action="readiness_export").count() == 1


def test_pdf_renders_text_outside_latin1(db_session, w):
    from app.classes import readiness, readiness_pdf

    db_session.query(User).filter_by(id=w["ann"].id).update({"display_name": "Zoë “Ann” 张"})
    db_session.flush()
    report = readiness.build(db_session, w["a"].id, TODAY - timedelta(days=60), TODAY + timedelta(days=30), None)
    assert readiness_pdf.render(report, compress=False).startswith(b"%PDF")


# ── Scale ────────────────────────────────────────────────────────────────────


def _count_queries(db_session, fn) -> int:
    seen = []
    engine = db_session.get_bind()

    def before(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return len(seen)


def test_query_count_does_not_grow_with_students(db_session, w):
    from app.classes import readiness

    tenant_id = w["a"].id  # read before counting: an expired attribute is a query of its own

    def run():
        readiness.build(db_session, tenant_id, TODAY - timedelta(days=60), TODAY + timedelta(days=30), None)

    small = _count_queries(db_session, run)
    lead = w["instructor"]
    for i in range(4):
        cls = _class(db_session, w["a"].id, w["course"], lead, f"BIG-{i}", TODAY - timedelta(days=10),
                     TODAY - timedelta(days=8))
        cs = _session(db_session, cls, lead, NOW - timedelta(days=10))
        for j in range(60):
            u = _student(db_session, w["a"].id, f"s{i}-{j}", f"Unit {j % 7}")
            e = _seat(db_session, cls, u)
            _attend(db_session, cls, cs, e, "present")
            _result(db_session, cls, e, "pass", "ok")
    db_session.flush()
    big = _count_queries(db_session, run)
    assert big == small, (small, big)
    report = readiness.build(db_session, w["a"].id, TODAY - timedelta(days=60), TODAY + timedelta(days=30), None)
    assert report.summary.enrolments == 5 + 240
