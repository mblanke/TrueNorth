"""Range operations (app/range_ops; CR1-06 and F19 in docs/review/codereview1.md).

On main a provision or destroy was a state change plus a best-effort send: a broker that
was down, or a process that died between the commit and the send, left the range in
provisioning or destroying with nothing behind it, for good. Each request is now an
operation row written with the state change, used as its own outbox, with an
Idempotency-Key, outcomes reconciled from the range's observed state, and an operator's
abandon. Re-landed from #17 and #18.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import celery_client
from app.models import Range, RangeSnapshot, RangeState
from app.range_ops import RangeOperation
from app.range_ops import service as ops

VMS = json.dumps({"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]})


def _range(client, db_session, state: str = "created", output: str | None = None) -> str:
    client.post("/tenants", json={"name": "Ops Corp", "slug": "ops-corp"})
    tmpl = client.post(
        "/templates", json={"name": "ops-tmpl", "version": "1.0", "yaml": "assets: []", "is_public": True}
    ).json()
    rid = client.post("/ranges", json={"name": f"o-{uuid.uuid4().hex[:6]}", "template_id": tmpl["id"]}).json()["id"]
    if state != "created" or output:
        rng = db_session.get(Range, uuid.UUID(rid))
        rng.state, rng.provisioner_output = RangeState(state), output
        db_session.commit()
    return rid


def _set_state(db_session, rid: str, state: str, error: str | None = None) -> None:
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state, rng.error_message = RangeState(state), error
    db_session.commit()


def test_an_accepted_provision_is_a_recorded_operation_and_is_sent(client, db_session, no_real_broker):
    rid = _range(client, db_session)
    resp = client.post(f"/ranges/{rid}/provision")
    assert resp.status_code == 202 and resp.json()["state"] == "provisioning"
    op_id = resp.headers["Operation-Id"]
    assert resp.headers["Location"] == f"/ranges/{rid}/operations/{op_id}"
    op = client.get(f"/ranges/{rid}/operations/{op_id}").json()
    assert (op["action"], op["status"], op["generation"], op["dispatch_attempts"]) == ("provision", "dispatched", 1, 1)
    assert no_real_broker.sent == [("worker.tasks.provision_range", [rid])]


def test_a_process_that_died_between_the_commit_and_the_send_loses_nothing(client, db_session, monkeypatch):
    """F19 for ranges: the operation committed, the send never happened."""
    rid = _range(client, db_session)
    monkeypatch.setattr(ops, "dispatch", lambda db, op: None)  # the process dies here
    assert client.post(f"/ranges/{rid}/provision").status_code == 202
    monkeypatch.undo()
    sent = []
    monkeypatch.setattr(celery_client, "dispatch", lambda name, *a: sent.append((name, a)) or "task-1")
    assert ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1
    assert sent == [("provision_range", (rid,))]
    assert ops.redispatch_pending(db_session, min_age=timedelta(0)) == 0, "sent once, not again"


def test_a_failed_commit_accepts_nothing(client, db_session, no_real_broker, monkeypatch):
    rid = _range(client, db_session)

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr("app.routers.ranges._audit", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/ranges/{rid}/provision")
    # (the rollback also undoes this test's own setup: the fixture shares one transaction)
    assert db_session.query(RangeOperation).count() == 0 and no_real_broker.sent == []


def test_the_same_idempotency_key_replays_the_original_operation(client, db_session, no_real_broker):
    rid = _range(client, db_session)
    first = client.post(f"/ranges/{rid}/provision", headers={"Idempotency-Key": "k-1"})
    again = client.post(f"/ranges/{rid}/provision", headers={"Idempotency-Key": "k-1"})
    assert first.status_code == again.status_code == 202
    assert first.headers["Operation-Id"] == again.headers["Operation-Id"]
    assert len(no_real_broker.sent) == 1, "a replay sends nothing"


def test_an_idempotency_key_reused_for_a_different_request_is_409(client, db_session, no_real_broker):
    rid = _range(client, db_session)
    client.post(f"/ranges/{rid}/provision", headers={"Idempotency-Key": "k-2"})
    _set_state(db_session, rid, "ready")
    resp = client.post(f"/ranges/{rid}/destroy", headers={"Idempotency-Key": "k-2"})
    assert resp.status_code == 409 and "Idempotency-Key" in resp.json()["detail"]


def test_an_operation_outcome_comes_from_the_ranges_observed_state(client, db_session, no_real_broker):
    rid = _range(client, db_session)
    op_id = client.post(f"/ranges/{rid}/provision").headers["Operation-Id"]
    assert client.get(f"/ranges/{rid}/operations/{op_id}").json()["status"] == "dispatched"
    _set_state(db_session, rid, "failed", error="vCenter refused the clone")  # the worker reports
    op = client.get(f"/ranges/{rid}/operations/{op_id}").json()
    assert op["status"] == "failed" and op["error"] == {"code": "range_failed", "message": "vCenter refused the clone"}
    assert op["finished_at"]


def test_no_second_operation_while_one_is_in_flight(client, db_session, no_real_broker):
    rid = _range(client, db_session, "ready", VMS)
    client.post(f"/ranges/{rid}/stop")
    _set_state(db_session, rid, "ready")  # e.g. someone moved the state by hand meanwhile
    resp = client.post(f"/ranges/{rid}/stop")
    assert resp.status_code == 409 and "still in progress" in resp.json()["detail"]


def test_a_destroy_supersedes_an_operation_still_in_flight(client, db_session, no_real_broker):
    """The way out of a range whose power task was lost: stopping, nothing behind it."""
    rid = _range(client, db_session, "ready", VMS)
    stop_id = client.post(f"/ranges/{rid}/stop").headers["Operation-Id"]
    resp = client.post(f"/ranges/{rid}/destroy")
    assert resp.status_code == 202 and resp.json()["state"] == "destroying"
    stop = client.get(f"/ranges/{rid}/operations/{stop_id}").json()
    assert stop["status"] == "superseded" and stop["error"]["code"] == "superseded"


def test_a_send_with_no_outcome_is_reported_but_still_blocks(client, db_session, no_real_broker, monkeypatch):
    rid = _range(client, db_session)
    op_id = client.post(f"/ranges/{rid}/provision").headers["Operation-Id"]
    monkeypatch.setattr(ops, "_now", lambda: datetime.now(UTC) + timedelta(hours=7))
    op = client.get(f"/ranges/{rid}/operations/{op_id}").json()
    assert op["status"] == "dispatched" and op["error"]["code"] == "no_outcome"
    rng = db_session.get(Range, uuid.UUID(rid))
    rng.state, rng.provisioner_output = RangeState.running, VMS  # an outcome a provision does not recognise
    db_session.commit()
    resp = client.post(f"/ranges/{rid}/stop")
    assert resp.status_code == 409 and "still in progress" in resp.json()["detail"], "a quiet task still blocks"


def test_an_operator_can_abandon_an_operation_that_will_not_finish(client, db_session, no_real_broker):
    rid = _range(client, db_session)
    op_id = client.post(f"/ranges/{rid}/provision").headers["Operation-Id"]
    op = client.post(f"/ranges/{rid}/operations/{op_id}/abandon").json()
    assert op["status"] == "failed" and op["error"]["code"] == "abandoned"
    rng = client.get(f"/ranges/{rid}").json()
    assert rng["state"] == "failed" and "abandoned" in rng["error_message"]
    assert client.post(f"/ranges/{rid}/operations/{op_id}/abandon").status_code == 409
    assert client.post(f"/ranges/{rid}/provision").status_code == 202, "and the range can be built again"


def test_another_tenants_operations_are_not_found(client, db_session, no_real_broker):
    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app
    from app.models import UserRole

    rid = _range(client, db_session)
    op_id = client.post(f"/ranges/{rid}/provision").headers["Operation-Id"]
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
        assert client.get(f"/ranges/{rid}/operations").status_code == 404
        assert client.get(f"/ranges/{rid}/operations/{op_id}").status_code == 404
        assert client.post(f"/ranges/{rid}/operations/{op_id}/abandon").status_code == 404
        assert client.post(f"/ranges/{rid}/destroy").status_code == 404
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    other_rid = _range(client, db_session)
    assert client.get(f"/ranges/{other_rid}/operations/{op_id}").status_code == 404, "another range's operation"


@pytest.mark.parametrize(
    ("route", "task", "state_after_refusal"),
    [("snapshots", "snapshot_range", "failed"), ("restore", "restore_snapshot", "ready")],
)
def test_a_snapshot_task_is_sent_only_after_its_commit_and_a_refusal_is_undone(
    client, db_session, monkeypatch, route, task, state_after_refusal
):
    """The snapshot routes sent before they committed: the worker could look for a row
    that did not exist yet, and a refused send left the row creating / restoring."""
    rid = _range(client, db_session, "ready", VMS)
    snap = RangeSnapshot(
        range_id=uuid.UUID(rid),
        name="s",
        snapshot_state="ready",
        range_state_at_snapshot="ready",
        tenant_id=db_session.get(Range, uuid.UUID(rid)).tenant_id,
    )
    db_session.add(snap)
    db_session.commit()
    seen = []

    def send(name, *args):
        db_session.expire_all()
        seen.append(
            db_session.query(RangeSnapshot).filter(RangeSnapshot.snapshot_state.in_(("creating", "restoring"))).count()
        )

    monkeypatch.setattr(celery_client, "dispatch", send)  # returns None: the broker refused
    url = f"/ranges/{rid}/snapshots" if route == "snapshots" else f"/ranges/{rid}/snapshots/{snap.id}/restore"
    resp = client.post(url, json={"name": "n"}) if route == "snapshots" else client.post(url)
    assert seen == [1], "the row was committed before the task was sent"
    assert resp.status_code == 503
    db_session.expire_all()
    states = {s.snapshot_state for s in db_session.query(RangeSnapshot).all()}
    assert "creating" not in states and "restoring" not in states and state_after_refusal in states


# ── PostgreSQL ──────────────────────────────────────────────────────────


def _pg_world(postgres_engine):
    from app.models import Template, Tenant
    from sqlalchemy.orm import Session

    with Session(postgres_engine) as s:
        tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        s.flush()
        tmpl = Template(name="t", yaml="id: t\n", tenant_id=tenant.id)
        s.add(tmpl)
        s.flush()
        rng = Range(name="r", template_id=tmpl.id, tenant_id=tenant.id, state=RangeState.created)
        s.add(rng)
        s.commit()
        return rng.id, tenant.id


def _user(tenant_id):
    from app.auth import CurrentUser
    from app.models import UserRole

    return CurrentUser(
        id=str(uuid.uuid4()),
        email="u@example.test",
        display_name="u",
        role=UserRole.admin,
        tenant_id=str(tenant_id),
        keycloak_id="kc-u",
    )


def test_on_postgres_two_concurrent_provisions_record_one_operation(postgres_engine):
    from fastapi import HTTPException
    from sqlalchemy.orm import Session

    rid, tid = _pg_world(postgres_engine)
    barrier, results = threading.Barrier(2), []

    def provision():
        with Session(postgres_engine) as s:
            barrier.wait()
            try:
                ops.accept(s, rid, _user(tid), "provision")
                s.commit()
                results.append("accepted")
            except HTTPException as exc:
                s.rollback()
                results.append(exc.status_code)

    threads = [threading.Thread(target=provision) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(results, key=str) == [409, "accepted"]
    with Session(postgres_engine) as s:
        assert s.query(RangeOperation).count() == 1


def test_on_postgres_two_processes_resending_send_each_operation_once(postgres_engine, monkeypatch):
    from sqlalchemy.orm import Session

    rid, tid = _pg_world(postgres_engine)
    with Session(postgres_engine) as s:
        op, _, _ = ops.accept(s, rid, _user(tid), "provision")
        s.commit()
    sent, gate = [], threading.Event()

    def slow_send(name, *args):
        sent.append(name)
        gate.wait(5)  # hold the row lock while the other process looks
        return "task-1"

    monkeypatch.setattr(celery_client, "dispatch", slow_send)
    counts = []

    def resend():
        with Session(postgres_engine) as s:
            counts.append(ops.redispatch_pending(s, min_age=timedelta(0)))

    first = threading.Thread(target=resend)
    first.start()
    for _ in range(100):
        if sent:
            break
        threading.Event().wait(0.05)
    resend()  # the second process, while the first holds the operation
    gate.set()
    first.join(20)
    assert sent == ["provision_range"] and sorted(counts) == [0, 1]
