"""Integration test: scenario execution.

create range -> provision -> load scenario -> execute -> verify timeline + objectives.

**Half of this file tests an API that does not exist.** `/scenarios` is CRUD only —
create, read, update, delete. There is no `POST /scenarios/execute`, no
`GET /scenarios/executions/{id}/results` and no `GET /scenarios/executions/{id}/timeline`,
and there never has been: the file was written against a planned contract and then hidden
behind an env-gated skip, so nothing ever reported the gap.

Rather than delete those tests or leave them silently skipped, they are marked `xfail`
with the missing endpoint named. That keeps the gap visible in every run and makes the
tests turn green by themselves — as `XPASS`, which is a failure under `strict=True` — the
day the endpoints land. Note that execution *is* reachable today through the exercise
API (`POST /exercises/{id}/start`, see `test_exercise_lifecycle.py`); what is absent is a
scenario-level execution surface independent of an exercise.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 120

NO_EXECUTION_API = pytest.mark.xfail(
    strict=True,
    reason=(
        "no scenario-level execution API: POST /scenarios/execute, "
        "GET /scenarios/executions/{id}/results and /timeline do not exist. "
        "Scenarios run through POST /exercises/{id}/start instead."
    ),
)


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
            json={
                "name": "integ-scenario-execution",
                "yaml": "name: integ-scenario-execution\nobjectives: []\n",
                "is_public": False,
            },
        )
        assert resp.status_code in (200, 201), resp.text
        self.__class__._scenario_id = resp.json()["id"]

    def test_scenario_is_readable(self, api_client):
        resp = api_client.get(f"/scenarios/{self.__class__._scenario_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "integ-scenario-execution"

    @NO_EXECUTION_API
    def test_execute_scenario(self, api_client, execution_env):
        resp = api_client.post(
            "/scenarios/execute",
            json={
                "scenario_id": self.__class__._scenario_id,
                "range_id": execution_env["range_id"],
            },
        )
        assert resp.status_code in (200, 201, 202)
        self.__class__._execution_id = resp.json()["id"]

    @NO_EXECUTION_API
    def test_wait_for_completion(self, api_client):
        eid = self.__class__._execution_id
        resp = api_client.get(f"/scenarios/executions/{eid}/results")
        assert resp.status_code == 200

    @NO_EXECUTION_API
    def test_verify_timeline_events(self, api_client):
        eid = self.__class__._execution_id
        resp = api_client.get(f"/scenarios/executions/{eid}/timeline")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @NO_EXECUTION_API
    def test_evaluate_objectives(self, api_client):
        eid = self.__class__._execution_id
        resp = api_client.get(f"/scenarios/executions/{eid}/results")
        assert resp.status_code == 200
        assert "objectives" in resp.json()

    def test_cleanup_scenario(self, api_client):
        resp = api_client.delete(f"/scenarios/{self.__class__._scenario_id}")
        assert resp.status_code in (200, 202, 204)
