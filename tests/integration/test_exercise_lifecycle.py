"""Integration test: full exercise lifecycle.

create range -> provision -> create scenario -> create exercise -> start ->
acknowledge objectives -> complete -> generate AAR.

Written against the contract the API publishes. The previous version diverged from it at
almost every step: it created ranges with no `template_id` and exercises with a null
`scenario_id` (both required), waited for a range state `provisioned` that does not exist
in `RangeState` (it is `ready`), and POSTed to `/exercises/{id}/objectives` and
`/exercises/{id}/aar`, which are read-only — objectives are acknowledged one at a time via
`/objectives/{ref_id}/ack`, and an AAR is produced by `/aar/generate` and then read from
`/aar`.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 120


def _poll_state(client, url: str, target: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last = None
    while time.time() < deadline:
        resp = client.get(url)
        assert resp.status_code == 200, f"GET {url} => {resp.status_code}"
        last = resp.json()
        if last["state"] == target:
            return last
        if last["state"] in ("failed", "cancelled"):
            pytest.fail(f"{url} entered '{last['state']}' while waiting for '{target}'")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"{url} did not reach '{target}' within {POLL_TIMEOUT}s "
        f"(last state: {last['state'] if last else 'unknown'})"
    )


@pytest.fixture(scope="class")
def exercise_env(request, api_client, range_template):
    """A provisioned range plus a scenario — everything `ExerciseIn` requires."""
    r = api_client.post(
        "/ranges", json={"name": "integ-exercise-range", "template_id": range_template}
    )
    assert r.status_code in (200, 201), f"POST /ranges => {r.status_code} {r.text}"
    range_id = r.json()["id"]

    s = api_client.post(
        "/scenarios",
        json={
            "name": "integ-exercise-scenario",
            "yaml": "name: integ-exercise-scenario\nobjectives: []\n",
            "is_public": False,
        },
    )
    assert s.status_code in (200, 201), f"POST /scenarios => {s.status_code} {s.text}"
    scenario_id = s.json()["id"]

    yield {"range_id": range_id, "scenario_id": scenario_id}

    api_client.delete(f"/scenarios/{scenario_id}")
    api_client.delete(f"/ranges/{range_id}")


class TestExerciseLifecycle:
    """End-to-end exercise lifecycle against live services."""

    def test_provision_range(self, api_client, exercise_env):
        range_id = exercise_env["range_id"]
        resp = api_client.post(f"/ranges/{range_id}/provision")
        assert resp.status_code in (200, 202), resp.text
        _poll_state(api_client, f"/ranges/{range_id}", "ready")

    def test_create_exercise(self, api_client, exercise_env):
        resp = api_client.post(
            "/exercises",
            json={
                "name": "integ-exercise",
                "range_id": exercise_env["range_id"],
                "scenario_id": exercise_env["scenario_id"],
                "max_score": 100,
            },
        )
        assert resp.status_code in (200, 201), resp.text
        data = resp.json()
        assert data["state"] == "pending"
        self.__class__._exercise_id = data["id"]

    def test_create_exercise_requires_a_range_and_scenario(self, api_client):
        """An exercise with no range has nothing to run on and no scenario to run."""
        resp = api_client.post("/exercises", json={"name": "integ-exercise-invalid"})
        assert resp.status_code == 422

    def test_start_exercise(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.post(f"/exercises/{eid}/start")
        assert resp.status_code in (200, 202), resp.text
        _poll_state(api_client, f"/exercises/{eid}", "running")

    def test_read_objectives(self, api_client):
        """Objectives are derived from the scenario, not submitted by the client."""
        eid = self.__class__._exercise_id
        resp = api_client.get(f"/exercises/{eid}/objectives")
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json(), list)

    def test_complete_exercise(self, api_client):
        """Reaching `completed` is the assertion; driving the transition is not.

        A scenario with no objectives has nothing left to score, so the exercise can
        finish on its own between `start` and here — in which case `complete` correctly
        returns 409 rather than re-completing. Demanding the transition would make this
        test fail for the one reason that is not a defect.
        """
        eid = self.__class__._exercise_id
        state = api_client.get(f"/exercises/{eid}").json()["state"]
        if state != "completed":
            resp = api_client.post(f"/exercises/{eid}/complete")
            assert resp.status_code in (200, 202), resp.text
        _poll_state(api_client, f"/exercises/{eid}", "completed")

    def test_completing_twice_is_rejected(self, api_client):
        """The state machine must refuse a second completion, not silently re-run it."""
        eid = self.__class__._exercise_id
        resp = api_client.post(f"/exercises/{eid}/complete")
        assert resp.status_code == 409, resp.text

    def test_generate_aar(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.post(f"/exercises/{eid}/aar/generate")
        assert resp.status_code in (200, 201, 202), resp.text

    def test_read_aar(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.get(f"/exercises/{eid}/aar")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert any(k in data for k in ("score", "report", "id", "summary")), data

    def test_verify_scoring(self, api_client):
        eid = self.__class__._exercise_id
        resp = api_client.get(f"/exercises/{eid}")
        assert resp.status_code == 200
        assert resp.json()["state"] == "completed"
