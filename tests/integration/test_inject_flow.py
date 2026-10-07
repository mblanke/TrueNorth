"""Integration test: an exercise's timeline fires its injects (mock backend).

create range -> provision -> scenario with a timeline -> exercise -> start -> completed
-> GET /exercises/{id}/injects shows what each inject did -> its telemetry is searchable.

On the mock backend (the integration stack's) the range has no hosts, so the dispatch
runs only injectors that do not touch range hosts: ``simulated_execution`` fires and ships
telemetry; ``dns_spike`` is recorded ``skipped``; an unknown action is recorded ``failed``
(docs/scenario-inject-execution.md). Every one of the three is checked, so an exercise
that "completes" without firing anything fails here.

Needs the worker image to carry the scenario engine (Dockerfile build context, as
detection scoring does); without it every inject is recorded failed and this test says so.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 120
SEARCH_TIMEOUT = 60

SCENARIO_YAML = """\
name: integ-inject-flow
timeline:
  - t: "00:00"
    action: inject.simulated_execution
    params: {technique: T1059.001, process_name: powershell.exe, target: ws-01}
  - t: "00:01"
    action: dns_spike
    params: {domains: [evil.test], count: 5}
  - t: "00:02"
    action: no_such_injector
objectives: []
"""


def _poll(client, url: str, target: str, timeout: int = POLL_TIMEOUT) -> dict:
    deadline = time.time() + timeout
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
    pytest.fail(f"{url} did not reach '{target}' within {timeout}s (last: {last and last['state']})")


@pytest.fixture(scope="class")
def inject_env(api_client, range_template):
    r = api_client.post("/ranges", json={"name": "integ-inject-range", "template_id": range_template})
    assert r.status_code in (200, 201), r.text
    range_id = r.json()["id"]
    s = api_client.post("/scenarios", json={"name": "integ-inject-flow", "yaml": SCENARIO_YAML, "is_public": False})
    assert s.status_code in (200, 201), s.text
    env = {"range_id": range_id, "scenario_id": s.json()["id"]}
    yield env
    # The exercise keeps the scenario (DELETE /scenarios/{id} answers 409; there is no
    # DELETE /exercises); the range goes.
    api_client.delete(f"/ranges/{range_id}")


class TestInjectFlow:
    def test_provision_range(self, api_client, inject_env):
        resp = api_client.post(f"/ranges/{inject_env['range_id']}/provision")
        assert resp.status_code in (200, 202), resp.text
        _poll(api_client, f"/ranges/{inject_env['range_id']}", "ready")

    def test_run_exercise_to_completion(self, api_client, inject_env):
        resp = api_client.post(
            "/exercises",
            json={
                "name": "integ-inject-exercise",
                "range_id": inject_env["range_id"],
                "scenario_id": inject_env["scenario_id"],
                "max_score": 100,
            },
        )
        assert resp.status_code in (200, 201), resp.text
        inject_env["exercise_id"] = eid = resp.json()["id"]
        assert api_client.post(f"/exercises/{eid}/start").status_code in (200, 202)
        _poll(api_client, f"/exercises/{eid}", "completed")

    def test_each_inject_outcome_is_recorded(self, api_client, inject_env):
        resp = api_client.get(f"/exercises/{inject_env['exercise_id']}/injects")
        assert resp.status_code == 200, resp.text
        by_seq = {r["seq"]: r for r in resp.json() if r["source"] == "timeline"}
        assert set(by_seq) == {0, 1, 2}, resp.json()

        fired = by_seq[0]
        assert fired["status"] == "fired", fired  # "failed: scenario-engine not importable" = engine not in the worker image
        assert fired["execution_mode"] == "simulated" and fired["mitre_technique"] == "T1059.001"
        assert fired["telemetry_count"] >= 1 and fired["telemetry_shipped"] is True

        assert by_seq[1]["status"] == "skipped"
        assert by_seq[1]["detail"] == "skipped: mock backend (injector needs range hosts)"

        assert by_seq[2]["status"] == "failed"
        assert "No injector registered" in by_seq[2]["detail"]

    def test_fired_inject_telemetry_is_searchable(self, api_client, inject_env):
        """The telemetry went through ingest_telemetry_batch into the range's index."""
        eid, rid = inject_env["exercise_id"], inject_env["range_id"]
        query = f'exercise_id:"{eid}" AND inject_action:simulated_execution'
        deadline = time.time() + SEARCH_TIMEOUT
        docs: list = []
        while time.time() < deadline:
            resp = api_client.get(f"/telemetry/{rid}/search", params={"q": query, "size": 10})
            if resp.status_code == 200:
                hits = resp.json()
                docs = hits.get("hits", hits) if isinstance(hits, dict) else hits
                if isinstance(docs, dict):
                    docs = docs.get("hits", [])
                if docs:
                    break
            time.sleep(POLL_INTERVAL)
        assert docs, f"no telemetry for {query!r} on range {rid} within {SEARCH_TIMEOUT}s"
        assert not [d for d in docs if "dns_spike" in str(d)]  # a skipped inject ships nothing
