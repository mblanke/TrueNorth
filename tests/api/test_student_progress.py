# Author: Marc-Andre Blanke
# Created: 2026-11-27T00:00:00Z
"""The Students page (app/classes/student_progress.py): every Student at a glance, and one
Student's record, for instructors and admins.

The rules under test:

- Instructors and admins (readiness:read + learning_record:read) read it; observers (who hold
  only readiness:read), Students and range ops are refused (403).
- Strict tenant isolation: another tenant's Students never appear, and asking for one by id
  is 404, as is a non-Student or an unknown id.
- The numbers are the readiness page's: same attendance, results and qualifications per
  class, summed per Student. Cancelled classes are left out.
- Proficiency is a level name from the Student's NICE assertions, never a score.
- Filters: formation (case and spaces ignored) and a search over name, email and callsign.
- It only reads.

Reuses the readiness tests' world (``test_unit_readiness_289.w``): tenant A has Ann (passed
C201-26-03, ALJQ), Bob (failed it), Cat (classes but no results); tenant B has Zed.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import act_as, real_user
from app.classes import student_progress
from app.models import AuditLog, Competency, CompetencyAssertion, CompetencyFramework, ProficiencyLevel, UserRole
from test_unit_readiness_289 import _get, world


@pytest.fixture
def w(db_session):
    return world(db_session)


def _nice(db, code: str, category: str) -> Competency:
    c = Competency(
        code=f"{code}-{uuid.uuid4().hex[:4]}", name=code, framework=CompetencyFramework.nice, category=category
    )
    db.add(c)
    db.flush()
    return c


def _rate(db, user, comp, level: ProficiencyLevel) -> None:
    db.add(CompetencyAssertion(user_id=user.id, competency_id=comp.id, proficiency=level))
    db.flush()


# ── Who may read it ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["instructor", "admin"])
def test_instructors_and_admins_see_their_tenants_students(client, w, role):
    r = _get(client, w[role], "/students")
    assert r.status_code == 200, r.text
    emails = {s["email"] for s in r.json()["students"]}
    assert {w["ann"].email, w["bob"].email, w["cat"].email} <= emails


@pytest.mark.parametrize("role", ["observer", "student_user", "range_ops"])
def test_observers_students_and_range_ops_are_refused(client, w, role):
    assert _get(client, w[role], "/students").status_code == 403
    assert _get(client, w[role], f"/students/{w['ann'].id}").status_code == 403


def test_another_tenants_students_never_appear(client, w):
    body = _get(client, w["b_admin"], "/students").json()
    flat = str(body)
    for leaked in (w["ann"].email, w["bob"].email, "Solid triage", "C201-26-03"):
        assert leaked not in flat
    assert [s["display_name"] for s in body["students"]] == ["Zed"]


def test_a_record_outside_the_tenant_or_not_a_student_is_404(client, w):
    assert _get(client, w["b_admin"], f"/students/{w['ann'].id}").status_code == 404
    assert _get(client, w["admin"], f"/students/{w['instructor'].id}").status_code == 404
    assert _get(client, w["admin"], f"/students/{uuid.uuid4()}").status_code == 404


# ── What it shows ────────────────────────────────────────────────────────────


def test_rows_sum_the_readiness_numbers(client, w):
    rows = {s["email"]: s for s in _get(client, w["instructor"], "/students").json()["students"]}
    ann, bob, cat = rows[w["ann"].email], rows[w["bob"].email], rows[w["cat"].email]
    # Ann: the closed class (2 present, pass, ALJQ) and the upcoming one (no result yet).
    assert ann["classes"] == 2 and (ann["present"], ann["excused"], ann["absent"]) == (2, 0, 0)
    assert (ann["passed"], ann["failed"], ann["awaiting_result"]) == (1, 0, 1)
    assert ann["qualifications"] == ["ALJQ"] and ann["unit"] == "2 Sig Regt"
    assert (bob["present"], bob["excused"], bob["failed"], bob["qualifications"]) == (1, 1, 1, [])
    # Cat: the upcoming class and the old one; the cancelled one is left out.
    assert cat["classes"] == 2 and cat["awaiting_result"] == 2


def test_record_lists_each_class_newest_first(client, w):
    body = _get(client, w["instructor"], f"/students/{w['ann'].id}").json()
    assert [c["name"] for c in body["class_history"]] == ["LX-26-07", "C201-26-03"]
    closed = body["class_history"][1]
    assert closed["outcome"] == "pass" and closed["comment"] == "Solid triage"
    assert closed["qualification_earned"] == "ALJQ" and closed["sessions_held"] == 2
    assert body["display_name"] == "Ann" and body["qualifications"] == ["ALJQ"]


def test_proficiency_is_a_level_from_nice_assertions(client, db_session, w):
    ann = w["ann"]
    analyze, defend = _nice(db_session, "AN-X", "Analyze"), _nice(db_session, "PR-X", "Protect and Defend")
    _rate(db_session, ann, analyze, ProficiencyLevel.expert)  # 95
    _rate(db_session, ann, defend, ProficiencyLevel.intermediate)  # 50
    _rate(db_session, ann, _nice(db_session, "T0001", "task"), ProficiencyLevel.novice)  # no category: ignored
    db_session.commit()
    body = _get(client, w["instructor"], f"/students/{ann.id}").json()
    assert body["proficiency"] == "advanced"  # mean 72.5
    assert body["competencies"] == [
        {"category": "Analyze", "proficiency": "expert", "assertions": 1},
        {"category": "Protect & Defend", "proficiency": "intermediate", "assertions": 1},
    ]
    row = next(s for s in _get(client, w["instructor"], "/students").json()["students"] if s["user_id"] == str(ann.id))
    assert row["proficiency"] == "advanced"
    assert (
        next(
            s for s in _get(client, w["instructor"], "/students").json()["students"] if s["user_id"] == str(w["cat"].id)
        )["proficiency"]
        is None
    )


def test_no_score_percentage_or_grade_anywhere(client, w):
    body = _get(client, w["instructor"], f"/students/{w['ann'].id}").json()
    keys = set(body) | {k for c in body["class_history"] for k in c}
    assert not keys & {"score", "grade", "percent", "percentage", "final_score"}


def test_unit_filter_and_search(client, w):
    by_unit = _get(client, w["instructor"], "/students", unit="  2 SIG REGT ").json()
    assert {s["display_name"] for s in by_unit["students"]} == {"Ann", "Bob"}
    assert "2 Sig Regt" in by_unit["units"] and "Kingston Det" in by_unit["units"]
    assert [s["display_name"] for s in _get(client, w["instructor"], "/students", q="CAT").json()["students"]] == [
        "Cat"
    ]
    assert _get(client, w["instructor"], "/students", q="nobody-here").json()["students"] == []


def test_inactive_and_deleted_students_are_left_out(client, db_session, w):
    w["bob"].is_active = False
    db_session.commit()
    emails = {s["email"] for s in _get(client, w["instructor"], "/students").json()["students"]}
    assert w["bob"].email not in emails
    assert _get(client, w["instructor"], f"/students/{w['bob'].id}").status_code == 404


def test_reading_writes_nothing(client, db_session, w):
    before = db_session.query(AuditLog).count()
    _get(client, w["admin"], "/students")
    _get(client, w["admin"], f"/students/{w['ann'].id}")
    assert db_session.query(AuditLog).count() == before


@pytest.mark.parametrize(
    ("mean", "level"),
    [
        (None, None),
        (10, "novice"),
        (19.9, "novice"),
        (20, "beginner"),
        (50, "intermediate"),
        (62.5, "advanced"),
        (84.9, "advanced"),
        (85, "expert"),
        (95, "expert"),
    ],
)
def test_level_for(mean, level):
    assert student_progress.level_for(mean) == level


def test_a_student_in_no_class_still_shows(client, db_session, w):
    new = real_user(db_session, UserRole.student, w["a"].id)
    db_session.commit()
    act_as(w["admin"])
    row = next(s for s in client.get("/students").json()["students"] if s["user_id"] == str(new.id))
    assert row["classes"] == 0 and row["qualifications"] == [] and row["proficiency"] is None


def test_sorted_by_formation_then_surname_with_no_formation_last(client, db_session, w):
    # Ranked display names would sort by rank; the surname decides.
    w["ann"].display_name, w["ann"].last_name = "Sgt Ann Zimmer", "Zimmer"
    w["bob"].display_name, w["bob"].last_name = "Pte Bob Adams", "Adams"
    db_session.commit()
    names = [s["display_name"] for s in _get(client, w["admin"], "/students").json()["students"]]
    assert names.index("Pte Bob Adams") < names.index("Sgt Ann Zimmer")  # same formation: Adams first
    units = [s["unit"] for s in _get(client, w["admin"], "/students").json()["students"]]
    assert units[-1] is None and units[0] is not None  # Students with no formation come last


def test_a_class_months_ahead_is_on_the_record(client, db_session, w):
    from datetime import timedelta

    from test_unit_readiness_289 import TODAY, _class, _seat

    later = _class(
        db_session,
        w["a"].id,
        w["plain"],
        w["instructor"],
        "LX-27-02",
        TODAY + timedelta(days=120),
        TODAY + timedelta(days=124),
    )
    _seat(db_session, later, w["cat"])
    db_session.commit()
    body = _get(client, w["instructor"], f"/students/{w['cat'].id}").json()
    assert "LX-27-02" in [c["name"] for c in body["class_history"]]
