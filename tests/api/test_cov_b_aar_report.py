# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""What the stored after-action report contains (app/aar_report.py, ``build_report``).

The report is the JSON ``POST /exercises/{id}/aar/generate`` stores. Injects come from the
scenario's planned timeline plus the MESL serials that actually ran; the timeline is what
happened, in time order — audit events, falling back to the exercise's own stamps when the
worker completed a run without writing audit rows — plus achieved objectives and analyst
annotations. Participants are the exercise's team members, then anyone the tenant assessed
or who annotated, each with their team or role.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from _shared import real_exercise, real_tenant, real_user
from app.aar_report import build_report
from app.models import (
    AnalystAnnotation,
    AuditLog,
    CompetencyAutoAssessment,
    MeslEvent,
    Objective,
    ObjectiveType,
    Scenario,
    Team,
    TeamMembership,
    UserRole,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _world(db_session, **ex_fields):
    a = real_tenant(db_session, "aar-report")
    ex = real_exercise(db_session, a.id, "id: s\n", started_at=T0, completed_at=T0 + timedelta(hours=2), **ex_fields)
    scenario = db_session.query(Scenario).filter(Scenario.id == ex.scenario_id).one()
    return {"db": db_session, "ex": ex, "scenario": scenario}


def _audit(db, ex, action, at):
    db.add(AuditLog(action=action, resource_type="exercise", resource_id=str(ex.id), timestamp=at))
    db.flush()


# ── injects: the scenario's planned timeline ───────────────────────────
def test_a_timeline_step_uses_its_description_and_offset(db_session):
    w = _world(db_session)
    w["scenario"].yaml = (
        "id: s\ntimeline:\n"
        "  - t: 5\n    action: phishing_email\n    description: Spearphish link\n"
        "    params: {mitre_technique: T1566.002, target: Blue}\n"
    )
    report = build_report(w["db"], w["ex"], w["scenario"], "tester")
    assert report["injects"] == [
        {"at": "T+5", "title": "phishing_email", "detail": "Spearphish link", "status": "", "source": "scenario"}
    ]


def test_a_step_without_a_description_sums_its_key_params(db_session):
    w = _world(db_session)
    w["scenario"].yaml = (
        "id: s\ntimeline:\n"
        "  - t: 30\n    name: c2 beacon\n    params: {subject: DNS, mitre_technique: T1071, other: x}\n"
        '  - t: 40\n    action: ""\n    params: not-a-dict\n'
    )
    report = build_report(w["db"], w["ex"], w["scenario"], "tester")
    assert [(i["title"], i["detail"]) for i in report["injects"]] == [
        ("c2 beacon", "mitre_technique: T1071, subject: DNS"),
        ("inject", ""),  # params not a dict, no action/name: an unnamed step
    ]


def test_steps_that_are_not_mappings_and_an_unparseable_scenario_yield_nothing(db_session):
    w = _world(db_session)
    w["scenario"].yaml = "id: s\ntimeline:\n  - 5\n  - [a, b]\n"
    assert build_report(w["db"], w["ex"], w["scenario"], "t")["injects"] == []
    w["scenario"].yaml = "timeline: [unclosed"
    assert build_report(w["db"], w["ex"], w["scenario"], "t")["injects"] == []


# ── injects: the MESL serials that ran ─────────────────────────────────
def test_mesl_serials_are_reported_in_order_with_their_route(db_session):
    w = _world(db_session)
    db = w["db"]
    db.add(
        MeslEvent(
            exercise_id=w["ex"].id,
            serial=2,
            scenario_time="D1 0900",
            title="C2 established",
            description="Beacon over DNS",
            from_cell="OPFOR",
            to_participant="Blue Team",
            status="delivered",
        )
    )
    db.add(MeslEvent(exercise_id=w["ex"].id, serial=1, title="Kickoff", status="responded"))
    db.flush()
    injects = build_report(db, w["ex"], w["scenario"], "tester")["injects"]
    assert [(i["at"], i["title"], i["detail"], i["source"]) for i in injects] == [
        ("Serial 001", "Kickoff", "", "mesl"),  # no scenario_time: named by serial
        ("D1 0900", "C2 established", "OPFOR -> Blue Team / Beacon over DNS", "mesl"),
    ]


# ── the timeline ───────────────────────────────────────────────────────
def test_without_audit_rows_the_exercises_own_stamps_stand_in(db_session):
    w = _world(db_session)
    timeline = build_report(w["db"], w["ex"], w["scenario"], "tester")["timeline"]
    assert [(e["title"], e["kind"]) for e in timeline] == [
        ("Exercise started", "lifecycle"),
        ("Exercise completed", "lifecycle"),
    ]
    assert timeline[0]["at"] == T0.isoformat() and timeline[1]["at"] == (T0 + timedelta(hours=2)).isoformat()


def test_audit_rows_are_shown_and_no_stamp_falls_back_behind_them(db_session):
    w = _world(db_session)
    _audit(w["db"], w["ex"], "start", T0)
    _audit(w["db"], w["ex"], "pause", T0 + timedelta(minutes=30))
    _audit(w["db"], w["ex"], "resume", T0 + timedelta(minutes=40))
    _audit(w["db"], w["ex"], "complete", T0 + timedelta(hours=2))
    # A complete stamp on the exercise as well: the audit row wins, no duplicate.
    titles = [e["title"] for e in build_report(w["db"], w["ex"], w["scenario"], "tester")["timeline"]]
    assert titles == ["Exercise started", "Exercise paused", "Exercise resumed", "Exercise completed"]


def test_achieved_objectives_and_annotations_are_on_the_timeline_in_time_order(db_session):
    w = _world(db_session)
    db = w["db"]
    db.add(
        Objective(
            exercise_id=w["ex"].id,
            ref_id="detect_c2",
            objective_type=ObjectiveType.detection,
            validator="opensearch_query",
            points=10,
            achieved=True,
            achieved_at=T0 + timedelta(minutes=90),
            description="C2 beacon detected",
        )
    )
    ada = real_user(db, UserRole.student, w["ex"].tenant_id)
    db.add(
        AnalystAnnotation(
            exercise_id=w["ex"].id,
            user_id=uuid.UUID(ada.id),
            content="Blue is quiet",
            user_display_name="Ada",
            annotation_type="observation",
            created_at=T0 + timedelta(minutes=20),
        )
    )
    db.flush()
    events = build_report(db, w["ex"], w["scenario"], "tester")["timeline"]
    assert [(e["kind"], e["title"], e["detail"]) for e in events] == [
        ("lifecycle", "Exercise started", ""),
        ("annotation", "observation by Ada", "Blue is quiet"),
        ("detection", "Objective detect_c2 achieved", "C2 beacon detected"),
        ("lifecycle", "Exercise completed", ""),
    ]


# ── participants ───────────────────────────────────────────────────────
def test_participants_are_team_members_then_the_assessed_and_the_annotators(db_session):
    w = _world(db_session)
    db = w["db"]
    a = w["ex"].tenant_id
    ada = real_user(db, UserRole.student, a)
    sam = real_user(db, UserRole.instructor, a)
    team = Team(id=uuid.uuid4(), name="Blue", tenant_id=a, exercise_id=w["ex"].id)
    db.add(team)
    db.flush()
    db.add(TeamMembership(id=uuid.uuid4(), team_id=team.id, user_id=uuid.UUID(ada.id), role="leader"))
    db.add(CompetencyAutoAssessment(user_id=uuid.UUID(sam.id), exercise_id=w["ex"].id))
    db.flush()
    people = build_report(db, w["ex"], w["scenario"], "tester")["participants"]
    assert people == [  # sorted by team then name: the unteamed first
        {"name": sam.display_name, "team": "", "role": "instructor"},  # assessed, no team
        {"name": ada.display_name, "team": "Blue", "role": "leader"},
    ]


def test_a_team_member_of_another_tenant_is_not_a_participant(db_session):
    w = _world(db_session)
    db = w["db"]
    outsider_tenant = real_tenant(db, "aar-report-other")
    outsider = real_user(db, UserRole.student, outsider_tenant.id)
    team = Team(id=uuid.uuid4(), name="Red", tenant_id=w["ex"].tenant_id, exercise_id=w["ex"].id)
    db.add(team)
    db.flush()
    db.add(TeamMembership(id=uuid.uuid4(), team_id=team.id, user_id=uuid.UUID(outsider.id), role="member"))
    db.flush()
    assert build_report(db, w["ex"], w["scenario"], "tester")["participants"] == []


def test_scores_and_summary_come_from_the_exercises_counters(db_session):
    w = _world(db_session, total_score=50, max_score=200)
    report = build_report(w["db"], w["ex"], w["scenario"], "tester")
    assert report["scores"] == {"total": 50, "max": 200, "pct": 25.0}
    assert report["summary"] == {"total_objectives": 0, "achieved": 0, "score_pct": 25.0}
    assert report["generated_by"] == "tester"
    assert report["range"]["name"] == "r" and report["scenario"]["name"] == "s"
