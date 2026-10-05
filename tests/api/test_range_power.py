"""Stop and start are operations on the hypervisor, not labels (codereview1 S0 defect).

``POST /ranges/{id}/stop`` used to set ``state = stopped`` and send nothing, so the
platform said a range was off while its VMs kept running; it was also only allowed from
``running``, which nothing sets, and there was no way to start a stopped range again.
Now stop and start are range operations like provision and destroy (app/range_ops.py):
202, a durable operation row, the range in ``stopping``/``starting`` until the worker
reports, and ``stopped``/``ready`` only once it has.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from app import celery_client, range_ops
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Range, RangeState, UserRole

OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []

    def send(task, *args):
        calls.append((task, *args))
        return f"task-{len(calls)}"

    monkeypatch.setattr(celery_client, "dispatch", send)
    return calls


def _range(client, db, state: RangeState) -> str:
    tmpl = client.post("/templates", json={"name": "T", "version": "1.0", "yaml": "id: t\n", "is_public": True})
    rid = client.post("/ranges", json={"name": f"R {uuid.uuid4().hex[:6]}", "template_id": tmpl.json()["id"]}).json()[
        "id"
    ]
    db.get(Range, uuid.UUID(rid)).state = state
    db.commit()
    return rid


def _worker_reports(db, rid: str, state: RangeState, error: str | None = None) -> None:
    rng = db.get(Range, uuid.UUID(rid))
    rng.state, rng.error_message = state, error
    db.commit()


def _ops(client, rid):
    r = client.get(f"/ranges/{rid}/operations")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("from_state", [RangeState.ready, RangeState.running])
def test_stop_is_accepted_and_sent_not_assumed(client, db_session, sent, from_state):
    rid = _range(client, db_session, from_state)
    r = client.post(f"/ranges/{rid}/stop")
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "stopping", "nothing is stopped until the worker says so"
    assert r.headers["Operation-Id"]
    assert sent == [("stop_range", rid)]
    (op,) = _ops(client, rid)
    assert (op["action"], op["status"]) == ("stop", "dispatched")


def test_the_range_reads_stopped_only_when_the_worker_reports_it(client, db_session, sent):
    rid = _range(client, db_session, RangeState.ready)
    client.post(f"/ranges/{rid}/stop")
    _worker_reports(db_session, rid, RangeState.stopped)
    assert _ops(client, rid)[0]["status"] == "succeeded"
    assert client.get(f"/ranges/{rid}").json()["state"] == "stopped"


def test_a_stop_the_worker_could_not_do_is_a_failed_operation(client, db_session, sent):
    rid = _range(client, db_session, RangeState.ready)
    client.post(f"/ranges/{rid}/stop")
    _worker_reports(db_session, rid, RangeState.ready, "VM r-dc01: timed out")
    op = _ops(client, rid)[0]
    assert op["status"] == "failed" and "r-dc01" in op["error"]["message"]


def test_a_stopped_range_can_be_started(client, db_session, sent):
    rid = _range(client, db_session, RangeState.stopped)
    r = client.post(f"/ranges/{rid}/start")
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "starting"
    assert sent == [("start_range", rid)]
    _worker_reports(db_session, rid, RangeState.ready)
    assert _ops(client, rid)[0]["status"] == "succeeded"


def test_a_start_the_worker_could_not_do_is_a_failed_operation(client, db_session, sent):
    rid = _range(client, db_session, RangeState.stopped)
    client.post(f"/ranges/{rid}/start")
    _worker_reports(db_session, rid, RangeState.stopped, "vCenter unreachable")
    assert _ops(client, rid)[0]["status"] == "failed"


@pytest.mark.parametrize(
    ("action", "state"),
    [
        ("stop", RangeState.created),
        ("stop", RangeState.stopped),
        ("stop", RangeState.destroyed),
        ("start", RangeState.ready),
        ("start", RangeState.created),
        ("start", RangeState.failed),
    ],
)
def test_power_actions_follow_the_state_machine(client, db_session, sent, action, state):
    rid = _range(client, db_session, state)
    assert client.post(f"/ranges/{rid}/{action}").status_code == 409
    assert sent == [] and _ops(client, rid) == []


def test_a_range_mid_stop_cannot_be_started_or_stopped_again(client, db_session, sent):
    rid = _range(client, db_session, RangeState.ready)
    client.post(f"/ranges/{rid}/stop")
    assert client.post(f"/ranges/{rid}/stop").status_code == 409
    assert client.post(f"/ranges/{rid}/start").status_code == 409
    assert len(sent) == 1


def test_a_stopped_range_can_still_be_destroyed(client, db_session, sent):
    rid = _range(client, db_session, RangeState.stopped)
    assert client.post(f"/ranges/{rid}/destroy").status_code == 202


def test_idempotency_key_replays_the_stop(client, db_session, sent):
    rid = _range(client, db_session, RangeState.ready)
    a = client.post(f"/ranges/{rid}/stop", headers={"Idempotency-Key": "s-1"})
    b = client.post(f"/ranges/{rid}/stop", headers={"Idempotency-Key": "s-1"})
    assert a.status_code == b.status_code == 202
    assert a.headers["Operation-Id"] == b.headers["Operation-Id"] and len(sent) == 1


def test_broker_down_the_stop_waits_and_is_sent_later(client, db_session, monkeypatch):
    rid = _range(client, db_session, RangeState.ready)
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: None)
    assert client.post(f"/ranges/{rid}/stop").status_code == 202
    assert _ops(client, rid)[0]["error"]["code"] == "broker_unavailable"
    calls = []
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: calls.append((task, *args)) or "t")
    assert range_ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1
    assert calls == [("stop_range", rid)]


@contextmanager
def acting_as(role: UserRole, tenant: str):
    who = CurrentUser(
        id=str(uuid.uuid4()), email="x@example.test", display_name="x", role=role, tenant_id=tenant, keycloak_id="kc"
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("action", ["stop", "start"])
def test_another_tenant_gets_404_and_a_student_403(client, db_session, sent, action):
    rid = _range(client, db_session, RangeState.ready if action == "stop" else RangeState.stopped)
    with acting_as(UserRole.admin, OTHER_TENANT):
        assert client.post(f"/ranges/{rid}/{action}").status_code == 404
    with acting_as(UserRole.student, "00000000-0000-0000-0000-000000000001"):
        assert client.post(f"/ranges/{rid}/{action}").status_code == 403
    assert sent == []


# ── From the S3d review ────────────────────────────────────────────────
def _abandon(client, rid):
    op = _ops(client, rid)[0]
    r = client.post(f"/ranges/{rid}/operations/{op['id']}/abandon")
    assert r.status_code == 200, r.text
    return client.get(f"/ranges/{rid}").json()


def test_abandoning_a_stop_leaves_a_built_range_not_failed(client, db_session, sent):
    """failed offers 'Provision again', which would orphan the VMs that still exist."""
    rid = _range(client, db_session, RangeState.ready)
    client.post(f"/ranges/{rid}/stop")
    rng = _abandon(client, rid)
    assert rng["state"] == "ready" and "abandoned" in rng["error_message"]
    assert client.post(f"/ranges/{rid}/provision").status_code == 409


def test_abandoning_a_start_returns_the_range_to_stopped(client, db_session, sent):
    rid = _range(client, db_session, RangeState.stopped)
    client.post(f"/ranges/{rid}/start")
    assert _abandon(client, rid)["state"] == "stopped"


def _restoring(db, rid):
    from app.models import RangeSnapshot

    rng = db.get(Range, uuid.UUID(rid))
    db.add(
        RangeSnapshot(
            range_id=rng.id,
            name="s",
            tenant_id=rng.tenant_id,
            range_state_at_snapshot="ready",
            snapshot_state="restoring",
        )
    )
    db.commit()


@pytest.mark.parametrize(("action", "state"), [("stop", RangeState.ready), ("start", RangeState.stopped)])
def test_power_waits_for_a_restore_in_progress(client, db_session, sent, action, state):
    rid = _range(client, db_session, state)
    _restoring(db_session, rid)
    r = client.post(f"/ranges/{rid}/{action}")
    assert r.status_code == 409 and "restore" in r.json()["detail"]
    assert sent == []


def test_a_restore_settles_a_finished_stop_before_it_changes_the_range(client, db_session, sent):
    """Unread, the stop was still 'dispatched'; the restore then put the range back in
    ready, and the next read would have called the stop failed."""
    from app.models import RangeSnapshot

    rid = _range(client, db_session, RangeState.ready)
    client.post(f"/ranges/{rid}/stop")
    _worker_reports(db_session, rid, RangeState.stopped)  # nobody reads the operation
    rng = db_session.get(Range, uuid.UUID(rid))
    snap = RangeSnapshot(
        range_id=rng.id, name="s", tenant_id=rng.tenant_id, range_state_at_snapshot="ready", snapshot_state="ready"
    )
    db_session.add(snap)
    db_session.commit()
    assert client.post(f"/ranges/{rid}/snapshots/{snap.id}/restore").status_code == 202
    _worker_reports(db_session, rid, RangeState.ready)  # the restore task's outcome
    assert _ops(client, rid)[0]["status"] == "succeeded"


def test_an_exercise_does_not_start_on_a_powered_off_range(client, db_session, sent):
    rid = _range(client, db_session, RangeState.stopped)
    sid = client.post(
        "/scenarios", json={"name": "S", "version": "1.0", "yaml": "id: s\ntimeline: []", "is_public": True}
    ).json()["id"]
    ex = client.post("/exercises", json={"name": "E", "range_id": rid, "scenario_id": sid, "max_score": 10}).json()
    r = client.post(f"/exercises/{ex['id']}/run")
    assert r.status_code == 409 and "power" in r.json()["detail"].lower()
