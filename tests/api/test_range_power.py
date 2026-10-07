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


def test_a_power_request_the_broker_refused_is_kept_and_sent_later(client, db_session, monkeypatch):
    from datetime import timedelta

    from app import celery_client
    from app.range_ops.service import redispatch_pending

    rid = _range(client, db_session, "ready")
    dispatch = celery_client.dispatch
    monkeypatch.setattr(celery_client, "dispatch", lambda *a, **k: None)
    assert client.post(f"/ranges/{rid}/stop").json()["state"] == "stopping"
    (op,) = client.get(f"/ranges/{rid}/operations").json()
    assert op["action"] == "stop" and op["status"] == "pending"
    monkeypatch.setattr(celery_client, "dispatch", dispatch)
    assert redispatch_pending(db_session, min_age=timedelta(0)) == 1


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


# ── From the adversarial review of 5dd2457 ──────────────────────────────


def _snapshot(db_session, rid: str, state: str):
    from app.models import RangeSnapshot

    rng = db_session.get(Range, uuid.UUID(rid))
    snap = RangeSnapshot(
        range_id=rng.id, name="s", snapshot_state=state, range_state_at_snapshot="ready", tenant_id=rng.tenant_id
    )
    db_session.add(snap)
    db_session.commit()
    return snap


def test_a_started_range_can_still_be_snapshotted_and_restored(client, db_session, no_real_broker):
    """After stop and start the worker writes running; snapshot and restore took only
    ready / stopped, so one power cycle took them away."""
    rid = _range(client, db_session, "running")
    assert client.post(f"/ranges/{rid}/snapshots", json={"name": "after-start"}).status_code == 202
    snap = _snapshot(db_session, rid, "ready")
    assert client.post(f"/ranges/{rid}/snapshots/{snap.id}/restore").status_code == 202


@pytest.mark.parametrize("snapshot_state", ["creating", "restoring"])
def test_power_is_refused_while_a_snapshot_is_taken_or_restored(client, db_session, no_real_broker, snapshot_state):
    rid = _range(client, db_session, "ready")
    _snapshot(db_session, rid, snapshot_state)
    resp = client.post(f"/ranges/{rid}/stop")
    assert resp.status_code == 409 and "snapshot" in resp.json()["detail"]
    assert no_real_broker.sent == []


def test_a_range_under_a_running_exercise_is_not_stopped(client, db_session, no_real_broker):
    from app.models import Exercise, ExerciseState, Scenario

    rid = _range(client, db_session, "ready")
    rng = db_session.get(Range, uuid.UUID(rid))
    sc = Scenario(name="s", yaml="id: s\n", tenant_id=rng.tenant_id)
    db_session.add(sc)
    db_session.flush()
    db_session.add(
        Exercise(name="e", range_id=rng.id, scenario_id=sc.id, tenant_id=rng.tenant_id, state=ExerciseState.running)
    )
    db_session.commit()
    resp = client.post(f"/ranges/{rid}/stop")
    assert resp.status_code == 409 and "exercise" in resp.json()["detail"]


@pytest.mark.parametrize("state", ["stopping", "starting"])
def test_a_range_whose_power_task_was_lost_can_still_be_destroyed(client, db_session, no_real_broker, state):
    rid = _range(client, db_session, state)
    resp = client.post(f"/ranges/{rid}/destroy")
    assert resp.status_code == 202 and resp.json()["state"] == "destroying"


def test_on_postgres_the_migrated_enum_takes_the_power_states(postgres_engine):
    """The rangestate enum is a native PostgreSQL type: the new values exist only if the
    migration (e3f4a5b6c7d8) added them. SQLite stores the enum as text and cannot tell."""
    from app.models import Template, Tenant
    from sqlalchemy.orm import Session

    with Session(postgres_engine) as s:
        tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        s.flush()
        tmpl = Template(name="t", yaml="id: t\n", tenant_id=tenant.id)
        s.add(tmpl)
        s.flush()
        rng = Range(name="r", template_id=tmpl.id, tenant_id=tenant.id, state=RangeState.stopping)
        s.add(rng)
        s.commit()
        rng.state = RangeState.starting
        s.commit()
        s.refresh(rng)
        assert rng.state == RangeState.starting
