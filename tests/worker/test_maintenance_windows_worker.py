"""The worker re-checks maintenance windows at a range task's entry (worker/maintenance_windows.py,
ADR 0013 §7, range certification S7).

A task the API accepted before a window opened, or sent late, can reach a worker inside the
window. A build or a restore is then refused, and so is a person's normal destroy; every
other destroy (an emergency, a booking that ended, a lab's end, expiry or failed build) runs.
These run the real tasks against SQLite, with the hypervisor stubbed to fail if touched.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from worker import maintenance_windows as mw
from worker import tables, tasks

REAL_MAINTENANCE_WINDOWS = True  # tests/worker/conftest.py: do not stub the re-check here

TENANT, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
OUTPUT = '{"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]}'
NOW = datetime.now(UTC)


@pytest.fixture
def wdb(db):
    """The worker test database (conftest ``db``) with what the re-check reads."""
    with db.begin() as conn:
        conn.execute(sa.text("ALTER TABLE ranges ADD COLUMN tenant_id TEXT"))
    tables.maintenance_windows.create(db)
    tables.range_operations.create(db)
    return db


@pytest.fixture(autouse=True)
def _no_hypervisor():
    """Any task that gets past the re-check would reach for a backend: make that loud."""
    with patch.object(tasks, "_get_backend", side_effect=AssertionError("the hypervisor was touched")):
        yield


def _range(db, state: str, *, tenant: str = TENANT, tags: list[str] | None = None, output: str | None = None) -> str:
    rid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    yml = "id: t\n" + (f"tags: {json.dumps(tags)}\n" if tags else "")
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO templates VALUES (:id, :y)"), {"id": tid, "y": yml})
        conn.execute(
            sa.text(
                "INSERT INTO ranges (id, template_id, state, provisioner_backend, provisioner_output, tenant_id) "
                "VALUES (:id, :t, :s, 'mock', :o, :tenant)"
            ),
            {"id": rid, "t": tid, "s": state, "o": output, "tenant": tenant},
        )
    return rid


def _window(db, *, tenant: str | None = TENANT, scope: str = "all", range_ids=(), tags=(), start=None, end=None):
    with db.begin() as conn:
        conn.execute(
            tables.maintenance_windows.insert().values(
                id=uuid.uuid4(), tenant_id=tenant, reason="SAN firmware", scope=scope,
                starts_at=start or NOW - timedelta(minutes=10), ends_at=end or NOW + timedelta(hours=1),
                range_ids=list(range_ids), tags=list(tags), created_at=NOW, updated_at=NOW,
            )
        )


def _op(db, rid: str, action: str, *, bypass: str | None = None, status: str = "dispatched", generation: int = 1):
    with db.begin() as conn:
        conn.execute(
            tables.range_operations.insert().values(
                id=uuid.uuid4(), tenant_id=TENANT, range_id=rid, action=action, generation=generation,
                request_hash="h", status=status, dispatch_attempts=1, window_bypass=bypass,
                created_at=NOW, updated_at=NOW,
            )
        )


def _state(db, rid: str) -> tuple:
    with db.connect() as conn:
        return tuple(conn.execute(sa.text("SELECT state, error_message FROM ranges WHERE id = :id"), {"id": rid}).one())


# ── refused ──────────────────────────────────────────────────────────────────
def test_a_build_inside_a_window_is_refused_and_recorded_failed(wdb):
    rid = _range(wdb, "provisioning")
    _op(wdb, rid, "provision")
    _window(wdb)
    result = tasks.provision_range.run(rid)
    assert result["status"] == "refused" and result["reason"] == "maintenance_window"
    state, error = _state(wdb, rid)
    assert state == "failed" and "maintenance window" in error and "SAN firmware" in error


def test_a_lab_build_with_no_operation_row_is_refused_too(wdb):
    rid = _range(wdb, "provisioning")
    _window(wdb)
    assert tasks.provision_range.run(rid)["status"] == "refused"


def test_a_persons_destroy_inside_a_window_is_refused(wdb):
    rid = _range(wdb, "destroying", output=OUTPUT)
    _op(wdb, rid, "destroy")
    _window(wdb)
    assert tasks.destroy_range.run(rid)["status"] == "refused"
    state, error = _state(wdb, rid)
    assert state == "failed" and "Destroying was refused" in error and "Nothing was changed" in error


def test_a_restore_inside_a_window_gives_the_snapshot_back_and_leaves_the_range(wdb):
    rid = _range(wdb, "ready", output=OUTPUT)
    sid = str(uuid.uuid4())
    with wdb.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO range_snapshots (id, range_id, snapshot_state, snapshot_data) VALUES (:i, :r, "
                    "'restoring', '{}')"),
            {"i": sid, "r": rid},
        )
    _window(wdb)
    assert tasks.restore_snapshot.run(rid, sid)["status"] == "refused"
    with wdb.connect() as conn:
        assert conn.execute(sa.text("SELECT snapshot_state FROM range_snapshots WHERE id = :i"), {"i": sid}).scalar() \
            == "ready"
    assert _state(wdb, rid) == ("ready", None)


# ── always runs ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bypass", ["emergency", "booking_ended", "failure_cleanup", "canary"])
def test_a_bypassed_destroy_runs_inside_a_window(wdb, bypass):
    rid = _range(wdb, "destroying", output=OUTPUT)
    _op(wdb, rid, "destroy", bypass=bypass)
    _window(wdb)
    with tables_session(wdb) as s:
        assert mw.refusal(s, "destroy", rid) is None


def test_a_lab_teardown_with_no_operation_row_runs_inside_a_window(wdb):
    rid = _range(wdb, "destroying", output=OUTPUT)
    _op(wdb, rid, "provision", status="succeeded")  # its last operation was something else
    _window(wdb)
    with tables_session(wdb) as s:
        assert mw.refusal(s, "destroy", rid) is None


def test_a_destroy_that_reaches_the_hypervisor_is_the_one_that_bypassed(wdb):
    """End to end through the task: past the re-check, the teardown asks for its backend."""
    rid = _range(wdb, "destroying", output=OUTPUT)
    _op(wdb, rid, "destroy", bypass="emergency")
    _window(wdb)
    with pytest.raises(AssertionError, match="hypervisor was touched"):
        tasks.destroy_range.run(rid)


def test_unreadable_windows_never_block_a_teardown_but_do_block_a_build(wdb):
    with wdb.begin() as conn:
        conn.execute(sa.text("DROP TABLE maintenance_windows"))
    destroy, build = _range(wdb, "destroying", output=OUTPUT), _range(wdb, "provisioning")
    _op(wdb, destroy, "destroy")
    assert mw.at_entry(tasks._db_session, "destroy", destroy) is None
    result = mw.at_entry(tasks._db_session, "provision", build)
    assert result["status"] == "refused" and "could not be read" in _state(wdb, build)[1]


# ── which windows apply ──────────────────────────────────────────────────────
def test_another_tenants_window_does_not_apply(wdb):
    rid = _range(wdb, "provisioning")
    _window(wdb, tenant=OTHER)
    with tables_session(wdb) as s:
        assert mw.refusal(s, "provision", rid) is None


def test_a_platform_wide_window_applies_to_every_tenant(wdb):
    rid = _range(wdb, "provisioning", tenant=OTHER)
    _window(wdb, tenant=None)
    with tables_session(wdb) as s:
        assert "SAN firmware" in mw.refusal(s, "provision", rid)


def test_ranges_and_tags_scopes(wdb):
    listed, tagged, neither = _range(wdb, "provisioning"), _range(wdb, "provisioning", tags=["SOC"]), _range(
        wdb, "provisioning", tags=["red"])
    _window(wdb, scope="ranges", range_ids=[listed])
    _window(wdb, scope="tags", tags=["soc"])
    with tables_session(wdb) as s:
        assert mw.refusal(s, "provision", listed) and mw.refusal(s, "provision", tagged)
        assert mw.refusal(s, "provision", neither) is None


def test_the_window_is_start_inclusive_end_exclusive(wdb):
    rid = _range(wdb, "provisioning")
    start, end = NOW + timedelta(days=1), NOW + timedelta(days=1, hours=2)
    _window(wdb, start=start, end=end)
    with tables_session(wdb) as s:
        assert mw.refusal(s, "provision", rid, now=start - timedelta(microseconds=1)) is None
        assert mw.refusal(s, "provision", rid, now=start)
        assert mw.refusal(s, "provision", rid, now=end - timedelta(microseconds=1))
        assert mw.refusal(s, "provision", rid, now=end) is None


def test_stop_and_start_are_never_checked(wdb):
    rid = _range(wdb, "stopping", output=OUTPUT)
    _window(wdb)
    assert mw.at_entry(tasks._db_session, "stop", rid) is None


def tables_session(db):
    """A session on the test database, as tasks._db_session opens one."""
    return tasks._db_session()
