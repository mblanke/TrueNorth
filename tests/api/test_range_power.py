"""POST /ranges/{id}/stop and /start are range operations (app/range_ops.py), like provision.

S0 found /stop set the range to ``stopped`` and did nothing else: every VM kept running.
The vmware branch then sent a power task, but still set ``stopped``/``running`` in the API
before anything had happened, outside any operation: no record, no Idempotency-Key, a
broker outage lost the request silently, and two clicks raced. Now a stop is accepted
like a provision: an operation row and the in-progress state (``stopping`` /
``starting``) commit together, the task is sent after, the worker writes the observed
state, and the operation's outcome is reconciled from it.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from app import celery_client, range_ops
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Range, RangeSnapshot, RangeState, UserRole

OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
POWER = {"stop": ("stop_range", "stopping", "stopped"), "start": ("start_range", "starting", "running")}


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []

    def send(task, *args):
        calls.append((task, *args))
        return f"task-{len(calls)}"

    monkeypatch.setattr(celery_client, "dispatch", send)
    return calls


def _range(client, db_session, state: str) -> Range:
    tmpl = client.post("/templates", json={"name": f"T {uuid.uuid4().hex[:6]}", "version": "1.0",
                                           "yaml": "id: t\n", "is_public": True}).json()
    rid = client.post("/ranges", json={"name": f"R {uuid.uuid4().hex[:6]}", "template_id": tmpl["id"]}).json()["id"]
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state = RangeState(state)
    db_session.flush()
    return rng


def _worker_reports(db_session, rng: Range, state: str):
    rng.state = RangeState(state)
    db_session.flush()


def _ops(client, rid) -> list[dict]:
    r = client.get(f"/ranges/{rid}/operations")
    assert r.status_code == 200, r.text
    return r.json()


@contextmanager
def _acting_as(role, tenant):
    who = CurrentUser(id=str(uuid.uuid4()), email="x@x", display_name="x", role=role, tenant_id=tenant,
                      keycloak_id="kc")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(("action", "state"), [("stop", "ready"), ("stop", "running"), ("start", "stopped")])
def test_a_power_request_is_a_recorded_operation_and_the_state_says_it_is_in_progress(
    client, db_session, sent, action, state
):
    task, in_progress, _ = POWER[action]
    rng = _range(client, db_session, state)
    r = client.post(f"/ranges/{rng.id}/{action}")
    assert r.status_code == 202, r.text
    assert r.json()["state"] == in_progress, "not the target state: nothing has been powered yet"
    [op] = _ops(client, rng.id)
    assert (op["action"], op["status"], op["task_id"]) == (action, "dispatched", "task-1")
    assert r.headers["Operation-Id"] == op["id"]
    assert sent == [(task, str(rng.id))]


@pytest.mark.parametrize("action", ["stop", "start"])
def test_the_outcome_is_reconciled_from_what_the_worker_observed(client, db_session, sent, action):
    _, _, done = POWER[action]
    rng = _range(client, db_session, "running" if action == "stop" else "stopped")
    client.post(f"/ranges/{rng.id}/{action}")
    _worker_reports(db_session, rng, done)
    [op] = _ops(client, rng.id)
    assert op["status"] == "succeeded"

    other = _range(client, db_session, "running" if action == "stop" else "stopped")
    client.post(f"/ranges/{other.id}/{action}")
    other.error_message = "vm-1: host down"
    _worker_reports(db_session, other, "failed")
    [op] = _ops(client, other.id)
    assert (op["status"], op["error"]["code"]) == ("failed", "range_failed")


def test_a_failed_power_operation_can_be_retried_without_destroying_the_range(client, db_session, sent):
    rng = _range(client, db_session, "failed")
    assert client.post(f"/ranges/{rng.id}/start").status_code == 202
    _worker_reports(db_session, rng, "running")
    assert client.post(f"/ranges/{rng.id}/stop").status_code == 202


def test_the_same_idempotency_key_replays_the_original_stop(client, db_session, sent):
    rng = _range(client, db_session, "running")
    a = client.post(f"/ranges/{rng.id}/stop", headers={"Idempotency-Key": "k1"})
    b = client.post(f"/ranges/{rng.id}/stop", headers={"Idempotency-Key": "k1"})
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.headers["Operation-Id"] == b.headers["Operation-Id"]
    assert len(sent) == 1


def test_a_second_power_request_while_one_is_in_flight_is_refused(client, db_session, sent):
    rng = _range(client, db_session, "running")
    assert client.post(f"/ranges/{rng.id}/stop").status_code == 202
    assert client.post(f"/ranges/{rng.id}/stop").status_code == 409
    assert client.post(f"/ranges/{rng.id}/start").status_code == 409
    assert len(sent) == 1


def test_with_the_broker_down_the_stop_is_kept_and_sent_later(client, db_session, monkeypatch):
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: None)
    rng = _range(client, db_session, "running")
    r = client.post(f"/ranges/{rng.id}/stop")
    assert (r.status_code, r.json()["state"]) == (202, "stopping")
    [op] = _ops(client, rng.id)
    assert (op["status"], op["error"]["code"]) == ("pending", "broker_unavailable")

    sent: list = []
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: sent.append((task, *args)) or "task-9")
    assert range_ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1
    assert sent == [("stop_range", str(rng.id))]


def test_an_abandoned_stop_leaves_the_range_failed(client, db_session, sent):
    rng = _range(client, db_session, "running")
    op_id = client.post(f"/ranges/{rng.id}/stop").headers["Operation-Id"]
    assert client.post(f"/ranges/{rng.id}/operations/{op_id}/abandon").status_code == 200
    db_session.refresh(rng)
    assert rng.state == RangeState.failed


@pytest.mark.parametrize(
    ("action", "state"),
    [("stop", "created"), ("stop", "stopped"), ("stop", "destroyed"), ("stop", "provisioning"),
     ("start", "running"), ("start", "ready"), ("start", "created"), ("start", "stopping")],
)
def test_wrong_state_is_refused_and_nothing_is_recorded_or_sent(client, db_session, sent, action, state):
    rng = _range(client, db_session, state)
    assert client.post(f"/ranges/{rng.id}/{action}").status_code == 409
    assert sent == []
    assert _ops(client, rng.id) == []


@pytest.mark.parametrize("action", ["stop", "start"])
def test_a_power_request_during_a_snapshot_restore_is_refused(client, db_session, sent, action):
    rng = _range(client, db_session, "running" if action == "stop" else "stopped")
    db_session.add(RangeSnapshot(range_id=rng.id, name="s", snapshot_state="restoring", range_state_at_snapshot="running", tenant_id=rng.tenant_id))
    db_session.flush()
    assert client.post(f"/ranges/{rng.id}/{action}").status_code == 409
    assert sent == []


@pytest.mark.parametrize("action", ["stop", "start"])
def test_another_tenant_gets_404_and_a_student_403(client, db_session, sent, action):
    rng = _range(client, db_session, "running" if action == "stop" else "stopped")
    with _acting_as(UserRole.admin, OTHER_TENANT):
        assert client.post(f"/ranges/{rng.id}/{action}").status_code == 404
    with _acting_as(UserRole.student, str(rng.tenant_id)):
        assert client.post(f"/ranges/{rng.id}/{action}").status_code == 403
    assert sent == []
