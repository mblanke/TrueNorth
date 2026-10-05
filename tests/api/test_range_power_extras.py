"""Power behaviour #30 added on top of #33's power operations (kept in the integration).

* A restore settles a finished but unread stop/start first: the restore task moves the
  range, and an operation still ``dispatched`` would be judged on the restored state
  (with #33's outcome maps, a stop seeing ``ready`` has no outcome and turns ``no_outcome``).
* An exercise does not run on a range whose VMs are off or changing power state.
"""

from __future__ import annotations

import uuid

import pytest
from app import celery_client
from app.models import Range, RangeSnapshot, RangeState


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: calls.append((task, *args)) or "t")
    return calls


def _range(client, db, state: RangeState) -> Range:
    tmpl = client.post("/templates", json={"name": "T", "version": "1.0", "yaml": "id: t\n", "is_public": True})
    rid = client.post("/ranges", json={"name": f"R {uuid.uuid4().hex[:6]}", "template_id": tmpl.json()["id"]})
    rng = db.get(Range, uuid.UUID(rid.json()["id"]))
    rng.state = state
    rng.provisioner_output = '{"vms": [{"name": "dc1", "vm_id": "vm-1"}]}'
    db.commit()
    return rng


def test_a_restore_settles_a_finished_stop_before_it_changes_the_range(client, db_session, sent):
    rng = _range(client, db_session, RangeState.ready)
    assert client.post(f"/ranges/{rng.id}/stop").status_code == 202
    rng.state = RangeState.stopped  # the worker's report; nobody reads the operation
    snap = RangeSnapshot(
        range_id=rng.id, name="s", tenant_id=rng.tenant_id, range_state_at_snapshot="ready", snapshot_state="ready"
    )
    db_session.add(snap)
    db_session.commit()
    assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
    rng.state = RangeState.ready  # the restore task's outcome
    db_session.commit()
    assert client.get(f"/ranges/{rng.id}/operations").json()[0]["status"] == "succeeded"


@pytest.mark.parametrize("state", [RangeState.stopped, RangeState.stopping, RangeState.starting])
def test_an_exercise_does_not_run_on_a_range_whose_vms_are_off(client, db_session, sent, state):
    rng = _range(client, db_session, state)
    sid = client.post(
        "/scenarios", json={"name": "S", "version": "1.0", "yaml": "id: s\ntimeline: []", "is_public": True}
    ).json()["id"]
    ex = client.post("/exercises", json={"name": "E", "range_id": str(rng.id), "scenario_id": sid, "max_score": 10})
    r = client.post(f"/exercises/{ex.json()['id']}/run")
    assert r.status_code == 409 and "power" in r.json()["detail"].lower(), r.text
