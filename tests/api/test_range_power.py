"""POST /ranges/{id}/stop and /start power the range's VMs (CR1-05 in
docs/review/codereview1.md).

On main /stop set the range to ``stopped`` and sent nothing: the VMs kept running while
the platform said they were off, and there was no /start. Now the request is recorded
(``stopping`` / ``starting``) and the worker writes ``stopped`` / ``running`` once the
hypervisor has done it (worker/power_tasks.py).
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.models import Range, RangeState

VMS = json.dumps({"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]})


def _range(client, db_session, state: str, output: str | None = VMS) -> str:
    client.post("/tenants", json={"name": "Power Corp", "slug": "power-corp"})
    tmpl = client.post(
        "/templates", json={"name": "power-tmpl", "version": "1.0", "yaml": "assets: []", "is_public": True}
    ).json()
    rid = client.post("/ranges", json={"name": f"p-{uuid.uuid4().hex[:6]}", "template_id": tmpl["id"]}).json()["id"]
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state, rng.provisioner_output = RangeState(state), output
    db_session.commit()
    return rid


@pytest.mark.parametrize(
    ("action", "start", "claimed", "task"),
    [
        ("stop", "ready", "stopping", "worker.tasks.stop_range"),
        ("stop", "running", "stopping", "worker.tasks.stop_range"),
        ("start", "stopped", "starting", "worker.tasks.start_range"),
    ],
)
def test_a_power_request_is_recorded_and_sent_never_assumed(
    client, db_session, no_real_broker, action, start, claimed, task
):
    rid = _range(client, db_session, start)
    resp = client.post(f"/ranges/{rid}/{action}")
    assert resp.status_code == 202
    assert resp.json()["state"] == claimed, "the API must not claim a power state the VMs are not in"
    assert no_real_broker.sent == [(task, [rid])]


@pytest.mark.parametrize(
    ("action", "state"), [("stop", "created"), ("stop", "stopped"), ("start", "ready"), ("start", "stopping")]
)
def test_power_is_refused_from_a_state_it_cannot_leave(client, db_session, no_real_broker, action, state):
    rid = _range(client, db_session, state)
    assert client.post(f"/ranges/{rid}/{action}").status_code == 409
    assert no_real_broker.sent == []


def test_a_range_with_no_vms_cannot_be_powered(client, db_session, no_real_broker):
    rid = _range(client, db_session, "ready", output=None)
    resp = client.post(f"/ranges/{rid}/stop")
    assert resp.status_code == 409 and "no VMs" in resp.json()["detail"]
    assert no_real_broker.sent == []


def test_a_power_request_the_broker_refused_leaves_the_range_as_it_was(client, db_session, monkeypatch):
    from app import celery_client

    rid = _range(client, db_session, "ready")
    monkeypatch.setattr(celery_client, "dispatch", lambda *a, **k: None)
    assert client.post(f"/ranges/{rid}/stop").status_code == 503
    assert client.get(f"/ranges/{rid}").json()["state"] == "ready"


def test_another_tenants_range_cannot_be_powered(client, db_session, no_real_broker):
    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app
    from app.models import UserRole

    rid = _range(client, db_session, "ready")
    other = CurrentUser(
        id=str(uuid.uuid4()),
        email="o@example.test",
        display_name="o",
        role=UserRole.admin,
        tenant_id="00000000-0000-0000-0000-0000000000ff",
        keycloak_id="kc-o",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: other
    try:
        assert client.post(f"/ranges/{rid}/stop").status_code == 404
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    assert no_real_broker.sent == []
