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
from worker.provisioners import vsphere_guest as guest_mod

vc, worker_reserves = _vsp.vc, _vsp.worker_reserves  # the fake vCenter; the worker's reservations (autouse)

SUCCEEDED, FAILED = "TOOLSDEPLOYPKG_SUCCEEDED", "TOOLSDEPLOYPKG_FAILED"
RUNNING, PENDING = "TOOLSDEPLOYPKG_RUNNING", "TOOLSDEPLOYPKG_PENDING"
SYSPREP_ERROR = "Windows could not finish configuring the system (oobeSystem)"
TEMPLATE_PW = "Template-Build-1!"  # the golden template's build Administrator password

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
    repeats; None: not reported; a dict: ``status`` with its ``error`` / ``end``, either a
    value or a callable of the VM). Returns the read count per VM name."""
    reads: dict[str, int] = {}

    def info(self):
        name = self._vm.name
        seq = next((s for k, s in script.items() if name.endswith(k)), [SUCCEEDED])
        reads[name] = reads.get(name, 0) + 1
        item = seq[min(reads[name], len(seq)) - 1]
        if item is None:
            return None
        item = item if isinstance(item, dict) else {"status": item}
        error, end = (v(self._vm) if callable(v) else v for v in (item.get("error"), item.get("end")))
        return SimpleNamespace(customizationStatus=item["status"], endTime=end,
                               errorMsg=error or (SYSPREP_ERROR if item["status"] == FAILED else None))

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
        monkeypatch.setenv("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", TEMPLATE_PW)
        script = {"win01": [RUNNING]}
        _sysprep(monkeypatch, script)
        prov = _prov(vc)
        prov._guest_ip_timeout = 600
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {"finish_by": clock.t + 80}))
        assert result.status == "ok" and result.not_ready == [], result.errors
        rec = _out(result)["customization"]
        assert rec["status"] == "running" and rec["vsphere_status"] == RUNNING
        assert real_time.time() + 500 < rec["deadline"] <= real_time.time() + 600  # the window's end, epoch
        assert real_time.time() - 60 < rec["powered_on_at"] <= real_time.time()  # for the stale-success rule
        # No template-login check while Sysprep is still running: it would prove nothing.
        assert _out(result)["template_login"] == {"status": "inconclusive", "detail": "checked when its guest "
                                                  "customization (Sysprep) has finished (reconcile)"}
        assert TEMPLATE_PW not in vc.guest_ops.passwords_seen
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
        # Succeeded: ok, then the template-login check the build left, proven by a login as the
        # VM's own Administrator; the stored record then certifies.
        script["win01"] = [SUCCEEDED]
        sysprep_pw = vc.vms[_out(result)["vm_id"]].customize_specs[-1].identity.guiUnattended.password.value
        accounts = {"win01": [guest_mod.RangeAccount("Administrator", guest_mod.Secret(sysprep_pw), True)]}
        settled = _run(_prov(vc).reconcile_guest_ips(RANGE_ID, stored, accounts=accounts))
        assert settled[WIN]["customization"] == {"status": "ok", "vsphere_status": SUCCEEDED, "source": "guest"}
        assert settled[WIN]["template_login"] == {"status": "rejected", "username": "Administrator",
                                                  "detail": "the golden template's password is refused"}
        assert TEMPLATE_PW in vc.guest_ops.passwords_seen  # tried now, once, after Sysprep
        assert setup_reconcile._merge_ips(stored, settled) == []
        assert setup_reconcile._merge_template_logins(stored, settled) == []
        assert not [w for w in stored["warnings"] if cz.NOT_FINISHED_YET in w]
        assert evidence.guest_readiness(stored)[0] == "pass"
        # Without the range's Administrator the refusal proves nothing: inconclusive, never a pass.
        unproven = _run(_prov(vc).reconcile_guest_ips(RANGE_ID, {**stored, "vms": [
            {**_out(result), "customization": rec}]}))
        assert unproven[WIN]["template_login"]["status"] == "inconclusive"

    def test_a_success_from_before_this_power_on_is_not_this_boots(self, vc, monkeypatch):
        """Item 4 of the #264 review: a SUCCEEDED whose endTime predates the power-on is left
        over (the template's, a previous boot's): not run yet, so the VM is not ready at the
        window's end; a fresh one is accepted."""
        old = datetime(2026, 1, 1, tzinfo=UTC)
        script = {"win01": [{"status": SUCCEEDED, "end": old}]}
        _sysprep(monkeypatch, script)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(MIXED), {}))
        assert result.not_ready == [WIN]
        assert _out(result)["customization"]["status"] == "failed"
        assert any(f"last status {SUCCEEDED} from before this power-on" in e for e in result.errors), result.errors

    def test_a_fresh_success_after_a_left_over_one_is_accepted(self, vc, monkeypatch):
        _sysprep(monkeypatch, {"win01": [{"status": SUCCEEDED, "end": datetime(2026, 1, 1, tzinfo=UTC)},
                                         {"status": SUCCEEDED, "end": lambda vm: datetime.now(UTC)}]})
        prov = _prov(vc)
        prov._guest_ip_timeout = 60
        result = _run(prov.provision(RANGE_ID, _rendered(MIXED), {}))
        assert result.not_ready == [], result.errors
        assert _out(result)["customization"]["status"] == "ok"

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


