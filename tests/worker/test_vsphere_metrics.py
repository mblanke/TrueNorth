"""vSphere range metrics and the scheduled run's single vCenter login.

collect_metrics reads every VM of a range in one PropertyCollector call. The collector
here is fake, but every spec the code builds is a real pyVmomi data object (they reject
unknown properties), as in test_vsphere_provision.py. REST goes through an
httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from pyVmomi import vim, vmodl
from worker.provisioners import vsphere_api as mod


def _run(coro):
    return asyncio.run(coro)


def _content(vm_id: str, **props):
    return SimpleNamespace(obj=vim.VirtualMachine(vm_id, None),
                           propSet=[SimpleNamespace(name=k, val=v) for k, v in props.items()])


def _stats(power="poweredOn", tools="guestToolsRunning", cpu=250, cpu_max=4800, mem=512, mem_cfg=2048, up=3600):
    # pyVmomi hands enums back as str subclasses; the code must store plain strings.
    return {
        "runtime.powerState": vim.VirtualMachine.PowerState(power),
        "guest.toolsRunningStatus": tools,
        "summary.quickStats.overallCpuUsage": cpu,
        "summary.quickStats.guestMemoryUsage": mem,
        "summary.quickStats.uptimeSeconds": up,
        "summary.runtime.maxCpuUsage": cpu_max,
        "summary.config.memorySizeMB": mem_cfg,
    }


class FakeCollector:
    """RetrievePropertiesEx over an inventory {vm_id: props}; vCenter's faults and paging."""

    def __init__(self, inventory: dict[str, dict], page: int | None = None, missing_fault: bool = True):
        self.inventory = inventory
        self.page = page
        self.missing_fault = missing_fault
        self.calls: list = []
        self.pending: list = []

    def _answer(self):
        batch, self.pending = (self.pending[: self.page], self.pending[self.page:]) if self.page else \
            (self.pending, [])
        return SimpleNamespace(objects=batch, token="more" if self.pending else None)

    def RetrievePropertiesEx(self, specSet, options):  # noqa: N802, N803 -- pyVmomi names
        (spec,) = specSet
        assert isinstance(spec, vmodl.query.PropertyCollector.FilterSpec)
        assert isinstance(options, vmodl.query.PropertyCollector.RetrieveOptions)
        ids = [o.obj._moId for o in spec.objectSet]
        self.calls.append((ids, list(spec.propSet[0].pathSet), spec.propSet[0].type))
        if self.missing_fault:
            for vm_id in ids:  # vCenter fails the whole call on the first unknown object
                if vm_id not in self.inventory:
                    raise vmodl.fault.ManagedObjectNotFound(obj=vim.VirtualMachine(vm_id, None))
        self.pending = [_content(i, **self.inventory[i]) for i in ids if i in self.inventory]
        return self._answer()

    def ContinueRetrievePropertiesEx(self, token):  # noqa: N802
        assert token == "more"
        self.calls.append(("continue",))
        return self._answer()


@pytest.fixture
def vc(monkeypatch):
    """SmartConnect hands out sessions on one fake collector; logins and logouts are counted."""
    state = SimpleNamespace(collector=FakeCollector({}), logins=[], logouts=0, fail_login=None)

    def connect(**kw):
        state.logins.append(kw)
        if state.fail_login:
            raise state.fail_login
        return SimpleNamespace(_stub=None, content=SimpleNamespace(propertyCollector=state.collector))

    def disconnect(si):
        state.logouts += 1

    monkeypatch.setattr(mod, "SmartConnect", connect)
    monkeypatch.setattr(mod, "Disconnect", disconnect)
    monkeypatch.setattr(mod, "VSPHERE_URL", "https://vcsa.test")
    monkeypatch.setattr(mod, "VSPHERE_METRICS_TIMEOUT", 15)
    return state


def _out(*vm_ids, names=None):
    return {"vms": [{"name": (names or {}).get(v, f"n-{v}"), "vm_id": v} for v in vm_ids]}


