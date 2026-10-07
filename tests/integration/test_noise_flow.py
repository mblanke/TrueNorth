"""Integration test: background noise on a mock-provisioned range, through the real API.

Template with a ``noise:`` block -> provision (mock backend) -> the agent node holds a
reserved management address -> deploy noise (the plan: profile, agents, personas, and
the worker's deploy_noise_agents task) -> start at a known level -> agents, personas
and the schedule are visible -> an agent polls with its own token and is seen ->
stop -> the schedule is empty -> destroy frees the address.

Needs the stack (API + worker + broker, PROVISIONER_BACKEND=mock) and a caller that
holds noise:read / noise:control (the dev stack's admin). Skipped when no API answers
(tests/integration/conftest.py); an error under INTEGRATION_REQUIRE_API=1.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from datetime import UTC, datetime

import pytest
import yaml

pytestmark = pytest.mark.integration

POLL_INTERVAL = 2
POLL_TIMEOUT = 90

AGENT = "lnx01"
TEMPLATE = {
    "name": "noise-integration",
    "nodes": [
        {"id": "web01", "role": "web_server", "os": "ubuntu-2404", "vlan": "corporate_lan", "ip": "10.10.0.20"},
        {"id": AGENT, "role": "workstation", "os": "ubuntu-2404", "vlan": "corporate_lan", "ip": "10.10.0.50"},
    ],
    "noise": {
        "enabled": True,
        "preset": "office",
        "personas": 6,
        "mgmt": {"controller_url": "https://10.255.0.1/api"},
    },
}
# Every pool the planner draws from, so the schedule cannot be empty for want of targets.
TARGETS = {
    "web": ["http://10.10.0.20/"],
    "dns": ["web01.corp.local"],
    "mail": ["mail.corp.local"],
    "share": ["files.corp.local"],
    "dc": ["dc01.corp.local"],
    "ssh": ["10.10.0.20"],
    "ntp": ["dc01.corp.local"],
    "subnet": ["10.10.0.0/24"],
    "hosts": ["10.10.0.50"],
}


def _working_hours_offset() -> int:
    """A UTC offset (-12..14) that puts the range's local clock at 10:00 now, so the
    personas are at work whenever the test runs."""
    off = (10 - datetime.now(UTC).hour) % 24
    return off - 24 if off > 14 else off


def _poll_state(client, range_id: str, target: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        resp = client.get(f"/ranges/{range_id}")
        assert resp.status_code == 200, f"GET /ranges/{range_id} => {resp.status_code} {resp.text}"
        last = resp.json()
        if last["state"] == target:
            return last
        if last["state"] == "failed":
            pytest.fail(f"range {range_id} failed while waiting for {target!r}: {last.get('error_message')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"range {range_id} not {target!r} within {POLL_TIMEOUT}s (last: {last.get('state')})")


@pytest.fixture
def noisy_range(api_client):
    """A range from a noise-enabled template; destroyed (and so released) afterwards."""
    tmpl = api_client.post(
        "/templates",
        json={
            "name": f"noise-integration-{uuid.uuid4().hex[:8]}",
            "version": "1.0",
            "yaml": yaml.safe_dump(TEMPLATE),
            "is_public": False,
        },
    )
    assert tmpl.status_code in (200, 201), f"POST /templates => {tmpl.status_code} {tmpl.text}"
    template_id = tmpl.json()["id"]
    rng = api_client.post("/ranges", json={"name": f"noise-it-{uuid.uuid4().hex[:6]}", "template_id": template_id})
    assert rng.status_code in (200, 201), f"POST /ranges => {rng.status_code} {rng.text}"
    rid = rng.json()["id"]
    yield rid
    state = api_client.get(f"/ranges/{rid}").json().get("state")
    if state not in ("destroyed", "created"):
        api_client.post(f"/ranges/{rid}/destroy")
        with contextlib.suppress(TimeoutError, pytest.fail.Exception):
            _poll_state(api_client, rid, "destroyed")
    api_client.delete(f"/ranges/{rid}")
    api_client.delete(f"/templates/{template_id}")


def test_noise_plan_start_visible_stop(api_client, noisy_range):
    rid = noisy_range

    # Provision: accepting it reserves the agent's management address.
    resp = api_client.post(f"/ranges/{rid}/provision")
    assert resp.status_code == 202, f"provision => {resp.status_code} {resp.text}"
    reservations = api_client.get(f"/ranges/{rid}/network-reservations").json()
    held = {r["holder"]: r["value"] for r in reservations if r["kind"] == "noise_mgmt_ip"}
    assert set(held) == {AGENT}, reservations
    _poll_state(api_client, rid, "ready")

    # Before anything: not configured.
    before = api_client.get(f"/noise/ranges/{rid}")
    assert before.status_code == 200, before.text
    assert before.json()["configured"] is False and before.json()["enabled"] is False

    # The plan, previewed: the agent at its reserved address; nothing changes.
    dry = api_client.post(f"/noise/ranges/{rid}/deploy", json={"dry_run": True})
    assert dry.status_code == 200, dry.text
    assert [(a["node"], a["mgmt_ip"]) for a in dry.json()["agents"]] == [(AGENT, held[AGENT])]
    assert "task_id" not in dry.json()
    assert api_client.get(f"/noise/ranges/{rid}/agents").json() == []

    # Deploy: profile enabled at the template's preset, agent registered, roster made,
    # deploy_noise_agents handed to the worker.
    deployed = api_client.post(f"/noise/ranges/{rid}/deploy", json={})
    assert deployed.status_code == 200, deployed.text
    assert deployed.json()["task_id"]

    # Start at a known level, in working hours, with every target pool filled.
    started = api_client.put(
        f"/noise/ranges/{rid}",
        json={
            "enabled": True,
            "paused": False,
            "level": 100,
            "utc_offset": _working_hours_offset(),
            "targets": TARGETS,
        },
    )
    assert started.status_code == 200, started.text
    profile = started.json()
    assert profile["enabled"] and profile["effective_level"] == 100

    # Agents and the schedule are visible.
    agents = api_client.get(f"/noise/ranges/{rid}/agents").json()
    assert [(a["node"], a["state"]) for a in agents] == [(AGENT, "pending")]
    personas = api_client.get(f"/noise/ranges/{rid}/personas").json()
    assert len(personas) == 6 and {p["node"] for p in personas} == {AGENT}
    plan = api_client.get(f"/noise/ranges/{rid}/plan", params={"node": AGENT, "minutes": 60})
    assert plan.status_code == 200, plan.text
    assert plan.json()["level"] == 100
    actions = plan.json()["actions"]
    assert actions, "a full-level hour in working hours with every target pool has actions"
    assert {a["persona"] for a in actions} <= {p["handle"] for p in personas}

    # The agent's own channel: a fresh token, a poll (the heartbeat), and it is seen.
    issued = api_client.post(f"/noise/ranges/{rid}/agents", json={"agents": [{"node": AGENT}]})
    assert issued.status_code == 201, issued.text
    token = issued.json()[0]["token"]
    polled = api_client.get("/noise/agent/plan", params={"minutes": 30}, headers={"X-Noise-Agent-Token": token})
    assert polled.status_code == 200, polled.text
    assert polled.json()["node"] == AGENT and all("lookalike" not in a for a in polled.json()["actions"])
    assert [a["state"] for a in api_client.get(f"/noise/ranges/{rid}/agents").json()] == ["ok"]

    # Stop: the profile is off and the schedule is empty for the white cell and the agent.
    stopped = api_client.put(f"/noise/ranges/{rid}", json={"enabled": False})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["enabled"] is False and stopped.json()["effective_level"] == 0
    plan = api_client.get(f"/noise/ranges/{rid}/plan", params={"node": AGENT, "minutes": 60}).json()
    assert plan["level"] == 0 and plan["actions"] == []
    polled = api_client.get("/noise/agent/plan", headers={"X-Noise-Agent-Token": token}).json()
    assert polled["actions"] == []

    # Destroy frees the management address for other ranges.
    assert api_client.post(f"/ranges/{rid}/destroy").status_code == 202
    _poll_state(api_client, rid, "destroyed")
    api_client.get(f"/ranges/{rid}/operations")  # reconcile the destroy's outcome
    assert api_client.get(f"/ranges/{rid}/network-reservations").json() == []
