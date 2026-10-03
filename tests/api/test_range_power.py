"""POST /ranges/{id}/stop and /start must reach the worker.

/stop used to flip the range to `stopped` and nothing else: every VM kept running.
"""

import uuid
from unittest.mock import patch

import pytest

DEV_TENANT = "00000000-0000-0000-0000-000000000001"  # what AUTH_DISABLED signs in as
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _range(db, state: str, tenant: str = DEV_TENANT):
    from app.models import Range, RangeState

    r = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(tenant), name=f"r-{state}", template_id=uuid.uuid4())
    r.state = RangeState(state)
    db.add(r)
    db.commit()
    return r


@pytest.fixture
def dispatched():
    with patch("app.routers.ranges._dispatch_task") as spy:
        yield spy


def test_stop_dispatches_the_worker(client, db_session, dispatched):
    rng = _range(db_session, "running")
    resp = client.post(f"/ranges/{rng.id}/stop")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "stopped"
    dispatched.assert_called_once_with("stop_range", str(rng.id))


def test_start_dispatches_the_worker(client, db_session, dispatched):
    rng = _range(db_session, "stopped")
    resp = client.post(f"/ranges/{rng.id}/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "running"
    dispatched.assert_called_once_with("start_range", str(rng.id))


@pytest.mark.parametrize(("action", "state"), [("stop", "created"), ("stop", "destroyed"),
                                               ("start", "running"), ("start", "ready")])
def test_wrong_state_is_refused_without_dispatch(client, db_session, dispatched, action, state):
    rng = _range(db_session, state)
    assert client.post(f"/ranges/{rng.id}/{action}").status_code == 409
    dispatched.assert_not_called()


@pytest.mark.parametrize("action", ["stop", "start"])
def test_another_tenants_range_is_not_found(client, db_session, dispatched, action):
    rng = _range(db_session, "running" if action == "stop" else "stopped", tenant=OTHER_TENANT)
    assert client.post(f"/ranges/{rng.id}/{action}").status_code == 404
    dispatched.assert_not_called()