class TestCollectMetrics:
    def test_one_batched_read_per_range_of_exactly_the_metric_paths(self, vc):
        vc.collector.inventory = {"vm-1": _stats(), "vm-2": _stats(power="poweredOff", tools="guestToolsNotRunning",
                                                                   cpu=0, mem=0, up=None)}
        result = _run(mod.VsphereAPIProvisioner().collect_metrics("r1", _out("vm-1", "vm-2")))

        assert result.status == "ok" and result.source == "vsphere" and not result.synthetic, result.errors
        ((ids, paths, kind),) = vc.collector.calls
        assert ids == ["vm-1", "vm-2"] and kind is vim.VirtualMachine
        assert sorted(paths) == sorted(mod.METRIC_PATHS)
        assert {"summary.quickStats.overallCpuUsage", "summary.quickStats.guestMemoryUsage",
                "summary.quickStats.uptimeSeconds", "runtime.powerState",
                "guest.toolsRunningStatus"} <= set(paths)
        assert result.vms[0] == {
            "vm_id": "vm-1", "name": "n-vm-1", "power_state": "poweredOn", "tools_status": "guestToolsRunning",
            "cpu_usage_mhz": 250, "cpu_capacity_mhz": 4800, "memory_active_mb": 512, "memory_configured_mb": 2048,
            "uptime_seconds": 3600,
        }
        assert type(result.vms[0]["power_state"]) is str  # not the pyVmomi enum: it goes to JSON
        assert result.vms[1]["power_state"] == "poweredOff" and result.vms[1]["uptime_seconds"] is None
        # Outside a scheduled run: a session of its own, bounded, and logged out.
        assert len(vc.logins) == 1 and vc.logins[0]["httpConnectionTimeout"] == 15 and vc.logouts == 1

    def test_a_vm_deleted_behind_our_back_is_not_found_and_the_rest_still_read(self, vc):
        vc.collector.inventory = {"vm-1": _stats(), "vm-3": _stats()}
        result = _run(mod.VsphereAPIProvisioner().collect_metrics("r1", _out("vm-1", "vm-2", "vm-3")))
        assert result.status == "partial"
        assert result.errors == ["VM n-vm-2: not found in vCenter"]
        assert [v["power_state"] for v in result.vms] == ["poweredOn", "notFound", "poweredOn"]
        assert result.vms[1]["cpu_usage_mhz"] is None  # nothing made up for it
        assert [c[0] for c in vc.collector.calls] == [["vm-1", "vm-2", "vm-3"], ["vm-1", "vm-3"]]

    def test_a_vm_silently_absent_from_the_answer_is_not_found_too(self, vc):
        vc.collector = FakeCollector({"vm-1": _stats()}, missing_fault=False)
        result = _run(mod.VsphereAPIProvisioner().collect_metrics("r1", _out("vm-1", "vm-2")))
        assert result.status == "partial" and result.vms[1]["power_state"] == "notFound"

    def test_paged_answers_are_followed(self, vc):
        vc.collector = FakeCollector({f"vm-{i}": _stats(cpu=i) for i in range(5)}, page=2)
        result = _run(mod.VsphereAPIProvisioner().collect_metrics("r1", _out(*(f"vm-{i}" for i in range(5)))))
        assert result.status == "ok" and [v["cpu_usage_mhz"] for v in result.vms] == [0, 1, 2, 3, 4]
        assert vc.collector.calls[1:] == [("continue",), ("continue",)]

    def test_no_vm_id_is_an_error_not_a_skip(self, vc):
        vc.collector.inventory = {"vm-1": _stats()}
        out = {"vms": [{"name": "a", "vm_id": "vm-1"}, {"name": "b"}]}
        result = _run(mod.VsphereAPIProvisioner().collect_metrics("r1", out))
        assert result.status == "partial" and result.errors == ["VM b: no vm_id recorded"]
        assert [v["name"] for v in result.vms] == ["a"]

    def test_vcenter_unreachable_is_a_failed_result_without_values(self, vc):
        vc.fail_login = vim.fault.InvalidLogin(msg="Cannot complete login due to an incorrect user name or password.")
        result = _run(mod.VsphereAPIProvisioner().collect_metrics("r1", _out("vm-1")))
        assert result.status == "failed" and result.vms == []
        assert "incorrect user name or password" in result.errors[0]

    def test_a_range_without_vms_reads_nothing(self, vc):
        result = _run(mod.VsphereAPIProvisioner().collect_metrics("r1", {"vms": []}))
        assert result.status == "ok" and result.vms == [] and vc.logins == [] and vc.collector.calls == []


