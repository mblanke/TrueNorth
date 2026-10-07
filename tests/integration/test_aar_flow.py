"""Integration test: an exercise run end to end, then its after-action report.

template -> range -> scenario -> exercise -> run (mock provisioner + mock scenario runner)
-> completed -> POST /aar/generate -> GET /aar/html and /aar/pdf.

The HTML must name the exercise and carry its objectives and the scenario's injects; the
PDF must be a real PDF, not an error body served as one.
"""

from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 180

SCENARIO_YAML = """\
name: {name}
environment: enterprise
timeline:
  - t: "0:00"
    action: email_phish
    description: Phishing email to the finance analyst
  - t: "0:05"
    action: http_burst
    description: Beacon to the C2 server
"""


def _poll(fn, describe: str):
    """Call ``fn`` until it returns something truthy, or fail after POLL_TIMEOUT."""
    deadline = time.time() + POLL_TIMEOUT
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(POLL_INTERVAL)
    pytest.fail(f"timed out after {POLL_TIMEOUT}s waiting for {describe} (last: {last!r})")


@pytest.fixture(scope="module")
def completed_exercise(api_client, range_template):
    suffix = uuid.uuid4().hex[:8]
    created: dict[str, str] = {}

    r = api_client.post("/ranges", json={"name": f"integ-aar-range-{suffix}", "template_id": range_template})
    assert r.status_code in (200, 201), f"POST /ranges => {r.status_code} {r.text}"
    created["range"] = r.json()["id"]

    scenario_name = f"integ-aar-scenario-{suffix}"
    s = api_client.post(
        "/scenarios",
        json={"name": scenario_name, "yaml": SCENARIO_YAML.format(name=scenario_name), "is_public": False},
    )
    assert s.status_code in (200, 201), f"POST /scenarios => {s.status_code} {s.text}"
    created["scenario"] = s.json()["id"]

    name = f"integ-aar-exercise-{suffix}"
    e = api_client.post(
        "/exercises",
        json={"name": name, "range_id": created["range"], "scenario_id": created["scenario"], "max_score": 100},
    )
    assert e.status_code in (200, 201), f"POST /exercises => {e.status_code} {e.text}"
    eid = e.json()["id"]

    # One click: provisions the range (mock) and starts the scenario runner.
    run = api_client.post(f"/exercises/{eid}/run")
    assert run.status_code in (200, 202), f"POST /exercises/{eid}/run => {run.status_code} {run.text}"

    def _settled():
        state = api_client.get(f"/exercises/{eid}").json()["state"]
        if state in ("failed", "cancelled"):
            pytest.fail(f"exercise entered {state!r}")
        return state if state in ("running", "paused", "completed") else None

    # The mock runner may finish on its own; if it is still going, the instructor ends it.
    # It can also finish between the poll and the call: a 409 "is completed" is that race.
    if _poll(_settled, "the exercise to start") in ("running", "paused"):
        done = api_client.post(f"/exercises/{eid}/complete")
        already_done = done.status_code == 409 and "is completed" in done.text
        assert done.status_code in (200, 202) or already_done, f"complete => {done.status_code} {done.text}"
    _poll(lambda: api_client.get(f"/exercises/{eid}").json()["state"] == "completed", "state 'completed'")

    yield {"id": eid, "name": name}

    api_client.delete(f"/scenarios/{created['scenario']}")
    api_client.delete(f"/ranges/{created['range']}")


class TestAarFlow:
    def test_generate(self, api_client, completed_exercise):
        eid = completed_exercise["id"]
        resp = api_client.post(f"/exercises/{eid}/aar/generate")
        assert resp.status_code == 201, resp.text
        assert resp.json()["exercise_id"] == eid

    def test_html_report(self, api_client, completed_exercise):
        eid = completed_exercise["id"]

        def _html():
            resp = api_client.get(f"/exercises/{eid}/aar/html")
            return resp if resp.status_code == 200 else None

        page = _poll(_html, "GET /aar/html => 200").text
        assert completed_exercise["name"] in page
        for section in ("summary", "objectives", "detections", "timeline", "participants"):
            assert f'id="{section}"' in page, section
        # The scenario's planned injects, read from its YAML.
        assert "email_phish" in page and "Beacon to the C2 server" in page
        assert "Exercise completed" in page
        # Every objective the exercise has is listed, with its result.
        objectives = api_client.get(f"/exercises/{eid}/objectives").json()
        for obj in objectives:
            assert obj["ref_id"] in page, obj["ref_id"]
        if objectives:
            assert "Achieved" in page or "Not achieved" in page
        else:
            assert "No objectives were recorded" in page

    def test_pdf_report(self, api_client, completed_exercise):
        eid = completed_exercise["id"]
        resp = api_client.get(f"/exercises/{eid}/aar/pdf")
        assert resp.status_code == 200, resp.text[:300]
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")
        assert len(resp.content) > 1000

    def test_missing_aar_is_404_not_500(self, api_client):
        for suffix in ("", "/html", "/pdf"):
            resp = api_client.get(f"/exercises/{uuid.uuid4()}/aar{suffix}")
            assert resp.status_code == 404, f"{suffix}: {resp.status_code}"
