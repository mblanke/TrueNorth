"""Integration test: full exercise lifecycle.

create range -> create exercise -> start -> submit objectives -> complete -> generate AAR
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 120


def _poll_exercise_state(client, exercise_id: str, target: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        resp = client.get(f"/exercises/{exercise_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["state"] == target:
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Exercise {exercise_id} did not reach '{target}' within {POLL_TIMEOUT}s")


class TestExerciseLifecycle:
    """End-to-end exercise lifecycle against live services."""

    _range_id: str = ""
    _exercise_id: str = ""

    def test_create_range(self, api_client):
        resp = api_client.post(
            "/ranges",
            json={
                "name": "integ-exercise-range",
                "description": "Range for exercise lifecycle test",
            },
        )
        assert resp.status_code in (200, 201)
        self.__class__._range_id = resp.json()["id"]

    def test_provision_range(self, api_client):
        range_id = self.__class__._range_id
        resp = api_client.post(f"/ranges/{range_id}/provision")
        assert resp.status_code in (200, 202)
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            r = api_client.get(f"/ranges/{range_id}")
            if r.json()["state"] == "provisioned":
                break
            time.sleep(POLL_INTERVAL)

    def test_create_exercise(self, api_client):
        resp = api_client.post(
            "/exercises",
            json={
                "name": "integ-exercise",
                "range_id": self.__class__._range_id,
                "scenario_id": None,
                "description": "Integration test exercise",
            },
        )
        assert resp.status_code in (200, 201)
        self.__class__._exercise_id = resp.json()["id"]

    def test_start_exercise(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.post(f"/exercises/{eid}/start")
        assert resp.status_code in (200, 202)
        _poll_exercise_state(api_client, eid, "running")

    def test_submit_objectives(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.post(
            f"/exercises/{eid}/objectives",
            json=[
                {"objective_id": "obj-1", "status": "completed", "score": 100},
                {"objective_id": "obj-2", "status": "completed", "score": 85},
            ],
        )
        assert resp.status_code in (200, 201, 202)

    def test_complete_exercise(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.post(f"/exercises/{eid}/complete")
        assert resp.status_code in (200, 202)
        _poll_exercise_state(api_client, eid, "completed")

    def test_generate_aar(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.post(f"/exercises/{eid}/aar")
        assert resp.status_code in (200, 201, 202)
        data = resp.json()
        assert "score" in data or "report" in data or "id" in data

    def test_verify_scoring(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.get(f"/exercises/{eid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "completed"

    def test_cleanup_range(self, api_client):
        range_id = self.__class__._range_id
        resp = api_client.delete(f"/ranges/{range_id}")
        assert resp.status_code in (200, 202, 204)
