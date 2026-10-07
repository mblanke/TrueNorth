"""Integration test: Exercise Forge through the live AI orchestrator.

control-plane API -> ai-orchestrator (AI_MODEL_BACKEND=mock) -> scenario YAML ->
Scenario + Exercise + Objectives + ForgedExercise persisted and readable back.

The itest stack (infra/platform/docker/compose.itest.yml) runs the orchestrator
with the mock backend, which answers forge prompts with a fixed, well-formed
scenario (``mock-forged-phishing-intrusion``, two objectives worth 50 points each).
No model and no network egress are involved; what is under test is the wiring:
the API reaching AI_ORCHESTRATOR_URL, the orchestrator routing the task to a
backend, and the API parsing and persisting what comes back.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

MOCK_SCENARIO = "mock-forged-phishing-intrusion"


def test_forge_generate_persists_exercise_from_orchestrator(api_client, make_range):
    range_id = make_range("itest-ai-forge")

    resp = api_client.post(
        "/exercise-forge/generate",
        json={
            "indicators": [
                {
                    "indicator_type": "domain",
                    "value": "evil-c2.example.com",
                    "severity": "high",
                    "mitre_attack_ids": ["T1566.001"],
                }
            ],
            "difficulty": "intermediate",
            "duration_minutes": 60,
            "objective_count": 2,
            "range_id": range_id,
        },
        timeout=120.0,
    )
    assert resp.status_code == 200, f"POST /exercise-forge/generate => {resp.status_code} {resp.text}"
    forged = resp.json()
    assert forged["model_used"] == "mock", "itest orchestrator must run AI_MODEL_BACKEND=mock"
    assert forged["name"] == MOCK_SCENARIO
    assert forged["indicators_used"] == 1
    assert "T1566.001" in forged["mitre_techniques"]

    exercise = api_client.get(f"/exercises/{forged['exercise_id']}")
    assert exercise.status_code == 200, exercise.text
    assert exercise.json()["range_id"] == range_id
    assert exercise.json()["scenario_id"] == forged["scenario_id"]

    objectives = api_client.get(f"/exercises/{forged['exercise_id']}/objectives")
    assert objectives.status_code == 200, objectives.text
    refs = sorted(o["ref_id"] for o in objectives.json())
    assert refs == ["contain-host", "detect-phish"]
    assert sum(o["points"] for o in objectives.json()) == 100

    history = api_client.get("/exercise-forge/history", params={"limit": 200})
    assert history.status_code == 200, history.text
    assert forged["exercise_id"] in {item["exercise_id"] for item in history.json()["items"]}