LEAKY = "net user Administrator S3cret! failed"


def _secret_free(blob, *secrets: str) -> None:
    text = json.dumps(blob, default=str)
    for secret in secrets:
        assert secret not in text, f"{secret!r} stored"


class TestRedaction:
    """Item 1 of the #264 review: Sysprep's message is masked before it is stored, on the
    build path and on the reconcile path."""

    def test_build_path(self, vc, monkeypatch):
        monkeypatch.setenv("TN_TEMPLATE_WINDOWS_ADMIN_PASSWORD", TEMPLATE_PW)

        def error(vm):
            own = vm.customize_specs[-1].identity.guiUnattended.password.value
            return f"{LEAKY}; then {own} and {TEMPLATE_PW} were rejected"

        _sysprep(monkeypatch, {"win01": [{"status": FAILED, "error": error}]})
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(MIXED), {}))
        own = vc.vms[_out(result)["vm_id"]].customize_specs[-1].identity.guiUnattended.password.value
        assert result.not_ready == [WIN]
        detail = _out(result)["customization"]["detail"]
        assert "net user Administrator *** failed; then *** and *** were rejected" in detail
        _secret_free({"stored": _stored(result), "errors": result.errors, "warnings": result.warnings},
                     "S3cret!", own, TEMPLATE_PW)

    def test_reconcile_path(self, vc, monkeypatch, clock):
        script = {"win01": [RUNNING]}
        _sysprep(monkeypatch, script)
        prov = _prov(vc)
        prov._guest_ip_timeout = 600
        stored = _stored(_run(prov.provision(RANGE_ID, _rendered(MIXED), {"finish_by": clock.t + 80})))
        script["win01"] = [{"status": FAILED, "error": f"{LEAKY}; analyst Declared-Pw-9 too"}]
        accounts = {"win01": [guest_mod.RangeAccount("analyst", guest_mod.Secret("Declared-Pw-9"))]}
        settled = _run(_prov(vc).reconcile_guest_ips(RANGE_ID, stored, accounts=accounts))
        assert "net user Administrator *** failed; analyst *** too" in settled[WIN]["customization"]["detail"]
        assert "net user Administrator *** failed" in settled[WIN]["not_ready"]
        _secret_free(settled, "S3cret!", "Declared-Pw-9")

    @pytest.mark.parametrize("raw,masked", [
        (LEAKY, "net user Administrator *** failed"),
        ("net.exe user bob Pw-1 /add", "net.exe user bob *** /add"),
        ("net user bob /add", "net user bob /add"),  # no password in it
        ("Password=Hunter2! rejected", "Password=*** rejected"),
        ("<Value>Plain-1</Value>", "<Value>***</Value>"),
    ])
    def test_scrub(self, raw, masked):
        assert cz.scrub(raw) == masked


class TestRecordDeadline:
    """Item 2 of the #264 review: a ``running`` check never stays open past its window plus
    the grace, whatever vCenter does."""

    def test_expired_from_the_record_alone(self):
        vm = {"name": WIN, "vm_id": "vm-1", "customization": {"status": "running", "deadline": 1000.0,
                                                              "vsphere_status": RUNNING}}
        assert cz.expired(vm, 1000.0 + cz.GRACE_SECONDS) is None
        verdict = cz.expired(vm, 1001.0 + cz.GRACE_SECONDS)
        assert verdict["customization"]["status"] == "failed" and verdict["customization"]["source"] == "record"
        assert "not confirmed within" in verdict["not_ready"]
        assert cz.expired({**vm, "customization": {"status": "ok"}}, 1e12) is None


class TestRead:
    def test_a_left_over_success_event_is_ignored(self):
        old = vim.event.CustomizationSucceeded(key=1, createdTime=datetime(2026, 1, 1, tzinfo=UTC))
        content = SimpleNamespace(eventManager=SimpleNamespace(QueryEvents=lambda spec: [old]))
        vm = vim.VirtualMachine("vm-7", None)
        assert cz.read(content, vm)["status"] == SUCCEEDED
        assert cz.read(content, vm, since=datetime(2026, 10, 1, tzinfo=UTC).timestamp())["status"] == ""

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
