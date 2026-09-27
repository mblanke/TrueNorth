"""Range snapshot, restore and delete: backends and worker tasks.

Until this file, restore could not succeed anywhere. worker.tasks called
provisioner.restore() and provisioner.delete_snapshot(), which no backend defined.
The vSphere backend snapshotted through a REST path vCenter does not have. And
snapshot_range stored failed snapshots as `ready`. These tests pin each of those.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("MOCK_PROVISION_DELAY", "0")
os.environ.setdefault("MOCK_FAILURE_RATE", "0")

from worker.provisioners import MockProvisioner, TerraformProvisioner
from worker.provisioners.results import RestoreResult, SnapshotDeleteResult, SnapshotResult, outcome


def _run(coro):
    return asyncio.run(coro)


# -----------------------------------------------------------------------
# Shared contract
# -----------------------------------------------------------------------


class TestContract:
    def test_outcome(self):
        assert outcome(3, []) == "ok"
        assert outcome(1, ["x"]) == "partial"
        assert outcome(0, ["x"]) == "failed"

    def test_backend_without_support_fails_instead_of_raising(self):
        """No more AttributeError: an unsupporting backend returns a failed result."""
        prov = TerraformProvisioner()
        restored = _run(prov.restore("r1", {"vms": [{"name": "a"}]}, "tnx", power_on=True))
        deleted = _run(prov.delete_snapshot("r1", {"vms": [{"name": "a"}]}, "tnx"))
        assert isinstance(restored, RestoreResult) and restored.status == "failed"
        assert "does not support snapshot restore" in restored.errors[0]
        assert isinstance(deleted, SnapshotDeleteResult) and deleted.status == "failed"

    def test_terraform_snapshot_no_longer_claims_success(self):
        result = _run(TerraformProvisioner().snapshot("r1", {"vms": [{"name": "a"}]}, "tnx"))
        assert result.status == "failed"


class TestMock:
    PROV = {"vms": [{"name": "a"}, {"name": "b"}]}

    def test_fresh_instance_uses_stored_output(self):
        """Each worker task gets a new provisioner, so memory is empty; use the DB copy."""
        prov = MockProvisioner()
        assert _run(prov.snapshot("r1", self.PROV, "tnx")).vms_snapped == 2
        restored = _run(prov.restore("r1", self.PROV, "tnx", power_on=True))
        assert restored.status == "ok" and restored.vms_restored == 2
        deleted = _run(prov.delete_snapshot("r1", self.PROV, "tnx"))
        assert deleted.status == "ok" and deleted.vms_cleaned == 2


# -----------------------------------------------------------------------
# vSphere (pyVmomi)
# -----------------------------------------------------------------------


class FakeTask:
    def __init__(self, label: str, fail: str | None = None):
        self.label = label
        self.fail = fail


class FakeSnap:
    def __init__(self, name: str, age_minutes: int, children=()):
        self.name = name
        self.createTime = datetime(2026, 1, 1) + timedelta(minutes=age_minutes)
        self.childSnapshotList = list(children)
        self.snapshot = MagicMock()
        self.snapshot.RevertToSnapshot_Task.return_value = FakeTask(f"revert:{name}:{age_minutes}")
        self.snapshot.RemoveSnapshot_Task.return_value = FakeTask(f"remove:{name}:{age_minutes}")


class FakeVM:
    def __init__(self, vm_id: str, roots=(), power: str = "poweredOff", fail_create: str | None = None):
        self.snapshot = SimpleNamespace(rootSnapshotList=list(roots)) if roots else None
        self.runtime = SimpleNamespace(powerState=power)
        self.CreateSnapshot_Task = MagicMock(return_value=FakeTask(f"create:{vm_id}", fail_create))
        self.PowerOnVM_Task = MagicMock(return_value=FakeTask(f"poweron:{vm_id}"))


@pytest.fixture
def vsphere(monkeypatch):
    """A VsphereAPIProvisioner whose pyVmomi calls land on FakeVMs in ``vms``."""
    import worker.provisioners.vsphere_api as mod

    vms: dict[str, FakeVM] = {}
    waited: list[str] = []

    def wait(task, si=None):
        waited.append(task.label)
        if task.fail:
            raise RuntimeError(task.fail)

    connect = MagicMock(return_value=SimpleNamespace(_stub=object()))
    monkeypatch.setattr(mod, "SmartConnect", connect)
    monkeypatch.setattr(mod, "Disconnect", MagicMock())
    monkeypatch.setattr(mod, "WaitForTask", wait)
    monkeypatch.setattr(mod, "vim", SimpleNamespace(VirtualMachine=lambda vm_id, stub: vms[vm_id]))
    return SimpleNamespace(prov=mod.VsphereAPIProvisioner(), vms=vms, waited=waited, connect=connect, mod=mod)


def _prov_output(*vm_ids):
    return {"vms": [{"name": f"n-{v}", "vm_id": v} for v in vm_ids]}


class TestVsphereSnapshots:
    def test_snapshot_goes_through_pyvmomi_not_rest(self, vsphere):
        vsphere.vms.update({"vm-1": FakeVM("vm-1"), "vm-2": FakeVM("vm-2")})
        result = _run(vsphere.prov.snapshot("r1", _prov_output("vm-1", "vm-2"), "tnabc"))

        assert result.status == "ok" and result.vms_snapped == 2
        vsphere.vms["vm-1"].CreateSnapshot_Task.assert_called_once_with(
            name="tnabc", description="TrueNorth range snapshot", memory=False, quiesce=False
        )
        assert sorted(vsphere.waited) == ["create:vm-1", "create:vm-2"]
        vsphere.mod.Disconnect.assert_called_once()

    def test_one_vm_failing_makes_the_snapshot_partial(self, vsphere):
        vsphere.vms.update({"vm-1": FakeVM("vm-1"), "vm-2": FakeVM("vm-2", fail_create="disk full")})
        result = _run(vsphere.prov.snapshot("r1", _prov_output("vm-1", "vm-2"), "tnabc"))
        assert result.status == "partial" and result.vms_snapped == 1
        assert result.errors == ["VM vm-2: disk full"]

    def test_vm_without_an_id_is_an_error_not_a_skip(self, vsphere):
        vsphere.vms["vm-1"] = FakeVM("vm-1")
        output = {"vms": [{"name": "a", "vm_id": "vm-1"}, {"name": "b"}]}
        result = _run(vsphere.prov.snapshot("r1", output, "tnabc"))
        assert result.status == "partial"
        assert result.errors == ["VM b: no vm_id recorded"]

    def test_restore_reverts_to_newest_matching_snapshot_anywhere_in_tree(self, vsphere):
        old, new = FakeSnap("tnabc", 1), FakeSnap("tnabc", 5)
        tree = FakeSnap("base", 0, children=[old, FakeSnap("other", 2, children=[new])])
        vsphere.vms["vm-1"] = FakeVM("vm-1", roots=[tree])

        result = _run(vsphere.prov.restore("r1", _prov_output("vm-1"), "tnabc", power_on=False))

        assert result.status == "ok" and result.vms_restored == 1
        new.snapshot.RevertToSnapshot_Task.assert_called_once_with()
        old.snapshot.RevertToSnapshot_Task.assert_not_called()
        vsphere.vms["vm-1"].PowerOnVM_Task.assert_not_called()

    def test_restore_powers_on_only_what_is_off(self, vsphere):
        vsphere.vms.update(
            {
                "vm-1": FakeVM("vm-1", roots=[FakeSnap("tnabc", 1)], power="poweredOff"),
                "vm-2": FakeVM("vm-2", roots=[FakeSnap("tnabc", 1)], power="poweredOn"),
            }
        )
        result = _run(vsphere.prov.restore("r1", _prov_output("vm-1", "vm-2"), "tnabc", power_on=True))

        assert result.status == "ok"
        vsphere.vms["vm-1"].PowerOnVM_Task.assert_called_once_with()
        vsphere.vms["vm-2"].PowerOnVM_Task.assert_not_called()

    def test_restore_fails_when_a_vm_lacks_the_snapshot(self, vsphere):
        vsphere.vms["vm-1"] = FakeVM("vm-1", roots=[FakeSnap("something-else", 1)])
        result = _run(vsphere.prov.restore("r1", _prov_output("vm-1"), "tnabc", power_on=True))
        assert result.status == "failed"
        assert "no snapshot named 'tnabc'" in result.errors[0]

    def test_delete_removes_every_copy_and_tolerates_vms_without_it(self, vsphere):
        a, b = FakeSnap("tnabc", 1), FakeSnap("tnabc", 3)
        vsphere.vms.update({"vm-1": FakeVM("vm-1", roots=[a, b]), "vm-2": FakeVM("vm-2")})

        result = _run(vsphere.prov.delete_snapshot("r1", _prov_output("vm-1", "vm-2"), "tnabc"))

        assert result.status == "ok" and result.vms_cleaned == 2
        a.snapshot.RemoveSnapshot_Task.assert_called_once_with(removeChildren=False)
        b.snapshot.RemoveSnapshot_Task.assert_called_once_with(removeChildren=False)

    def test_connection_failure_fails_every_vm(self, vsphere):
        vsphere.connect.side_effect = OSError("vcenter unreachable")
        result = _run(vsphere.prov.snapshot("r1", _prov_output("vm-1", "vm-2"), "tnabc"))
        assert result.status == "failed" and result.vms_snapped == 0
        assert all("vcenter unreachable" in e for e in result.errors)

    def test_missing_pyvmomi_is_reported(self, vsphere, monkeypatch):
        monkeypatch.setattr(vsphere.mod, "SmartConnect", None)
        result = _run(vsphere.prov.restore("r1", _prov_output("vm-1"), "tnabc", power_on=True))
        assert result.status == "failed"
        assert "pyvmomi is required" in result.errors[0]


# -----------------------------------------------------------------------
# Proxmox
# -----------------------------------------------------------------------


class FakeProxmox:
    """An in-memory Proxmox API behind httpx.MockTransport."""

    def __init__(self, snapshots: dict[int, set[str]], power: dict[int, str], exitstatus: str = "OK"):
        self.snapshots = snapshots
        self.power = power
        self.exitstatus = exitstatus
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json/nodes/pve")
        self.calls.append(f"{request.method} {path}")
        if "/tasks/" in path:
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": self.exitstatus}})
        vmid = int(re.search(r"/qemu/(\d+)", path).group(1))
        if path.endswith("/status/current"):
            return httpx.Response(200, json={"data": {"status": self.power[vmid]}})
        if request.method == "GET" and path.endswith("/snapshot"):
            names = [{"name": n} for n in self.snapshots.get(vmid, set())] + [{"name": "current"}]
            return httpx.Response(200, json={"data": names})
        return httpx.Response(200, json={"data": f"UPID:pve:{vmid}:{request.method}"})


@pytest.fixture
def proxmox():
    from worker.provisioners.proxmox_api import ProxmoxAPIProvisioner

    def make(fake: FakeProxmox) -> ProxmoxAPIProvisioner:
        prov = ProxmoxAPIProvisioner()
        prov._node = "pve"
        prov._client = lambda: httpx.AsyncClient(
            base_url="https://pve.test", transport=httpx.MockTransport(fake.handler)
        )
        return prov

    return make


class TestProxmoxSnapshots:
    def test_snapshot_waits_for_the_task_and_checks_it(self, proxmox):
        """The POST only queues it; a task that ends in error is a failed snapshot."""
        fake = FakeProxmox({}, {}, exitstatus="snapshot feature is not available")
        result = _run(proxmox(fake).snapshot("r1", {"vms": [{"vmid": 101}]}, "tnabc"))
        assert result.status == "failed"
        assert "snapshot feature is not available" in result.errors[0]
        assert any("/tasks/" in c for c in fake.calls)

    def test_restore_rolls_back_then_starts_only_stopped_vms(self, proxmox):
        fake = FakeProxmox({101: {"tnabc"}, 102: {"tnabc"}}, {101: "running", 102: "stopped"})
        result = _run(proxmox(fake).restore("r1", {"vms": [{"vmid": 101}, {"vmid": 102}]}, "tnabc", power_on=True))

        assert result.status == "ok" and result.vms_restored == 2
        assert "POST /qemu/101/snapshot/tnabc/rollback" in fake.calls
        assert "POST /qemu/102/status/start" in fake.calls
        assert "POST /qemu/101/status/start" not in fake.calls  # already running: would error

    def test_delete_skips_vms_that_no_longer_have_it(self, proxmox):
        fake = FakeProxmox({101: {"tnabc"}, 102: set()}, {})
        result = _run(proxmox(fake).delete_snapshot("r1", {"vms": [{"vmid": 101}, {"vmid": 102}]}, "tnabc"))

        assert result.status == "ok" and result.vms_cleaned == 2
        assert "DELETE /qemu/101/snapshot/tnabc" in fake.calls
        assert "DELETE /qemu/102/snapshot/tnabc" not in fake.calls

    def test_vm_without_vmid_is_an_error(self, proxmox):
        fake = FakeProxmox({101: {"tnabc"}}, {101: "running"})
        result = _run(proxmox(fake).restore("r1", {"vms": [{"vmid": 101}, {"name": "x"}]}, "tnabc", power_on=False))
        assert result.status == "partial"
        assert "no vmid recorded" in result.errors[0]


# -----------------------------------------------------------------------
# Hyper-V
# -----------------------------------------------------------------------


@pytest.fixture
def hyperv(monkeypatch):
    import worker.provisioners.hyperv as mod

    monkeypatch.setenv("HYPERV_HOST", "hyperv.test")
    monkeypatch.setenv("HYPERV_USERNAME", "DOMAIN\\admin")
    monkeypatch.setenv("HYPERV_PASSWORD", "secret")

    def make(status_code: int = 0):
        session = MagicMock()
        session.run_ps.return_value = SimpleNamespace(status_code=status_code, std_out=b"", std_err=b"boom")
        winrm = MagicMock()
        winrm.Session.return_value = session
        monkeypatch.setattr(mod, "winrm", winrm)
        return mod.HypervProvisioner(), session

    return make


class TestHypervSnapshots:
    OUTPUT = {"vms": [{"name": "dc01"}]}

    def test_restore_reverts_and_starts(self, hyperv):
        prov, session = hyperv()
        result = _run(prov.restore("r1", self.OUTPUT, "tnabc", power_on=True))

        assert result.status == "ok" and result.vms_restored == 1
        script = session.run_ps.call_args[0][0]
        assert "Restore-VMCheckpoint -VMName 'r1-dc01' -Name 'tnabc' -Confirm:$false" in script
        assert "if ($true -and" in script

    def test_restore_error_is_reported(self, hyperv):
        prov, _ = hyperv(status_code=1)
        result = _run(prov.restore("r1", self.OUTPUT, "tnabc", power_on=False))
        assert result.status == "failed"
        assert "Restore failed for 'r1-dc01'" in result.errors[0]

    def test_delete_tolerates_a_missing_checkpoint(self, hyperv):
        prov, session = hyperv()
        result = _run(prov.delete_snapshot("r1", self.OUTPUT, "tnabc"))
        assert result.status == "ok"
        script = session.run_ps.call_args[0][0]
        assert "Get-VMCheckpoint -VMName 'r1-dc01' -Name 'tnabc' -ErrorAction SilentlyContinue" in script
        assert "Remove-VMCheckpoint" in script


# -----------------------------------------------------------------------
# Worker tasks
# -----------------------------------------------------------------------

tasks = pytest.importorskip("worker.tasks")

SNAP_ID = "3f2b9c1e-0d4a-4c8e-9b7a-1e2f3a4b5c6d"  # starts with a digit, as Proxmox forbids
PROV = {"vms": [{"name": "a", "vm_id": "vm-1"}, {"name": "b", "vm_id": "vm-2"}]}


def _db(*rows):
    """Stand-in for _db_session() whose successive execute().first() calls return ``rows``."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    pending = iter(rows)
    session.execute.side_effect = lambda *a, **k: MagicMock(first=MagicMock(return_value=next(pending)))
    return MagicMock(return_value=session)


