"""Snapshot endpoints: tenant scoping and refusing work that would race the worker.

DELETE /ranges/{id}/snapshots/{sid} carried a "tenant-safe: _tenant_range() above"
comment with no such call above it, so any tenant could delete any other tenant's
snapshot given the two ids. While the worker's delete was broken that only flipped a
row; once it worked, it removed the copy on the hypervisor.
"""

import uuid
from unittest.mock import patch

import pytest

DEV_TENANT = "00000000-0000-0000-0000-000000000001"  # what AUTH_DISABLED signs in as
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _range(db, tenant_id: str, name: str):
    from app.models import Range, RangeState

    r = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(tenant_id), name=name, template_id=uuid.uuid4())
    r.state = RangeState.ready
    db.add(r)
    db.flush()
    return r


def _snapshot(db, rng, state: str = "ready"):
    from app.models import RangeSnapshot

    s = RangeSnapshot(
        range_id=rng.id,
        name=f"snap-{state}",
        range_state_at_snapshot="ready",
        tenant_id=rng.tenant_id,
        snapshot_state=state,
    )
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def dispatched():
    with patch("app.routers.ranges._dispatch_task") as spy:
        yield spy


class TestDeleteSnapshot:
    def test_another_tenants_snapshot_cannot_be_deleted(self, client, db_session, dispatched):
        theirs = _range(db_session, OTHER_TENANT, "theirs")
        snap = _snapshot(db_session, theirs)
        db_session.commit()

        resp = client.delete(f"/ranges/{theirs.id}/snapshots/{snap.id}")

        assert resp.status_code == 404, f"LEAK: deleted another tenant's snapshot ({resp.status_code})"
        dispatched.assert_not_called()
        db_session.refresh(snap)
        assert snap.snapshot_state == "ready"

    def test_own_ready_snapshot_is_deleted(self, client, db_session, dispatched):
        mine = _range(db_session, DEV_TENANT, "mine")
        snap = _snapshot(db_session, mine)
        db_session.commit()

        assert client.delete(f"/ranges/{mine.id}/snapshots/{snap.id}").status_code == 204
        dispatched.assert_called_once_with("delete_snapshot", str(mine.id), str(snap.id))

    @pytest.mark.parametrize("state", ["creating", "restoring"])
    def test_a_snapshot_the_worker_holds_is_refused(self, client, db_session, dispatched, state):
        mine = _range(db_session, DEV_TENANT, "mine")
        snap = _snapshot(db_session, mine, state)
        db_session.commit()

        resp = client.delete(f"/ranges/{mine.id}/snapshots/{snap.id}")

        assert resp.status_code == 409
        dispatched.assert_not_called()


class TestNoOverlapWithARestore:
    def test_second_restore_is_refused(self, client, db_session, dispatched):
        mine = _range(db_session, DEV_TENANT, "mine")
        _snapshot(db_session, mine, "restoring")
        other = _snapshot(db_session, mine, "ready")
        db_session.commit()

        resp = client.post(f"/ranges/{mine.id}/snapshots/{other.id}/restore")

        assert resp.status_code == 409
        dispatched.assert_not_called()

    def test_new_snapshot_is_refused(self, client, db_session, dispatched):
        mine = _range(db_session, DEV_TENANT, "mine")
        _snapshot(db_session, mine, "restoring")
        db_session.commit()

        resp = client.post(f"/ranges/{mine.id}/snapshots", json={"name": "during-restore"})

        assert resp.status_code == 409
        dispatched.assert_not_called()

    def test_restore_goes_ahead_when_nothing_else_is_running(self, client, db_session, dispatched):
        mine = _range(db_session, DEV_TENANT, "mine")
        snap = _snapshot(db_session, mine, "ready")
        db_session.commit()

        assert client.post(f"/ranges/{mine.id}/snapshots/{snap.id}/restore").status_code == 202
        dispatched.assert_called_once_with("restore_snapshot", str(mine.id), str(snap.id))
