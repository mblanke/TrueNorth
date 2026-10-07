"""Integration test: scenario execution.

create range -> provision -> load scenario -> execute -> verify timeline + objectives.

The execution API (`POST /scenarios/execute`, `GET /scenarios/executions/{id}/results`
and `/timeline`, app/routers/scenario_executions.py) runs a scenario's timeline against a
range without an exercise. Until it existed these four tests were strict `xfail`s naming
the missing endpoints; they now assert what the run did. On the integration stack's mock
backend `simulated_execution` fires and `dns_spike` (needs range hosts) is skipped.
Objectives are reported `unassessed`: an execution scores nothing.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 120

SCENARIO_YAML = """\
name: integ-scenario-execution
timeline:
  - t: "00:00"
    action: simulated_execution
    params: {technique: T1003, target: dc-01}
  - t: "00:01"
    action: inject.dns_spike
    params: {domains: [evil.test], count: 3}
objectives:
  - id: obj-detect
    type: detection
    validator: validate.opensearch_query
    points: 10
    description: Detect the credential dump
"""


def _poll_range_state(client, range_id: str, target: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last = None
    while time.time() < deadline:
        resp = client.get(f"/ranges/{range_id}")
        assert resp.status_code == 200
        last = resp.json()
        if last["state"] == target:
            return last
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"Range {range_id} did not reach '{target}' within {POLL_TIMEOUT}s "
        f"(last state: {last['state'] if last else 'unknown'})"
    )


@pytest.fixture(scope="class")
def execution_env(request, api_client, range_template):
    r = api_client.post(
        "/ranges", json={"name": "integ-scenario-range", "template_id": range_template}
    )
    assert r.status_code in (200, 201), f"POST /ranges => {r.status_code} {r.text}"
    range_id = r.json()["id"]
    yield {"range_id": range_id}
    api_client.delete(f"/ranges/{range_id}")


class TestScenarioExecution:
    """Scenario setup works end to end; execution has no API to drive it."""

    def test_provision_range(self, api_client, execution_env):
        range_id = execution_env["range_id"]
        resp = api_client.post(f"/ranges/{range_id}/provision")
        assert resp.status_code in (200, 202), resp.text
        _poll_range_state(api_client, range_id, "ready")

    def test_load_scenario(self, api_client):
        resp = api_client.post(
            "/scenarios",
            json={"name": "integ-scenario-execution", "yaml": SCENARIO_YAML, "is_public": False},
        )
        assert resp.status_code in (200, 201), resp.text
        self.__class__._scenario_id = resp.json()["id"]

    def test_scenario_is_readable(self, api_client):
        resp = api_client.get(f"/scenarios/{self.__class__._scenario_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "integ-scenario-execution"

    def test_execute_scenario(self, api_client, execution_env):
        resp = api_client.post(
            "/scenarios/execute",
            json={
                "scenario_id": self.__class__._scenario_id,
                "range_id": execution_env["range_id"],
            },
        )
        assert resp.status_code in (200, 201, 202), resp.text
        self.__class__._execution_id = resp.json()["id"]

    def test_wait_for_completion(self, api_client):
        eid = self.__class__._execution_id
        deadline = time.time() + POLL_TIMEOUT
        state = None
        while time.time() < deadline:
            resp = api_client.get(f"/scenarios/executions/{eid}/results")
            assert resp.status_code == 200, resp.text
            state = resp.json()["state"]
            if state in ("completed", "failed"):
                break
            time.sleep(POLL_INTERVAL)
        assert state == "completed", f"execution {eid} ended {state!r}"

    def test_verify_timeline_events(self, api_client):
        eid = self.__class__._execution_id
        resp = api_client.get(f"/scenarios/executions/{eid}/timeline")
        assert resp.status_code == 200
        timeline = resp.json()
        assert isinstance(timeline, list)
        assert [(e["seq"], e["status"]) for e in timeline] == [(0, "fired"), (1, "skipped")], timeline
        assert timeline[0]["execution_mode"] == "simulated" and timeline[0]["telemetry_count"] >= 1

    def test_evaluate_objectives(self, api_client):
        eid = self.__class__._execution_id
        resp = api_client.get(f"/scenarios/executions/{eid}/results")
        assert resp.status_code == 200
        body = resp.json()
        assert "objectives" in body
        # Nothing is scored without Students and evidence: absence of evidence is not a pass.
        assert body["objectives"] == [
            {"ref_id": "obj-detect", "description": "Detect the credential dump", "status": "unassessed"}
        ]
        assert body["injects"] == {"total": 2, "fired": 1, "skipped": 1, "failed": 0, "pending": 0}

    def test_cleanup_scenario(self, api_client):
        """An execution does not pin its scenario (unlike an exercise, which answers 409)."""
        resp = api_client.delete(f"/scenarios/{self.__class__._scenario_id}")
        assert resp.status_code in (200, 202, 204)