class TestScheduledRunSession:
    def test_one_pyvmomi_login_for_every_range_in_the_run(self, vc):
        vc.collector.inventory = {f"vm-{i}": _stats() for i in range(4)}
        prov = mod.VsphereAPIProvisioner()

        async def run():
            async with prov.session():
                a = await prov.collect_metrics("r1", _out("vm-0", "vm-1"))
                b = await prov.collect_metrics("r2", _out("vm-2", "vm-3"))
                assert vc.logouts == 0  # still the run's session
                return a, b

        a, b = _run(run())
        assert a.status == b.status == "ok"
        assert len(vc.logins) == 1 and vc.logouts == 1 and len(vc.collector.calls) == 2
        assert prov._scope is None

    def test_a_failed_login_is_not_retried_for_every_range(self, vc):
        vc.fail_login = OSError("connection refused")
        prov = mod.VsphereAPIProvisioner()

        async def run():
            async with prov.session():
                return [await prov.collect_metrics(f"r{i}", _out(f"vm-{i}")) for i in range(3)]

        results = _run(run())
        assert [r.status for r in results] == ["failed"] * 3
        assert all("vCenter login failed: connection refused" in r.errors[0] for r in results)
        assert len(vc.logins) == 1 and vc.logouts == 0

    def test_rest_health_checks_share_one_token_and_log_out(self, vc):
        calls: list[tuple[str, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path, request.headers.get("vmware-api-session-id", "")))
            if request.url.path == "/api/session":
                return httpx.Response(201, json="tok-1") if request.method == "POST" else httpx.Response(204)
            return httpx.Response(200, json={"state": "POWERED_ON"})

        prov = mod.VsphereAPIProvisioner()
        prov._transport = httpx.MockTransport(handler)

        async def run():
            async with prov.session():
                return [await prov.health_check(f"r{i}", _out(f"vm-{i}", f"vm-{i}b")) for i in range(3)]

        results = _run(run())
        assert all(r.healthy for r in results)
        assert [c for c in calls if c[1] == "/api/session"] == [("POST", "/api/session", ""),
                                                                ("DELETE", "/api/session", "tok-1")]
        assert len([c for c in calls if c[1].startswith("/api/vcenter/vm/")]) == 6
        assert prov._session_token is None
        assert vc.logins == []  # health is REST only: no pyVmomi session was opened

    def test_the_session_is_closed_when_the_run_fails(self, vc):
        vc.collector.inventory = {"vm-1": _stats()}
        prov = mod.VsphereAPIProvisioner()

        async def run():
            async with prov.session():
                await prov.collect_metrics("r1", _out("vm-1"))
                raise RuntimeError("worker killed the run")

        with pytest.raises(RuntimeError, match="worker killed"):
            _run(run())
        assert len(vc.logins) == 1 and vc.logouts == 1 and prov._scope is None


def test_snapshot_sessions_are_still_opened_without_a_socket_timeout(vc):
    """Snapshots and builds wait on long vCenter tasks; only the metrics read is time-boxed."""
    prov = mod.VsphereAPIProvisioner()
    prov._fan_out = MagicMock(return_value=({}, {"vm-1"}))
    _run(prov.snapshot("r1", _out("vm-1"), "tnx"))
    assert "httpConnectionTimeout" not in vc.logins[0]
