"""The noise API's published shapes (app/noise/schemas.py) are the ones it always sent.

The handlers returned plain dicts; declaring response models put the shapes into
docs/interfaces/openapi.json for the console's generated types. A response model also
filters: a field missing from it would silently vanish from the response. These tests
pin every key the console and the agents read, so that cannot happen unnoticed.
"""

from __future__ import annotations

import pytest
import yaml
from app.models import RangeState

from tests.api.test_noise import TARGETS, _hdr, _range, _setup


@pytest.fixture
def rng(db_session):
    return _range(db_session)


PROFILE_KEYS = {
    "range_id",
    "configured",
    "enabled",
    "paused",
    "level",
    "effective_level",
    "seed",
    "utc_offset",
    "overrides",
    "targets",
    "pack",
    "dial",
}
AGENT_KEYS = {"id", "node", "zone", "version", "state", "last_seen_at"}
PERSONA_KEYS = {
    "id",
    "handle",
    "display_name",
    "title",
    "department",
    "node",
    "work_start",
    "work_end",
    "habits",
    "lookalikes",
    "attrs",
}


def test_profile_shape_before_and_after_it_is_configured(client, rng):
    fresh = client.get(f"/noise/ranges/{rng.id}").json()
    assert set(fresh) == PROFILE_KEYS and fresh["configured"] is False
    assert set(fresh["dial"]) == {"active_fraction", "actions_per_hour", "diurnal_amplitude", "lookalike_share"}
    put = client.put(f"/noise/ranges/{rng.id}", json={"enabled": True, "level": 55, "targets": TARGETS}).json()
    assert set(put) == PROFILE_KEYS and put["configured"] is True and put["targets"] == TARGETS


def test_agents_personas_plan_and_stats_shapes(client, rng):
    tokens = _setup(client, rng, level=95, count=20)
    issued_again = client.post(f"/noise/ranges/{rng.id}/agents", json={"agents": [{"node": "ws01"}]}).json()
    assert set(issued_again[0]) == AGENT_KEYS | {"token"}
    tokens["ws01"] = issued_again[0]["token"]

    agents = client.get(f"/noise/ranges/{rng.id}/agents").json()
    assert agents and all(set(a) == AGENT_KEYS for a in agents)
    assert all("token" not in a for a in agents), "a token is shown only when issued"

    personas = client.get(f"/noise/ranges/{rng.id}/personas").json()
    assert personas and all(set(p) == PERSONA_KEYS for p in personas)

    plan = client.get(f"/noise/ranges/{rng.id}/plan", params={"node": "ws01", "minutes": 60}).json()
    assert set(plan) == {"node", "level", "actions"} and plan["actions"]
    assert set(plan["actions"][0]) == {"at", "persona", "kind", "target", "lookalike", "params"}

    agent_plan = client.get("/noise/agent/plan", params={"minutes": 60}, headers=_hdr(tokens["ws01"])).json()
    assert set(agent_plan) == {"node", "level", "poll_seconds", "actions"}
    assert set(agent_plan["actions"][0]) == {"at", "persona", "kind", "target", "params", "sig"}

    report = client.post(
        "/noise/agent/report", json={"results": agent_plan["actions"][:2]}, headers=_hdr(tokens["ws01"])
    ).json()
    assert report == {"accepted": 2, "rejected": 0, "duplicate": 0}

    activity = client.get(f"/noise/ranges/{rng.id}/activity").json()
    assert activity and set(activity[0]) == {"at", "node", "persona", "kind", "target", "lookalike", "ok", "detail"}

    stats = client.get(f"/noise/ranges/{rng.id}/stats").json()
    assert set(stats) == {"minutes", "total", "failed", "lookalikes", "by_kind", "agents"}
    assert stats["total"] == 2


def test_a_dry_run_deploy_has_no_task_id(client, rng, db_session):
    rng.template.yaml = yaml.safe_dump({"noise": {"enabled": True}, "nodes": []})
    rng.state = RangeState.ready
    db_session.commit()
    dry = client.post(f"/noise/ranges/{rng.id}/deploy", json={"dry_run": True})
    assert dry.status_code == 200, dry.text
    assert set(dry.json()) == {
        "dry_run",
        "agents",
        "skipped",
        "targets",
        "dropped_targets",
        "controller_url",
        "mgmt_cidr",
    }


def test_presets_shape(client):
    body = client.get("/noise/presets").json()
    assert set(body) == {"presets", "activities", "lookalikes", "target_pools"}
    assert body["presets"]["office"] == 40
