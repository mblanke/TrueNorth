"""Integration test: scenario execution flow.

Load ransomware-lite scenario -> execute on range -> verify timeline events -> evaluate objectives.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 3
POLL_TIMEOUT = 180


def _poll_execution_state(client, execution_id: str, target: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        resp = client.get(f"/scenarios/executions/{execution_id}")
        if resp.status_code == 200:
            data = resp.json()
            if data.get("state") == target:
                return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Execution {execution_id} did not reach '{target}' within {POLL_TIMEOUT}s")


class TestScenarioExecution:
    """End-to-end scenario execution against live services."""

    _range_id: str = ""
    _scenario_id: str = ""
    _execution_id: str = ""

    def test_create_range(self, api_client):
        resp = api_client.post(
            "/ranges",
            json={
                "name": "integ-scenario-range",
                "description": "Range for scenario execution test",
            },
        )
        assert resp.status_code in (200, 201)
        self.__class__._range_id = resp.json()["id"]

    def test_provision_range(self, api_client):
        range_id = self.__class__._range_id
        api_client.post(f"/ranges/{range_id}/provision")
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            r = api_client.get(f"/ranges/{range_id}")
            if r.json()["state"] == "provisioned":
                return
            time.sleep(POLL_INTERVAL)
        pytest.fail("Range did not provision in time")

    def test_load_scenario(self, api_client):
        """Load the ransomware-lite scenario (or find it if pre-loaded)."""
        # Try to find existing ransomware-lite
        resp = api_client.get("/scenarios", params={"name": "ransomware-lite"})
        if resp.status_code == 200:
            items = resp.json()
            if isinstance(items, list) and items:
                self.__class__._scenario_id = items[0]["id"]
                return
            if isinstance(items, dict) and items.get("items"):
                self.__class__._scenario_id = items["items"][0]["id"]
                return

        # Create a minimal scenario
        resp = api_client.post(
            "/scenarios",
            json={
                "name": "ransomware-lite",
                "description": "Lightweight ransomware simulation for testing",
                "objectives": [
                    {"id": "detect", "name": "Detect Ransomware", "points": 50},
                    {"id": "contain", "name": "Contain Spread", "points": 30},
                    {"id": "recover", "name": "Recover Systems", "points": 20},
                ],
            },
        )
        assert resp.status_code in (200, 201)
        self.__class__._scenario_id = resp.json()["id"]

    def test_execute_scenario(self, api_client):
        resp = api_client.post(
            "/scenarios/execute",
            json={
                "scenario_id": self.__class__._scenario_id,
                "range_id": self.__class__._range_id,
            },
        )
        assert resp.status_code in (200, 201, 202)
        self.__class__._execution_id = resp.json()["id"]

    def test_wait_for_completion(self, api_client):
        _poll_execution_state(api_client, self.__class__._execution_id, "completed")

    def test_verify_timeline_events(self, api_client):
        resp = api_client.get(f"/scenarios/executions/{self.__class__._execution_id}/timeline")
        assert resp.status_code == 200
        timeline = resp.json()
        events = timeline if isinstance(timeline, list) else timeline.get("events", [])
        assert len(events) > 0, "Expected at least one timeline event"

    def test_evaluate_objectives(self, api_client):
        resp = api_client.get(f"/scenarios/executions/{self.__class__._execution_id}/results")
        assert resp.status_code == 200
        results = resp.json()
        # Should have objective evaluations
        objectives = results if isinstance(results, list) else results.get("objectives", [])
        assert len(objectives) > 0, "Expected objective evaluations"

    def test_cleanup_range(self, api_client):
        range_id = self.__class__._range_id
        api_client.delete(f"/ranges/{range_id}")