@pytest.fixture
def backend():
    prov = MagicMock()
    prov.snapshot = AsyncMock(return_value=SnapshotResult(status="ok", vms_snapped=2))
    prov.restore = AsyncMock(return_value=RestoreResult(status="ok", vms_restored=2))
    prov.delete_snapshot = AsyncMock(return_value=SnapshotDeleteResult(status="ok", vms_cleaned=2))
    return prov


@pytest.fixture
def spies(backend):
    with (
        patch.object(tasks, "_get_backend", return_value=backend),
        patch.object(tasks, "_update_range_state") as range_state,
        patch.object(tasks, "_update_snapshot_state") as snapshot_state,
        patch.object(tasks, "_notify_api"),
    ):
        yield SimpleNamespace(range_state=range_state, snapshot_state=snapshot_state)


class TestBackendSnapshotName:
    def test_valid_proxmox_snapname_from_a_uuid(self):
        name = tasks._backend_snapshot_name(SNAP_ID)
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name)
        assert len(name) <= 40
        assert name == "tn" + SNAP_ID.replace("-", "")

    def test_distinct_ids_give_distinct_names(self):
        a = tasks._backend_snapshot_name("3f2b9c1e-0d4a-4c8e-9b7a-1e2f3a4b5c6d")
        b = tasks._backend_snapshot_name("3f2b9c1e-0d4a-4c8e-9b7a-1e2f3a4b5c6e")
        assert a != b


