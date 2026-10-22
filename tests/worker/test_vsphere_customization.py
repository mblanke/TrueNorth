"""#255: every Windows range VM's guest customization (Sysprep) outcome is checked after
power-on, against the vSphere fakes of test_vsphere_provision.

Before: only a Windows VM with software to install (``_guest`` credentials) ever looked at
``guest.customizationInfo``; any other one whose Sysprep failed kept the golden template's
build Administrator password and Basic WinRM and could still be marked ready.
"""

from __future__ import annotations

import json
import time as real_time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import test_vsphere_provision as _vsp
from app.range_certification import evidence
from pyVmomi import vim
from test_vsphere_provision import R8, RANGE_ID, _prov, _rendered, _run
from worker import range_alloc, setup_reconcile
from worker.provisioners import vsphere_api as mod
from worker.provisioners import vsphere_customization as cz

vc, worker_reserves = _vsp.vc, _vsp.worker_reserves  # the fake vCenter; the worker's reservations (autouse)

SUCCEEDED, FAILED = "TOOLSDEPLOYPKG_SUCCEEDED", "TOOLSDEPLOYPKG_FAILED"
RUNNING, PENDING = "TOOLSDEPLOYPKG_RUNNING", "TOOLSDEPLOYPKG_PENDING"
SYSPREP_ERROR = "Windows could not finish configuring the system (oobeSystem)"

# A Windows workstation with no software (so no ``_guest`` credentials: the #255 case) and a
# Linux VM beside it.
MIXED = {
    "name": "Windows Starter",
    "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.70.10.0/24"}]},
    "nodes": [
        {"id": "win01", "role": "workstation", "os": "windows-server-2022", "vlan": "lan", "ip": "10.70.10.21"},
        {"id": "ubu01", "role": "workstation", "os": "ubuntu-2404", "vlan": "lan", "ip": "10.70.10.11"},
    ],
}
WIN = f"{R8}-win01"


def _sysprep(monkeypatch, script: dict[str, list]) -> dict[str, int]:
    """``guest.customizationInfo`` per VM: node suffix -> statuses read in turn (the last one
    repeats; None: not reported). Returns the read count per VM name."""
    reads: dict[str, int] = {}

    def info(self):
        name = self._vm.name
        seq = next((s for k, s in script.items() if name.endswith(k)), [SUCCEEDED])
        reads[name] = reads.get(name, 0) + 1
        status = seq[min(reads[name], len(seq)) - 1]
        if status is None:
            return None
        return SimpleNamespace(customizationStatus=status, errorMsg=SYSPREP_ERROR if status == FAILED else None)

    monkeypatch.setattr(_vsp.FakeGuest, "customizationInfo", property(info, lambda self, v: None), raising=False)
    return reads


@pytest.fixture
def clock(monkeypatch):
    """10 s pass per look at the monotonic clock, so a bounded wait takes no real time."""

    class Clock:
        t = real_time.monotonic()
        sleep = staticmethod(lambda s: None)
        time = staticmethod(real_time.time)

        def monotonic(self):
            Clock.t += 10
            return Clock.t

    monkeypatch.setattr(mod, "time", Clock())
    return Clock


def _out(result, name=WIN) -> dict:
    return next(v for v in result.vms if v["name"] == name)


def _stored(result) -> dict:
    return range_alloc.stored_output("vsphere_api", RANGE_ID, result)


