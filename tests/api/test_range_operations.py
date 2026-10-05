"""Range operations: an accepted provision/destroy is durable, ordered and idempotent.

Before: a request flipped the range's state, committed, then sent the Celery task and
ignored the result. With the broker down the range sat in ``provisioning`` with nothing
behind it and nothing to retry it; two concurrent requests could both send a task.
Now (app/range_ops.py) a 202 means the operation is recorded with the state change in
one transaction; a broker outage leaves it pending and visibly delayed, and it is sent
when the broker is back; the same Idempotency-Key replays the original operation; the
range row is locked while an operation is accepted.

The test suite's broker is unreachable, so ``dispatch`` really returns None unless a
test substitutes a working one.
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
from app.models_range_ops import RangeOperation

OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture
def sent(monkeypatch):
    """A working broker: records each task sent and returns a task id."""
    calls: list[tuple] = []

    def send(task, *args):
        calls.append((task, *args))
        return f"task-{len(calls)}"

    monkeypatch.setattr(celery_client, "dispatch", send)
    return calls


@pytest.fixture
def broker_down(monkeypatch):
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *args: None)


def _range(client) -> dict:
    tmpl = client.post(
        "/templates",
        json={
            "name": f"T {uuid.uuid4().hex[:6]}",
            "version": "1.0",
            "yaml": "id: test\nnodes:\n  - name: dc1",
            "is_public": True,
        },
    )
    assert tmpl.status_code in (200, 201), tmpl.text
    rng = client.post("/ranges", json={"name": f"R {uuid.uuid4().hex[:6]}", "template_id": tmpl.json()["id"]})
    assert rng.status_code in (200, 201), rng.text
    return rng.json()


def _ops(client, range_id) -> list[dict]:
    r = client.get(f"/ranges/{range_id}/operations")
    assert r.status_code == 200, r.text
    return r.json()


def test_an_accepted_provision_is_a_recorded_operation_and_is_sent(client, sent):
    rng = _range(client)
    r = client.post(f"/ranges/{rng['id']}/provision")
    assert r.status_code == 202
    assert r.json()["state"] == "provisioning"
    [op] = _ops(client, rng["id"])
    assert r.headers["Operation-Id"] == op["id"]
    assert r.headers["Location"] == f"/ranges/{rng['id']}/operations/{op['id']}"
    assert (op["action"], op["generation"], op["status"], op["task_id"]) == ("provision", 1, "dispatched", "task-1")
    assert sent == [("provision_range", rng["id"])]


def test_with_the_broker_down_the_operation_waits_visibly_and_is_sent_later(
    client, db_session, broker_down, monkeypatch
):
    rng = _range(client)
    r = client.post(f"/ranges/{rng['id']}/provision")
    assert r.status_code == 202, "accepted: the operation is recorded even though the broker is down"
    [op] = _ops(client, rng["id"])
    assert op["status"] == "pending" and op["error"]["code"] == "broker_unavailable"
    assert op["dispatch_attempts"] == 1

    sent: list = []
    monkeypatch.setattr(celery_client, "dispatch", lambda task, *a: sent.append((task, *a)) or "task-late")
    assert range_ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1
    [op] = _ops(client, rng["id"])
    assert (op["status"], op["task_id"], op["dispatch_attempts"], op["error"]) == ("dispatched", "task-late", 2, None)
    assert sent == [("provision_range", rng["id"])]
    assert range_ops.redispatch_pending(db_session, min_age=timedelta(0)) == 0, "sent once, not again"


def test_a_failed_commit_accepts_nothing(client, db_session, sent, monkeypatch):
    rng = _range(client)

    def broken_audit(*a, **k):
        raise RuntimeError("database went away")

    from app.routers import ranges

    monkeypatch.setattr(ranges, "_audit", broken_audit)
    with pytest.raises(RuntimeError):
        client.post(f"/ranges/{rng['id']}/provision")
    monkeypatch.undo()
    # The handler rolled back. (In this suite that also rolls back the test's own setup,
    # so the range row is gone too; in production only the request's changes are.)
    assert db_session.query(RangeOperation).count() == 0
    assert db_session.query(Range).filter(Range.state == RangeState.provisioning).count() == 0
    assert sent == [], "nothing is sent for an operation that was not accepted"


def test_the_same_idempotency_key_replays_the_original_operation(client, sent):
    rng = _range(client)
    first = client.post(f"/ranges/{rng['id']}/provision", headers={"Idempotency-Key": "click-1"})
    again = client.post(f"/ranges/{rng['id']}/provision", headers={"Idempotency-Key": "click-1"})
    assert (first.status_code, again.status_code) == (202, 202)
    assert first.headers["Operation-Id"] == again.headers["Operation-Id"]
    assert len(_ops(client, rng["id"])) == 1
    assert len(sent) == 1, "a replay must not send the task again"


def test_an_idempotency_key_reused_for_a_different_request_is_409(client, db_session, sent):
    rng = _range(client)
    client.post(f"/ranges/{rng['id']}/provision", headers={"Idempotency-Key": "k"})
    db_session.get(Range, uuid.UUID(rng["id"])).state = RangeState.ready  # the worker finished
    db_session.flush()
    clash = client.post(f"/ranges/{rng['id']}/destroy", headers={"Idempotency-Key": "k"})
    assert clash.status_code == 409 and "Idempotency-Key" in clash.json()["detail"]
    assert len(sent) == 1


def test_an_operation_outcome_comes_from_the_ranges_observed_state(client, db_session, sent):
    rng = _range(client)
    client.post(f"/ranges/{rng['id']}/provision")
    [op] = _ops(client, rng["id"])
    assert op["status"] == "dispatched", "not done until the worker says so"
    row = db_session.get(Range, uuid.UUID(rng["id"]))
    row.state, row.error_message = RangeState.failed, "vCenter refused the clone"
    db_session.flush()
    [op] = _ops(client, rng["id"])
    assert op["status"] == "failed" and op["finished_at"]
    assert op["error"] == {"code": "range_failed", "message": "vCenter refused the clone"}

    retry = client.post(f"/ranges/{rng['id']}/provision")
    assert retry.status_code == 202
    ops = _ops(client, rng["id"])
    assert [o["generation"] for o in ops] == [2, 1]


def test_no_second_operation_while_one_is_in_flight(client, db_session, broker_down):
    """A provision still waiting for the broker blocks a destroy, even if the state machine would allow it."""
    rng = _range(client)
    client.post(f"/ranges/{rng['id']}/provision")  # pending: the broker is down
    db_session.get(Range, uuid.UUID(rng["id"])).state = RangeState.failed  # destroy is a legal transition from here
    db_session.flush()
    r = client.post(f"/ranges/{rng['id']}/destroy")
    assert r.status_code == 409 and "still in progress" in r.json()["detail"]
    assert [o["action"] for o in _ops(client, rng["id"])] == ["provision"]


@contextmanager
def acting_as(role: UserRole, tenant: str):
    who = CurrentUser(
        id=str(uuid.uuid4()), email="x@example.test", display_name="x", role=role, tenant_id=tenant, keycloak_id="kc-x"
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_another_tenant_cannot_see_or_start_operations(client, sent):
    rng = _range(client)
    r = client.post(f"/ranges/{rng['id']}/provision")
    op_id = r.headers["Operation-Id"]
    with acting_as(UserRole.admin, OTHER_TENANT):
        assert client.get(f"/ranges/{rng['id']}/operations").status_code == 404
        assert client.get(f"/ranges/{rng['id']}/operations/{op_id}").status_code == 404
        assert client.post(f"/ranges/{rng['id']}/destroy").status_code == 404
    assert len(sent) == 1


def test_on_postgres_two_concurrent_provisions_send_one_task():
    """Real row locking: the second request waits for the first, then is refused.

    Runs against a scratch database when TEST_POSTGRES_ADMIN_URL is set; skipped otherwise.
    """
    import os
    import threading

    import sqlalchemy as sa
    from app.models import Template, Tenant
    from app.sections import Base
    from sqlalchemy.orm import sessionmaker

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    name = f"tn_ops_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(sa.engine.make_url(admin_url).set(database=name), pool_size=10)
    try:
        Base.metadata.create_all(engine)
        make = sessionmaker(engine, expire_on_commit=False)
        tenant = uuid.uuid4()
        with make() as s:
            s.add(Tenant(id=tenant, name=f"t-{tenant.hex[:6]}", slug=f"t-{tenant.hex[:6]}"))
            s.flush()
            tmpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\nnodes: []", tenant_id=tenant)
            s.add(tmpl)
            s.flush()
            rng = Range(id=uuid.uuid4(), name="r", template_id=tmpl.id, tenant_id=tenant, state=RangeState.created)
            s.add(rng)
            s.commit()
        user = CurrentUser(
            id=str(uuid.uuid4()),
            email="a@x",
            display_name="a",
            role=UserRole.admin,
            tenant_id=str(tenant),
            keycloak_id="kc",
        )
        a, b = make(), make()
        op_a, _, _ = range_ops.accept(a, rng.id, user, "provision")  # A holds the range row lock
        outcome: dict = {}

        def b_requests():
            try:
                range_ops.accept(b, rng.id, user, "provision")
                outcome["b"] = "accepted"
            except Exception as exc:  # HTTPException 409
                outcome["b"] = getattr(exc, "status_code", repr(exc))

        t = threading.Thread(target=b_requests)
        t.start()
        t.join(0.5)
        assert "b" not in outcome, "B did not wait for A's lock on the range"
        a.commit()
        t.join(10)
        assert outcome["b"] == 409
        b.rollback()
        with make() as s:
            assert s.query(RangeOperation).count() == 1
        a.close()
        b.close()
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