class TestSnapshotRangeTask:
    def test_records_the_backend_name(self, backend, spies):
        with patch.object(tasks, "_db_session", _db((json.dumps(PROV),))):
            assert tasks.snapshot_range(range_id="r1", snapshot_id=SNAP_ID)["status"] == "ready"

        name = tasks._backend_snapshot_name(SNAP_ID)
        backend.snapshot.assert_awaited_once_with("r1", PROV, name)
        state, kwargs = spies.snapshot_state.call_args[0][1], spies.snapshot_state.call_args[1]
        assert state == "ready"
        assert json.loads(kwargs["data"])["snapshot_name"] == name

    def test_partial_snapshot_is_failed_and_discarded(self, backend, spies):
        backend.snapshot.return_value = SnapshotResult(status="partial", vms_snapped=1, errors=["VM b: disk full"])
        with patch.object(tasks, "_db_session", _db((json.dumps(PROV),))), pytest.raises(RuntimeError, match="partial"):
            tasks.snapshot_range(range_id="r1", snapshot_id=SNAP_ID)

        backend.delete_snapshot.assert_awaited_once_with("r1", PROV, tasks._backend_snapshot_name(SNAP_ID))
        spies.snapshot_state.assert_called_with(SNAP_ID, "failed")


class TestRestoreSnapshotTask:
    def _rows(self, range_state: str, snapshot_data: dict | None = None, at_snapshot: str = "ready"):
        data = json.dumps(snapshot_data if snapshot_data is not None else {"snapshot_name": "tnabc"})
        return _db((data, at_snapshot), (range_state, json.dumps(PROV)))

    def test_restores_with_recorded_name_and_powers_on_a_ready_range(self, backend, spies):
        with patch.object(tasks, "_db_session", self._rows("ready")):
            assert tasks.restore_snapshot(range_id="r1", snapshot_id=SNAP_ID)["status"] == "restored"

        backend.restore.assert_awaited_once_with("r1", PROV, "tnabc", power_on=True)
        spies.range_state.assert_called_once_with("r1", "ready", only_from=tasks._RESTORABLE_STATES)

    def test_a_stopped_snapshot_is_not_powered_on(self, backend, spies):
        with patch.object(tasks, "_db_session", self._rows("failed", at_snapshot="stopped")):
            tasks.restore_snapshot(range_id="r1", snapshot_id=SNAP_ID)
        backend.restore.assert_awaited_once_with("r1", PROV, "tnabc", power_on=False)

    def test_legacy_snapshot_falls_back_to_the_bare_id(self, backend, spies):
        with patch.object(tasks, "_db_session", self._rows("ready", snapshot_data={"provider": "proxmox"})):
            tasks.restore_snapshot(range_id="r1", snapshot_id=SNAP_ID)
        backend.restore.assert_awaited_once_with("r1", PROV, SNAP_ID, power_on=True)

    def test_a_destroyed_range_is_left_alone(self, backend, spies):
        with patch.object(tasks, "_db_session", self._rows("destroyed")):
            assert tasks.restore_snapshot(range_id="r1", snapshot_id=SNAP_ID)["status"] == "skipped"

        backend.restore.assert_not_awaited()
        spies.range_state.assert_not_called()
        spies.snapshot_state.assert_called_once_with(SNAP_ID, "ready")

    def test_failure_is_recorded_only_on_a_restorable_range(self, backend, spies):
        backend.restore.return_value = RestoreResult(status="failed", errors=["VM vm-1: no snapshot named 'tnabc'"])
        with (
            patch.object(tasks, "_db_session", self._rows("ready")),
            pytest.raises(RuntimeError, match="restore failed"),
        ):
            tasks.restore_snapshot(range_id="r1", snapshot_id=SNAP_ID)

        args, kwargs = spies.range_state.call_args
        assert args == ("r1", "failed")
        assert kwargs["only_from"] == tasks._RESTORABLE_STATES


