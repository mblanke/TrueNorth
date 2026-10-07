"""An exercise created from a scenario carries that scenario's objectives (PR #28).

Found in an end-to-end run (docs/hardening/s7-interruption-exercise-2026-10-04.md): a
scenario exercise ran and completed with 0 objectives and a 0/0 score, and its AAR was
empty, because Objective rows were only made for forged and curriculum-driven exercises.
The run, a Student's detection (ADR 0005) and the AAR all work from the rows, by
``ref_id``, so with none there was nothing to score. Both ways an exercise is made from a
scenario are covered: ``POST /exercises`` and a scheduler booking.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app import exercise_lifecycle, scenario_objectives
from app.models import Exercise, Objective, ObjectiveType, Range, Scenario, Template

MINE = uuid.UUID("00000000-0000-0000-0000-000000000001")  # the AUTH_DISABLED user's tenant
APT = (Path(__file__).resolve().parents[2] / "scenario-engine" / "examples" / "apt-breach.yaml").read_text()


def _range(client) -> str:
    tmpl = client.post(
        "/templates",
        json={"name": f"T{uuid.uuid4().hex[:6]}", "version": "1.0", "is_public": True, "yaml": "nodes: []\n"},
    ).json()
    return client.post("/ranges", json={"name": f"R{uuid.uuid4().hex[:6]}", "template_id": tmpl["id"]}).json()["id"]


def _exercise(client, scenario_yaml: str, **extra) -> dict:
    sc = client.post(
        "/scenarios",
        json={"name": f"S{uuid.uuid4().hex[:6]}", "version": "1.0", "yaml": scenario_yaml, "is_public": True},
    )
    assert sc.status_code == 201, sc.text
    ex = client.post(
        "/exercises", json={"name": "E", "range_id": _range(client), "scenario_id": sc.json()["id"], **extra}
    )
    assert ex.status_code == 201, ex.text
    return ex.json()


def test_the_scenarios_objectives_become_the_exercises(client):
    ex = _exercise(client, APT)
    objectives = {o["ref_id"]: o for o in client.get(f"/exercises/{ex['id']}/objectives").json()}
    assert {k: o["points"] for k, o in objectives.items()} == {
        "detect-phish": 20,
        "detect-c2": 30,
        "detect-priv-esc": 25,
        "incident-report": 25,
    }
    assert objectives["incident-report"]["objective_type"] == "deliverable"
    assert objectives["detect-c2"]["objective_type"] == "detection"
    assert not any(o["achieved"] for o in objectives.values())
    assert ex["max_score"] == 100, "the score is out of the objectives' points"


def test_validators_are_canonical_and_rows_carry_the_answer_key(client, db_session):
    ex = _exercise(client, APT)
    row = db_session.query(Objective).filter_by(exercise_id=uuid.UUID(ex["id"]), ref_id="detect-c2").one()
    assert row.validator == "opensearch_query"
    assert json.loads(row.validator_params) == {"query": "event_type:http AND url:*c2.evil*", "min_hits": 1}
    report = db_session.query(Objective).filter_by(exercise_id=uuid.UUID(ex["id"]), ref_id="incident-report").one()
    assert report.validator == "deliverable_check"


def test_a_scenario_without_objectives_keeps_the_requested_max_score(client):
    ex = _exercise(client, "name: plain\ntimeline: []\n", max_score=40)
    assert client.get(f"/exercises/{ex['id']}/objectives").json() == []
    assert ex["max_score"] == 40


def test_malformed_objectives_are_tolerated_not_fatal(client):
    yaml_text = (
        "objectives:\n"
        "  - id: good\n    type: response\n    validator: validate.manual_ack\n    points: 10\n"
        "  - type: detection\n    points: 5\n"  # no id: gets one
        "  - id: weird-type\n    type: telepathy\n    points: lots\n"
        "  - id: good\n    type: detection\n    points: 99\n"  # repeated id: skipped
        "  - not-a-mapping\n"
    )
    ex = _exercise(client, yaml_text)
    got = {o["ref_id"]: (o["objective_type"], o["points"]) for o in client.get(f"/exercises/{ex['id']}/objectives").json()}
    assert got == {"good": ("response", 10), "obj-2": ("detection", 5), "weird-type": ("deliverable", 0)}
    assert ex["max_score"] == 15


def test_unreadable_yaml_makes_no_rows():
    assert scenario_objectives.parse("objectives: [unclosed") == []
    assert scenario_objectives.parse(None) == []
    assert scenario_objectives.parse("- just\n- a list\n") == []


def test_a_booked_exercise_carries_the_objectives_too(db_session):
    t = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=MINE)
    s = Scenario(id=uuid.uuid4(), name="s", yaml=APT, tenant_id=MINE)
    db_session.add_all([t, s])
    db_session.flush()
    r = Range(id=uuid.uuid4(), name="r", template_id=t.id, tenant_id=MINE)
    db_session.add(r)
    db_session.flush()
    ex = exercise_lifecycle.create_for_booking(db_session, tenant_id=MINE, range_id=r.id, scenario_id=s.id, name="b")
    db_session.commit()
    rows = db_session.query(Objective).filter_by(exercise_id=ex.id).all()
    assert {o.ref_id for o in rows} == {"detect-phish", "detect-c2", "detect-priv-esc", "incident-report"}
    assert {o.objective_type for o in rows} == {ObjectiveType.detection, ObjectiveType.deliverable}
    assert db_session.get(Exercise, ex.id).max_score == 100
