"""The worker re-checks maintenance windows at a range task's entry (worker/maintenance_windows.py,
ADR 0013 §7, range certification S7).

A task the API accepted before a window opened, or sent late, can reach a worker inside the
window. Builds and restores are then held, never failed: a recorded operation goes back to
``pending`` (the API re-sends it after the window), a lab's build puts its range back to
``created`` (its session re-sends it). A destroy runs only when identified as one that must:
a bypass, accepted before the window began, or a Student lab's cleanup; anything else is held.
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
        conn.execute(sa.text("CREATE TABLE lab_sessions (id TEXT PRIMARY KEY, range_id TEXT, state TEXT, "
                             "end_reason TEXT)"))
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


def _window(db, *, tenant: str | None = TENANT, scope: str = "all", range_ids=(), tags=(), start=None, end=None,
            reason: str = "SAN firmware"):
    with db.begin() as conn:
        conn.execute(
            tables.maintenance_windows.insert().values(
                id=uuid.uuid4(), tenant_id=tenant, reason=reason, scope=scope,
                starts_at=start or NOW - timedelta(minutes=10), ends_at=end or NOW + timedelta(hours=1),
                range_ids=list(range_ids), tags=list(tags), created_at=NOW, updated_at=NOW,
            )
        )


def _op(db, rid: str, action: str, *, bypass: str | None = None, status: str = "dispatched",
        created: datetime | None = None) -> uuid.UUID:
    oid = uuid.uuid4()
    with db.begin() as conn:
        conn.execute(
            tables.range_operations.insert().values(
                id=oid, tenant_id=TENANT, range_id=rid, action=action, generation=1,
                request_hash="h", status=status, dispatch_attempts=1, window_bypass=bypass,
                created_at=created or NOW, updated_at=NOW,
            )
        )
    return oid


def _op_row(db, oid: uuid.UUID):
    with db.connect() as conn:
        return conn.execute(
            sa.select(tables.range_operations.c.status, tables.range_operations.c.error)
            .where(tables.range_operations.c.id == oid)
        ).one()


def _state(db, rid: str) -> tuple:
    with db.connect() as conn:
        return tuple(conn.execute(sa.text("SELECT state, error_message FROM ranges WHERE id = :id"), {"id": rid}).one())


def _session():
    return tasks._db_session()


# ── held, never failed ───────────────────────────────────────────────────────
def test_a_recorded_build_inside_a_window_is_held_pending_not_failed(wdb):
    rid = _range(wdb, "provisioning")
    oid = _op(wdb, rid, "provision")
    _window(wdb)
    result = tasks.provision_range.run(rid)
    assert result["status"] == "held" and result["reason"] == "maintenance_window"
    assert _state(wdb, rid) == ("provisioning", None), "not failed; still recorded as being built"
    status, error = _op_row(wdb, oid)
    assert status == "pending" and error["code"] == "maintenance_window" and "SAN firmware" in error["message"]


def test_a_lab_build_inside_a_window_goes_back_to_created(wdb):
    rid = _range(wdb, "provisioning")
    _window(wdb)
    assert tasks.provision_range.run(rid)["status"] == "held"
    state, error = _state(wdb, rid)
    assert state == "created" and "maintenance window" in error


def test_a_persons_destroy_accepted_inside_the_window_is_held_pending(wdb):
    rid = _range(wdb, "destroying", output=OUTPUT)
    oid = _op(wdb, rid, "destroy")  # accepted after the window began (a race with the API's check)
    _window(wdb)
    assert tasks.destroy_range.run(rid)["status"] == "held"
    assert _state(wdb, rid) == ("destroying", None)
    assert _op_row(wdb, oid)[0] == "pending"


def test_a_persons_destroy_accepted_before_the_window_began_runs(wdb):
    rid = _range(wdb, "destroying", output=OUTPUT)
    _op(wdb, rid, "destroy", created=NOW - timedelta(hours=1))
    _window(wdb)  # began 10 minutes ago
    with pytest.raises(AssertionError, match="hypervisor was touched"):
        tasks.destroy_range.run(rid)


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
    assert tasks.restore_snapshot.run(rid, sid)["status"] == "held"
    with wdb.connect() as conn:
        assert conn.execute(sa.text("SELECT snapshot_state FROM range_snapshots WHERE id = :i"), {"i": sid}).scalar() \
            == "ready"
    assert _state(wdb, rid) == ("ready", None)


def test_a_destroy_nothing_identifies_is_held_not_run_and_not_failed(wdb):
    """Default deny: no operation row, not a lab's range."""
    rid = _range(wdb, "destroying", output=OUTPUT)
    _window(wdb)
    assert tasks.destroy_range.run(rid)["status"] == "held"
    state, error = _state(wdb, rid)
    assert state == "destroying" and "maintenance window" in error