class TestDeleteSnapshotTask:
    def test_deletes_by_recorded_name(self, backend, spies):
        with patch.object(tasks, "_db_session", _db((json.dumps({"snapshot_name": "tnabc"}),), (json.dumps(PROV),))):
            assert tasks.delete_snapshot(range_id="r1", snapshot_id=SNAP_ID)["status"] == "deleted"
        backend.delete_snapshot.assert_awaited_once_with("r1", PROV, "tnabc")

    def test_a_snapshot_that_never_completed_needs_no_backend_call(self, backend, spies):
        with patch.object(tasks, "_db_session", _db((None,), (json.dumps(PROV),))):
            tasks.delete_snapshot(range_id="r1", snapshot_id=SNAP_ID)
        backend.delete_snapshot.assert_not_awaited()
        spies.snapshot_state.assert_called_once_with(SNAP_ID, "deleted")

    def test_backend_failure_raises_for_retry(self, backend, spies):
        backend.delete_snapshot.return_value = SnapshotDeleteResult(status="partial", errors=["VM vm-2: locked"])
        rows = _db((json.dumps({"snapshot_name": "tnabc"}),), (json.dumps(PROV),))
        with patch.object(tasks, "_db_session", rows), pytest.raises(RuntimeError, match="delete partial"):
            tasks.delete_snapshot(range_id="r1", snapshot_id=SNAP_ID)


