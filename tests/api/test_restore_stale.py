"""A restore that never finished does not lock its range for good.

Re-review of #39: while a snapshot is ``restoring`` every operation on the range is
refused (409). A restore whose worker was killed never sets it back, so the range could
never be stopped, started, destroyed or restored again, with no API way out. A restore
cannot run longer than the worker's hard time limit, so a ``restoring`` row older than
``RESTORE_STALE_AFTER`` no longer blocks anything.
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
    rng = _range_restoring(client, db_session, timedelta(hours=3))
    r = client.post(f"/ranges/{rng.id}/stop")
    assert r.status_code == 202, r.text