# ── always runs ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bypass", ["emergency", "booking_ended", "failure_cleanup", "canary"])
def test_a_bypassed_destroy_runs_inside_a_window(wdb, bypass):
    rid = _range(wdb, "destroying", output=OUTPUT)
    _op(wdb, rid, "destroy", bypass=bypass)
    _window(wdb)
    with pytest.raises(AssertionError, match="hypervisor was touched"):
        tasks.destroy_range.run(rid)


def test_an_unknown_bypass_marker_does_not_count(wdb):
    rid = _range(wdb, "destroying", output=OUTPUT)
    _op(wdb, rid, "destroy", bypass="because")
    _window(wdb)
    with _session() as s:
        assert mw.decide(s, "destroy", rid)[0]


@pytest.mark.parametrize("state, reason", [("expired", "expired"), ("cleaning", "failed"), ("completed", "completed")])
def test_a_lab_cleanup_runs_inside_a_window(wdb, state, reason):
    rid = _range(wdb, "destroying", output=OUTPUT)
    with wdb.begin() as conn:
        conn.execute(sa.text("INSERT INTO lab_sessions VALUES (:i, :r, :s, :e)"),
                     {"i": str(uuid.uuid4()), "r": rid, "s": state, "e": reason})
    _window(wdb)
    with pytest.raises(AssertionError, match="hypervisor was touched"):
        tasks.destroy_range.run(rid)


def test_unreadable_windows_never_block_a_teardown_but_do_hold_a_build(wdb):
    with wdb.begin() as conn:
        conn.execute(sa.text("DROP TABLE maintenance_windows"))
    destroy, build = _range(wdb, "destroying", output=OUTPUT), _range(wdb, "provisioning")
    _op(wdb, destroy, "destroy")
    oid = _op(wdb, build, "provision")
    assert mw.at_entry(tasks._db_session, "destroy", destroy) is None
    result = mw.at_entry(tasks._db_session, "provision", build)
    assert result["status"] == "held" and "could not be read" in result["detail"]
    assert _state(wdb, build)[0] == "provisioning" and _op_row(wdb, oid)[0] == "pending"


# ── which windows apply ──────────────────────────────────────────────────────
def test_another_tenants_window_does_not_apply(wdb):
    rid = _range(wdb, "provisioning")
    _window(wdb, tenant=OTHER)
    with _session() as s:
        assert mw.decide(s, "provision", rid)[0] is None


def test_a_platform_wide_window_applies_to_every_tenant(wdb):
    rid = _range(wdb, "provisioning", tenant=OTHER)
    _window(wdb, tenant=None)
    with _session() as s:
        assert "SAN firmware" in mw.decide(s, "provision", rid)[0]


def test_many_windows_never_crowd_out_the_one_that_applies(wdb):
    """Review of #261: a LIMIT before scope matching let a tenant hide a platform window."""
    rid = _range(wdb, "provisioning")
    decoy = _range(wdb, "created")
    for _ in range(250):  # this tenant's own, for another range, ending later than the platform's
        _window(wdb, scope="ranges", range_ids=[decoy], end=NOW + timedelta(days=3650), reason="decoy")
    for _ in range(250):  # another tenant's, covering everything of theirs
        _window(wdb, tenant=OTHER, end=NOW + timedelta(days=3650), reason="theirs")
    _window(wdb, tenant=None, reason="platform vCenter upgrade")
    with _session() as s:
        assert "platform vCenter upgrade" in mw.decide(s, "provision", rid)[0]


def test_ranges_and_tags_scopes(wdb):
    listed, tagged, neither = _range(wdb, "provisioning"), _range(wdb, "provisioning", tags=["SOC"]), _range(
        wdb, "provisioning", tags=["red"])
    _window(wdb, scope="ranges", range_ids=[listed])
    _window(wdb, scope="tags", tags=["soc"])
    with _session() as s:
        assert mw.decide(s, "provision", listed)[0] and mw.decide(s, "provision", tagged)[0]
        assert mw.decide(s, "provision", neither)[0] is None


def test_the_window_is_start_inclusive_end_exclusive(wdb):
    rid = _range(wdb, "provisioning")
    start, end = NOW + timedelta(days=1), NOW + timedelta(days=1, hours=2)
    _window(wdb, start=start, end=end)
    with _session() as s:
        assert mw.decide(s, "provision", rid, now=start - timedelta(microseconds=1))[0] is None
        assert mw.decide(s, "provision", rid, now=start)[0]
        assert mw.decide(s, "provision", rid, now=end - timedelta(microseconds=1))[0]
        assert mw.decide(s, "provision", rid, now=end)[0] is None


def test_stop_and_start_are_never_checked(wdb):
    rid = _range(wdb, "stopping", output=OUTPUT)
    _window(wdb)
    assert mw.at_entry(tasks._db_session, "stop", rid) is None


def test_template_yaml_with_aliases_declares_no_tags():
    assert mw.template_tags("base: &b [soc]\ntags: *b\n") == set()