class TestGuardedRangeStateOnARealDatabase:
    """The clobber fix, end to end through the real SQL on SQLite."""

    @pytest.fixture
    def ranges_db(self, tmp_path, monkeypatch):
        from sqlalchemy import create_engine, event, text

        url = f"sqlite:///{tmp_path / 'ranges.db'}"

        def add_now(dbapi_conn, _record):  # the task SQL uses Postgres' NOW()
            dbapi_conn.create_function("NOW", 0, lambda: "now")

        engine = create_engine(url)
        event.listen(engine.__class__, "connect", add_now)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE ranges (id TEXT PRIMARY KEY, state TEXT, updated_at TEXT,"
                    " error_message TEXT, provisioner_output TEXT)"
                )
            )
            conn.execute(text("INSERT INTO ranges (id, state) VALUES ('live', 'ready'), ('gone', 'destroyed')"))
        monkeypatch.setattr(tasks, "DATABASE_URL", url)
        yield lambda rid: engine.connect().execute(text("SELECT state FROM ranges WHERE id = :r"), {"r": rid}).scalar()
        event.remove(engine.__class__, "connect", add_now)
        engine.dispose()

    def test_failed_restore_does_not_overwrite_destroyed(self, ranges_db):
        for rid in ("live", "gone"):
            tasks._update_range_state(rid, "failed", error="restore failed", only_from=tasks._RESTORABLE_STATES)
        assert ranges_db("live") == "failed"
        assert ranges_db("gone") == "destroyed"

    def test_unguarded_update_still_writes(self, ranges_db):
        tasks._update_range_state("gone", "failed")
        assert ranges_db("gone") == "failed"