class TestBuild:
    def test_succeeded_is_recorded_and_the_range_is_ready_and_certifiable(self, vc, monkeypatch):
        reads = _sysprep(monkeypatch, {"win01": [PENDING, RUNNING, SUCCEEDED]})
        prov = _prov(vc)
        prov._guest_ip_timeout = 60
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {}))
        assert result.status == "ok" and result.not_ready == [], result.errors
        assert _out(result)["customization"] == {"status": "ok", "vsphere_status": SUCCEEDED, "source": "guest"}
        assert reads[WIN] == 3  # polled until it finished, not one look
        assert "customization" not in _out(result, f"{R8}-ubu01")  # Linux: cloud-init, no Sysprep
        assert range_alloc.build_outcome(result) == ("ready", None)
        # S6: what the worker stored is what the certification record reads.
        status, summary, detail = evidence.guest_readiness(_stored(result))
        assert status == "pass" and "Sysprep succeeded on 1 Windows VM(s)" in summary
        row = next(r for r in detail["vms"] if r["name"] == WIN)
        assert row["customization"] == {"status": "ok", "vsphere_status": SUCCEEDED, "source": "guest"}

    def test_failed_fails_the_vm_and_the_range_and_skips_its_guest_work(self, vc, monkeypatch):
        _sysprep(monkeypatch, {"win01": [RUNNING, FAILED]})
        seen: dict[str, list] = {}

        def spy(name):
            async def record(si, vm_defs, outs, *a, **k):
                seen[name] = sorted(o["name"] for o in outs)
            return record

        prov = _prov(vc)
        prov._guest_ip_timeout = 60
        for name in ("_install_roles", "_install_software", "_check_template_logins"):
            setattr(prov, name, spy(name))
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {}))
        assert result.not_ready == [WIN] and len(result.vms) == 2  # recorded, so destroy finds it
        rec = _out(result)["customization"]
        assert (rec["status"], rec["vsphere_status"], rec["source"]) == ("failed", FAILED, "guest")
        (err,) = [e for e in result.errors if e.startswith(f"VM {WIN}:")]
        assert f"guest customization (Sysprep) failed ({FAILED}: {SYSPREP_ERROR})" in err
        assert "build Administrator password" in err
        state, error = range_alloc.build_outcome(result)
        assert state == "failed" and error.startswith(f"1 VM(s) built but not ready: VM {WIN}: guest customization")
        assert seen == {k: [f"{R8}-ubu01"] for k in ("_install_roles", "_install_software", "_check_template_logins")}
        status, summary, _ = evidence.guest_readiness(_stored(result))
        assert (status, summary) == ("fail", f"1 VM(s) not ready: {WIN}")

    def test_never_finishing_fails_at_the_readiness_window_not_before(self, vc, monkeypatch, clock):
        reads = _sysprep(monkeypatch, {"win01": [PENDING, RUNNING]})
        prov = _prov(vc)
        prov._guest_ip_timeout = 120
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {}))
        assert result.not_ready == [WIN], result.errors
        assert 2 < reads[WIN] < 20  # polled, and bounded by the window
        rec = _out(result)["customization"]
        assert (rec["status"], rec["vsphere_status"]) == ("failed", RUNNING)
        assert any(e.startswith(f"VM {WIN}: guest customization (Sysprep) did not finish within 120s of power-on "
                                f"(last status {RUNNING})") for e in result.errors), result.errors
        assert range_alloc.build_outcome(result)[0] == "failed"

    def test_a_budget_cut_is_left_to_reconcile_then_settled(self, vc, monkeypatch, clock):
        """Powered on just before the build's ``finish_by``: the budget, not the window, ended
        the wait. Not failed: left ``running`` with the window's end for the reconcile task."""
        script = {"win01": [RUNNING]}
        _sysprep(monkeypatch, script)
        prov = _prov(vc)
        prov._guest_ip_timeout = 600
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {"finish_by": clock.t + 80}))
        assert result.status == "ok" and result.not_ready == [], result.errors
        rec = _out(result)["customization"]
        assert rec["status"] == "running" and rec["vsphere_status"] == RUNNING
        assert real_time.time() + 500 < rec["deadline"] <= real_time.time() + 600  # the window's end, epoch
        assert any(w.startswith(f"VM {WIN}: {cz.NOT_FINISHED_YET}") for w in result.warnings), result.warnings
        assert range_alloc.build_outcome(result) == ("ready", None)
        stored = _stored(result)
        assert '"running"' in json.dumps(stored)  # what db_ops.ranges_with_running_setups selects on
        assert [v["name"] for v in setup_reconcile._ip_pending(stored)] == [WIN]
        assert evidence.guest_readiness(stored)[:2] == (
            "incomplete", f"guest customization (Sysprep) check still running on {WIN}")

        # Inside the window and still running: nothing to store yet.
        assert _run(_prov(vc).reconcile_guest_ips(RANGE_ID, stored)) == {}
        # Past the window: not ready.
        late = _run(_prov(vc).reconcile_guest_ips(RANGE_ID, stored, now=rec["deadline"] + 1))
        assert late[WIN]["customization"]["status"] == "failed"
        assert "did not finish before its readiness window ended" in late[WIN]["not_ready"]
        # Failed inside the window: not ready at once.
        script["win01"] = [FAILED]
        failed = _run(_prov(vc).reconcile_guest_ips(RANGE_ID, stored))
        assert failed[WIN]["customization"]["status"] == "failed" and SYSPREP_ERROR in failed[WIN]["not_ready"]
        # Succeeded: ok, and the stored record then certifies.
        script["win01"] = [SUCCEEDED]
        settled = _run(_prov(vc).reconcile_guest_ips(RANGE_ID, stored))
        assert settled == {WIN: {"customization": {"status": "ok", "vsphere_status": SUCCEEDED, "source": "guest"}}}
        assert setup_reconcile._merge_ips(stored, settled) == []
        assert not [w for w in stored["warnings"] if cz.NOT_FINISHED_YET in w]
        assert evidence.guest_readiness(stored)[0] == "pass"

    def test_reconcile_never_reads_a_vm_that_is_not_this_ranges(self, vc, monkeypatch):
        reads = _sysprep(monkeypatch, {})
        built = _run(_prov(vc).provision(RANGE_ID, _rendered(MIXED), {}))
        out = dict(_out(built), customization={"status": "running", "deadline": 0})
        vc.vms[out["vm_id"]].config.annotation = mod.range_annotation("another-range-entirely")
        before = reads[WIN]
        assert _run(_prov(vc).reconcile_guest_ips(RANGE_ID, {"vms": [out]}, now=1.0)) == {}
        assert reads[WIN] == before

    def test_check_off_records_unverified_but_an_observed_failure_still_fails(self, vc, monkeypatch):
        script = {"win01": [RUNNING]}
        reads = _sysprep(monkeypatch, script)
        prov = _prov(vc)
        prov._guest_ip_check, prov._guest_ip_timeout = False, 600  # a simulator
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {}))
        assert result.not_ready == [] and reads[WIN] == 1  # one look, no wait
        assert _out(result)["customization"]["status"] == "unverified"
        assert evidence.guest_readiness(_stored(result))[0] != "pass"

        script["win01"] = [FAILED]
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {}))
        assert result.not_ready == [WIN]


