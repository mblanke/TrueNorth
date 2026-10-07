"""Windows Server roles inside range VMs (worker/provisioners/vsphere_roles.py).

Guest operations are faked at the GuestSession level: what is held here is the sequencing
(features, reboot on 3010, promote only the forest root), the PowerShell actually sent, and
that every failure or skip comes back per role instead of a quiet "ready". The provision
flow around it (credentials through Sysprep, errors on the range) is in
test_vsphere_provision.py, TestWindowsRoles.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from pyVmomi import vim
from worker.provisioners import vsphere_guest as guest
from worker.provisioners import vsphere_infra as infra
from worker.provisioners import vsphere_roles as roles


def _decode(arguments: str) -> str:
    return base64.b64decode(arguments.rsplit(" ", 1)[1]).decode("utf-16-le")


class FakeSession:
    """GuestSession's surface: wait_ready, run, and the clock the reboot wait uses."""

    def __init__(self, codes=(), fail_on: str = ""):
        self.vm = SimpleNamespace(name="dc01", guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning"))
        self.creds = guest.windows_credentials()
        self.codes = list(codes)
        self.fail_on = fail_on
        self.scripts: list[str] = []
        self.waits = 0
        self._poll = 1
        self._clock = lambda: 0.0
        self._sleep = lambda s: None

    def wait_ready(self, deadline):
        self.waits += 1

    def run(self, cmd, deadline):
        script = _decode(cmd.arguments)
        self.scripts.append(script)
        if self.fail_on and self.fail_on in script:
            raise RuntimeError(f"guest ops refused for {self.creds.password.reveal()}")
        code = self.codes.pop(0) if self.codes else 0
        return ("ok" if code in cmd.ok_codes else "failed"), code


def _dc(**extra):
    return {"name": "dc01", "roles": ["ad-ds", "dns"], "role_features": ["AD-Domain-Services", "DNS", "GPMC"],
            "ad_domain": "corp.test", **extra}


@pytest.fixture
def reboots():
    calls = []
    return calls, (lambda session, deadline: calls.append(deadline))


def test_features_install_then_the_forest_root_is_promoted(reboots):
    calls, reboot = reboots
    s = FakeSession(codes=[3010, 3010])
    out = roles.install(s, _dc(ad_forest_root=True), 100, reboot)
    assert out["ad-ds"] == {"status": "ok", "detail": "forest corp.test created"}
    assert out["dns"]["status"] == "ok"
    feature_script, forest_script = s.scripts
    assert "Install-WindowsFeature -Name AD-Domain-Services,DNS,GPMC" in feature_script
    assert "Resize-Partition -DriveLetter C" in feature_script
    assert "Install-ADDSForest -DomainName 'corp.test' -DomainNetbiosName 'CORP'" in forest_script
    assert len(calls) == 2


def test_an_extra_dc_is_skipped_not_ok(reboots):
    calls, reboot = reboots
    s = FakeSession()
    out = roles.install(s, _dc(ad_forest_root=False), 100, reboot)
    assert out["ad-ds"]["status"] == "skipped" and "not automated" in out["ad-ds"]["detail"]
    assert out["dns"]["status"] == "ok"
    assert len(s.scripts) == 1 and not calls


def test_failed_feature_install_fails_every_feature_role(reboots):
    out = roles.install(FakeSession(codes=[1]), _dc(ad_forest_root=True), 100, reboots[1])
    assert {v["status"] for v in out.values()} == {"failed"}


def test_forest_failure_is_pinned_on_ad_ds_and_the_password_never_shows(reboots):
    s = FakeSession(fail_on="Install-ADDSForest")
    out = roles.install(s, _dc(ad_forest_root=True), 100, reboots[1])
    assert out["ad-ds"]["status"] == "failed" and "guest ops refused" in out["ad-ds"]["detail"]
    assert s.creds.password.reveal() not in out["ad-ds"]["detail"]
    assert out["dns"]["status"] == "ok"


def test_image_roles_need_no_guest_work_unless_the_image_is_missing():
    s = FakeSession()
    assert roles.install(s, {"roles": ["exchange"], "role_features": []}, 100) == {
        "exchange": {"status": "ok", "detail": "in role image"}}
    assert not s.scripts and not roles.needs_guest({"roles": ["exchange"], "role_features": []})
    missing = roles.initial_status({"roles": ["exchange"], "role_image_missing": "srv2022-exchange2019"})
    assert missing["exchange"]["status"] == "skipped" and "srv2022-exchange2019" in missing["exchange"]["detail"]


def test_errors_name_every_role_that_did_not_end_ok():
    errs = roles.errors("r-dc01", {"ad-ds": {"status": "skipped", "detail": "x"}, "dns": {"status": "ok", "detail": ""}})
    assert errs == ["VM r-dc01: role ad-ds skipped (x)"]


def test_reboot_waits_for_tools_to_drop_then_for_the_login():
    s = FakeSession()
    seen = []

    def reboot_guest():
        seen.append("reboot")

    def sleep(_):  # Tools go down on the first poll after the reboot request
        s.vm.guest.toolsRunningStatus = "guestToolsNotRunning"

    s.vm.RebootGuest = reboot_guest
    s._sleep = sleep
    roles.reboot(s, 100)
    assert seen == ["reboot"] and s.waits == 1


def test_netbios_name_is_the_first_label_capped_at_15():
    assert roles.netbios("corp.range.local") == "CORP"
    assert roles.netbios("a-very-long-domain-name.local") == "AVERYLONGDOMAIN"
    assert roles.ps_quote("o'brien") == "o''brien"


def test_disk_grows_only_upward_and_only_for_role_vms():
    def disk(gb):
        return vim.vm.device.VirtualDisk(key=2000, capacityInKB=gb * 1024 * 1024)

    (change,) = infra.disk_grow_changes([disk(40)], 100)
    assert change.operation == "edit" and change.device.capacityInKB == 100 * 1024 * 1024
    assert infra.disk_grow_changes([disk(200)], 100) == []
    assert infra.disk_grow_changes([], 100) == []
    plain = infra.hardware_spec({"cores": 2, "memory_mb": 4096, "disk_gb": 100}, [disk(40)], [])
    assert not any(isinstance(c.device, vim.vm.device.VirtualDisk) for c in plain.deviceChange or [])
    role_vm = infra.hardware_spec({"cores": 2, "memory_mb": 4096, "disk_gb": 100, "roles": ["file"]}, [disk(40)], [])
    assert [c.device.capacityInKB for c in role_vm.deviceChange] == [100 * 1024 * 1024]
