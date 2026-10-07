"""Integration test: a booking builds its own range and exercise (ADR 0004 slice 11).

Book a session from a template and a scenario with no range linked -> run the scheduler
clock (POST /schedule/tick, platform admin; the background clock may get there first,
which the guarded claims make harmless) -> the booking now holds a range built from the
template, through a range operation, and a pending exercise on it -> the booking is in
the event list and in the caller's iCalendar feed. Cancelling it at the end tears down
the range it built and withdraws the unstarted exercise.

Runs against the live stack (API_BASE_URL; AUTH_DISABLED, so the caller is the dev
admin, who is the platform admin when PLATFORM_TENANT_ID is unset).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

# One small VM: the template must declare its VMs, or the booking cannot be sized (422).
TEMPLATE_YAML = (
    "name: integ-scheduler\nnodes:\n  - id: ws\n    os: ubuntu22\n    vcpu: 1\n    ram_mb: 512\n    disk_gb: 8\n"
)
SCENARIO_YAML = "name: integ-scheduler-scenario\nobjectives: []\n"


def _created(resp, what: str) -> dict:
    assert resp.status_code in (200, 201), f"{what} => {resp.status_code} {resp.text}"
    return resp.json()


@pytest.fixture
def booking_inputs(api_client):
    tag = uuid.uuid4().hex[:8]
    tpl = _created(
        api_client.post("/templates", json={"name": f"integ-sched-{tag}", "yaml": TEMPLATE_YAML, "is_public": False}),
        "POST /templates",
    )
    scn = _created(
        api_client.post("/scenarios", json={"name": f"integ-sched-{tag}", "yaml": SCENARIO_YAML, "is_public": False}),
        "POST /scenarios",
    )
    me = api_client.get("/auth/me")
    assert me.status_code == 200, me.text
    yield {"tag": tag, "template_id": tpl["id"], "scenario_id": scn["id"], "me": me.json()["user"]["id"]}
    api_client.delete(f"/scenarios/{scn['id']}")
    api_client.delete(f"/templates/{tpl['id']}")


def test_a_booking_creates_its_range_and_exercise_and_shows_in_the_feed(api_client, booking_inputs):
    # Inside the provisioning lead (30 min by default), so the next tick builds it.
    start = (datetime.now(UTC) + timedelta(minutes=10)).replace(microsecond=0)
    end = start + timedelta(hours=1)
    name = f"integ-scheduler-{booking_inputs['tag']}"

    evt = _created(
        api_client.post(
            "/schedule/events",
            json={
                "name": name,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "template_id": booking_inputs["template_id"],
                "scenario_id": booking_inputs["scenario_id"],
                "instructor_id": booking_inputs["me"],
            },
        ),
        "POST /schedule/events",
    )
    event_id = evt["id"]
    try:
        assert evt["state"] == "scheduled"
        assert evt["range_id"] is None and evt["exercise_id"] is None
        assert evt["vm_count"] == 1  # sized from the template

        tick = api_client.post("/schedule/tick")
        assert tick.status_code == 200, tick.text

        got = api_client.get(f"/schedule/events/{event_id}")
        assert got.status_code == 200, got.text
        evt = got.json()
        assert evt["state"] == "provisioning", evt
        assert evt["range_id"] and evt["exercise_id"], evt

        # The range came from the booking's template and was built through range_ops.
        rng = api_client.get(f"/ranges/{evt['range_id']}")
        assert rng.status_code == 200, rng.text
        assert rng.json()["template_id"] == booking_inputs["template_id"]
        assert rng.json()["state"] in ("provisioning", "ready"), rng.json()
        ops = api_client.get(f"/ranges/{evt['range_id']}/operations")
        assert ops.status_code == 200, ops.text
        assert [o["action"] for o in ops.json()] == ["provision"], ops.json()

        # A pending exercise on that range, running the booked scenario.
        ex = api_client.get(f"/exercises/{evt['exercise_id']}")
        assert ex.status_code == 200, ex.text
        assert ex.json()["range_id"] == evt["range_id"]
        assert ex.json()["scenario_id"] == booking_inputs["scenario_id"]
        assert ex.json()["state"] == "pending"

        # In the schedule: the event list, and the caller's calendar feed.
        listed = api_client.get(
            "/schedule/events",
            params={"start": (start - timedelta(minutes=1)).isoformat(), "end": end.isoformat(), "limit": 200},
        )
        assert listed.status_code == 200, listed.text
        assert event_id in {e["id"] for e in listed.json()["items"]}

        issued = api_client.post("/schedule/feed-token")
        assert issued.status_code == 200, issued.text
        token = issued.json()["url"].rsplit("/", 1)[1].removesuffix(".ics")
        ics = api_client.get(f"/schedule/feed/{token}.ics")
        assert ics.status_code == 200, ics.text
        assert ics.headers["content-type"].startswith("text/calendar")
        assert f"UID:{event_id}@" in ics.text
        assert name in ics.text
    finally:
        cancelled = api_client.post(f"/schedule/events/{event_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        api_client.delete("/schedule/feed-token")