class TestRead:
    def test_a_vcenter_without_customization_info_falls_back_to_events(self):
        queried = []
        ok = vim.event.CustomizationSucceeded(key=1, createdTime=datetime(2026, 10, 22, 10, tzinfo=UTC),
                                              fullFormattedMessage="Customization of VM win01 succeeded.")
        bad = vim.event.CustomizationSysprepFailed(key=2, createdTime=datetime(2026, 10, 22, 11, tzinfo=UTC),
                                                   fullFormattedMessage="Sysprep failed on win01.")
        content = SimpleNamespace(eventManager=SimpleNamespace(
            QueryEvents=lambda spec: queried.append(spec) or [ok, bad]))
        vm = vim.VirtualMachine("vm-7", None)
        seen = cz.read(content, vm)
        assert seen == {"status": FAILED, "error": "Sysprep failed on win01.", "source": "event:CustomizationSysprepFailed"}
        (spec,) = queried
        assert spec.entity.entity is vm and spec.entity.recursion == "self"
        assert {"CustomizationSucceeded", "CustomizationSysprepFailed"} <= set(spec.eventTypeId)
        assert "failed (event:CustomizationSysprepFailed: Sysprep failed on win01.)" in \
            cz.assess({**seen, "checked": True})["not_ready"]

    def test_nothing_reported_and_no_events_is_never_a_pass(self):
        content = SimpleNamespace(eventManager=SimpleNamespace(QueryEvents=lambda spec: []))
        vm = SimpleNamespace(guest=SimpleNamespace(customizationInfo=None))
        assert cz.read(content, vm)["status"] == ""
        broken = SimpleNamespace(eventManager=SimpleNamespace(QueryEvents=lambda spec: 1 / 0))
        assert cz.read(broken, vm)["status"] == ""
        verdict = cz.assess({"status": "", "checked": True, "cut": False, "timeout": 600})
        assert verdict["customization"]["status"] == "failed" and verdict["not_ready"]

    def test_only_windows_vms_are_held_to_it(self):
        assert cz.applies({"os": "windows-server-2022"}) is True
        assert cz.applies({"os": "ubuntu-2404"}) is False
        assert cz.applies({"os": "pfsense"}) is False
