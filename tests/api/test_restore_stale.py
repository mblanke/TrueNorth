"""A restore that never finished does not lock its range for good.

Re-review of #39: while a snapshot is ``restoring`` every operation on the range is
refused (409). A restore whose worker was killed never sets it back, so the range could
never be stopped, started, destroyed or restored again, with no API way out. A running
restore holds the range's lease (worker/fencing.py); a ``restoring`` row untouched for
``RESTORE_STALE_AFTER`` with no lease held belongs to a worker that died: it no longer
blocks anything, and restoring or deleting it gives it back first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import celery_client
from app.models import Range, RangeSnapshot, RangeState


@pytest.fixture
def sent(monkeypatch):
    calls: list = []
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *a: calls.append(task) or "t")
    return calls


def _range_restoring(client, db, age: timedelta) -> Range:
    tmpl = client.post("/templates", json={"name": "T", "version": "1.0", "yaml": "id: t\n", "is_public": True})
    rid = client.post("/ranges", json={"name": f"R {uuid.uuid4().hex[:6]}", "template_id": tmpl.json()["id"]})
    rng = db.get(Range, uuid.UUID(rid.json()["id"]))
    rng.state = RangeState.ready
    rng.provisioner_output = '{"vms": [{"name": "dc1", "vm_id": "vm-1"}]}'
    snap = RangeSnapshot(
        range_id=rng.id, name="s", tenant_id=rng.tenant_id, range_state_at_snapshot="ready", snapshot_state="restoring"
    )
    db.add(snap)
    db.flush()
    snap.updated_at = datetime.now(UTC) - age
    db.commit()
    return rng


def test_a_restore_in_progress_still_blocks(client, db_session, sent):
    rng = _range_restoring(client, db_session, timedelta(minutes=5))
    assert client.post(f"/ranges/{rng.id}/stop").status_code == 409


def test_a_restore_that_never_finished_stops_blocking(client, db_session, sent):
    rng = _range_restoring(client, db_session, timedelta(hours=6))
    r = client.post(f"/ranges/{rng.id}/stop")
    assert r.status_code == 202, r.text


def _snapshot(db, rng):
    from app.models import RangeSnapshot

    return db.query(RangeSnapshot).filter(RangeSnapshot.range_id == rng.id).one()


def test_a_snapshot_stuck_restoring_can_be_restored_again(client, db_session, sent):
    """Its worker died: restore said 404 "not ready" and delete 409 "is restoring", so the
    snapshot (and its copy on the hypervisor) could never be used or removed."""
    rng = _range_restoring(client, db_session, timedelta(hours=6))
    snap = _snapshot(db_session, rng)
    r = client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore")
    assert r.status_code == 202, r.text


def test_a_snapshot_stuck_restoring_can_be_deleted(client, db_session, sent):
    rng = _range_restoring(client, db_session, timedelta(hours=6))
    snap = _snapshot(db_session, rng)
    assert client.delete(f"/ranges/{rng.id}/snapshots/{snap.id}").status_code == 204


def test_a_live_restore_is_not_taken_for_stuck(client, db_session, sent):
    rng = _range_restoring(client, db_session, timedelta(hours=3))  # a retry may still be running
    snap = _snapshot(db_session, rng)
    assert client.delete(f"/ranges/{rng.id}/snapshots/{snap.id}").status_code == 409


def test_an_old_restore_still_holding_the_range_lease_is_live(client, db_session, sent):
    """The time counts from the dispatch (queue wait included); a restore that is running
    holds the range's lease, and that alone keeps it from being taken for stuck."""
    from app.models_range_ops import RangeLease

    rng = _range_restoring(client, db_session, timedelta(hours=6))
    db_session.add(RangeLease(range_id=rng.id, holder="worker", expires_at=datetime.now(UTC) + timedelta(minutes=30)))
    db_session.commit()
    snap = _snapshot(db_session, rng)
    assert client.post(f"/ranges/{rng.id}/stop").status_code == 409
    assert client.delete(f"/ranges/{rng.id}/snapshots/{snap.id}").status_code == 409
