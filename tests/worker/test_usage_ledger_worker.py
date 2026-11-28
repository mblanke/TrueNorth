# Author: Marc-Andre Blanke
# Created: 2026-11-28T03:36:45Z
"""The usage ledger in the worker (ADR 0021): every state change ``update_range_state``
makes appends a ``range_state_events`` row in the same transaction; the hourly reconciler
appends what any other path missed."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from worker import db_ops, tables
from worker.usage_reconcile import run_reconcile

TENANT = str(uuid.uuid4())


def _range(db, state: str) -> str:
    rid = str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ranges (id, template_id, state, provisioner_backend, tenant_id) "
                "VALUES (:id, 't', :s, 'mock', :tenant)"
            ),
            {"id": rid, "s": state, "tenant": TENANT},
        )
    return rid


def _events(db, rid: str) -> list[tuple]:
    with db.connect() as conn:
        e = tables.range_state_events
        rows = conn.execute(
            sa.select(e.c.from_state, e.c.to_state, e.c.source, e.c.tenant_id)
            .where(e.c.range_id == rid)
            .order_by(e.c.at)
        ).fetchall()
    return [tuple(r) for r in rows]


def test_a_state_change_appends_one_event_in_the_same_transaction(db):
    rid = _range(db, "ready")
    with db.begin() as conn:
        assert db_ops.update_range_state(conn, rid, "stopping") == 1
        assert db_ops.update_range_state(conn, rid, "stopped", only_from=("stopping",)) == 1
    assert [(f, t, s) for f, t, s, _ in _events(db, rid)] == [
        ("ready", "stopping", "worker"),
        ("stopping", "stopped", "worker"),
    ]
    assert str(_events(db, rid)[0][3]) == TENANT


def test_no_event_when_nothing_changed(db):
    rid = _range(db, "running")
    with db.begin() as conn:
        assert db_ops.update_range_state(conn, rid, "running", output="{}") == 1  # same state: output only
        assert db_ops.update_range_state(conn, rid, "stopped", only_from=("stopping",)) == 0  # guard missed
    assert _events(db, rid) == []


def test_a_rolled_back_change_leaves_no_event(db):
    rid = _range(db, "running")
    try:
        with db.begin() as conn:
            db_ops.update_range_state(conn, rid, "stopping")
            raise RuntimeError("the task failed")
    except RuntimeError:
        pass
    assert _events(db, rid) == []


def _with_deleted_at(db):
    with db.begin() as conn:
        conn.execute(sa.text("ALTER TABLE ranges ADD COLUMN deleted_at TIMESTAMP"))


def test_the_reconciler_records_what_the_ledger_missed(db):
    _with_deleted_at(db)
    older = _range(db, "running")  # from before the ledger: no events
    never_built = _range(db, "created")
    drifted = _range(db, "ready")
    with db.begin() as conn:
        db_ops.update_range_state(conn, drifted, "running")
        conn.execute(sa.text("UPDATE ranges SET state = 'stopped' WHERE id = :id"), {"id": drifted})  # a hand edit
    gone = _range(db, "running")
    with db.begin() as conn:
        db_ops.record_state_event(conn, gone, TENANT, None, "running", source="worker")
        conn.execute(sa.text("UPDATE ranges SET deleted_at = CURRENT_TIMESTAMP WHERE id = :id"), {"id": gone})
    with db.begin() as conn:
        result = run_reconcile(conn)
    assert result["appended"] == 3
    assert _events(db, older)[-1][:3] == (None, "running", "reconcile")
    assert _events(db, never_built) == []
    assert _events(db, drifted)[-1][:3] == ("running", "stopped", "reconcile")
    assert _events(db, gone)[-1][:3] == ("running", "destroyed", "reconcile")
    with db.begin() as conn:
        assert run_reconcile(conn)["appended"] == 0  # nothing left to record
