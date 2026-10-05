"""An exercise created from a scenario carries that scenario's objectives.

Found in an end-to-end run (docs/hardening/s7-interruption-exercise-2026-10-04.md): a
scenario exercise ran and completed with 0 objectives and a 0/0 score, and its AAR was
empty, because Objective rows were only ever created for curriculum-driven exercises
(app/qsp_paths.py). The worker scores the rows it finds by ``ref_id``, so with none it
scored nothing.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.models import Objective, ObjectiveType

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
    assert set(objectives) == {"detect-phish", "detect-c2", "detect-priv-esc", "incident-report"}
    assert {k: o["points"] for k, o in objectives.items()} == {
        "detect-phish": 20,
        "detect-c2": 30,
        "detect-priv-esc": 25,
        "incident-report": 25,
    }
    assert objectives["incident-report"]["objective_type"] == "deliverable"
    assert objectives["detect-c2"]["objective_type"] == "detection"
    assert ex["max_score"] == 100, "the score is out of the objectives' points"


def test_validators_use_the_platforms_names_and_keep_their_parameters(client, db_session):
    ex = _exercise(client, APT)
    row = db_session.query(Objective).filter_by(exercise_id=uuid.UUID(ex["id"]), ref_id="detect-c2").one()
    assert row.validator == "validate.opensearch_query"
    assert '"min_hits": 1' in row.validator_params and "c2.evil" in row.validator_params
    assert row.objective_type == ObjectiveType.detection and row.achieved is False


def test_a_scenario_without_objectives_keeps_the_requested_max_score(client):
    ex = _exercise(client, "name: plain\ntimeline: []\n", max_score=40)
    assert client.get(f"/exercises/{ex['id']}/objectives").json() == []
    assert ex["max_score"] == 40


def test_malformed_objectives_are_skipped_not_fatal(client):
    yaml_text = (
        "objectives:\n"
        "  - id: good\n    type: response\n    validator: manual\n    points: 10\n"
        "  - type: detection\n    points: 5\n"  # no id
        "  - id: weird-type\n    type: telepathy\n    validator: manual\n    points: 7\n"
        "  - not-a-mapping\n"
    )
    ex = _exercise(client, yaml_text)
    got = {o["ref_id"]: o["objective_type"] for o in client.get(f"/exercises/{ex['id']}/objectives").json()}
    assert got == {"good": "response", "weird-type": "detection"}
    assert ex["max_score"] == 17
