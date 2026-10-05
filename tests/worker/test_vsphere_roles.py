"""vSphere API provisioner: disk size and Windows Server role install.

Guest operations and pyVmomi are mocked; what is held here is the sequencing (install,
reboot on 3010, promote the forest root only), the PowerShell actually sent, and that
every failure or skip surfaces as an error instead of a quiet "ready".
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from worker.provisioners import vsphere_api
from worker.provisioners.vsphere_api import VsphereAPIProvisioner


def _decode(script_arg: str) -> str:
    return base64.b64decode(script_arg).decode("utf-16-le")


@pytest.fixture
def prov():
    p = VsphereAPIProvisioner()
    p._guest_password = "Pa55w0rd!"
    p._run_guest_ps = AsyncMock(return_value=0)
    p._guest_reboot = AsyncMock()
    return p


def _dc(**extra):
    return {"name": "dc01", "roles": ["ad-ds", "dns"],
            "role_features": ["AD-Domain-Services", "DNS", "GPMC"], "ad_domain": "corp.test", **extra}


def test_feature_roles_install_then_the_forest_root_is_promoted(prov):
    prov._run_guest_ps.side_effect = [3010, 3010]
    out = asyncio.run(prov._install_roles(MagicMock(), "vm-1", _dc(ad_forest_root=True)))
    assert out["ad-ds"] == {"status": "ok", "detail": "forest corp.test created"}
    assert out["dns"]["status"] == "ok"
    feature_script, forest_script = (c.args[2] for c in prov._run_guest_ps.call_args_list)
    assert "Install-WindowsFeature -Name AD-Domain-Services,DNS,GPMC" in feature_script
    assert "Resize-Partition -DriveLetter C" in feature_script
    assert "Install-ADDSForest -DomainName 'corp.test' -DomainNetbiosName 'CORP'" in forest_script
    assert prov._guest_reboot.await_count == 2


def test_an_extra_dc_is_reported_as_skipped_not_ok(prov):
    out = asyncio.run(prov._install_roles(MagicMock(), "vm-1", _dc(ad_forest_root=False)))
    assert out["ad-ds"]["status"] == "skipped"
    assert out["dns"]["status"] == "ok"
    assert prov._run_guest_ps.await_count == 1
    prov._guest_reboot.assert_not_awaited()


def test_failed_feature_install_marks_every_feature_role_failed(prov):
    prov._run_guest_ps.return_value = 1
    out = asyncio.run(prov._install_roles(MagicMock(), "vm-1", _dc(ad_forest_root=True)))
    assert {v["status"] for v in out.values()} == {"failed"}


def test_forest_failure_is_pinned_on_ad_ds(prov):
    prov._run_guest_ps.side_effect = [0, RuntimeError("guest ops refused")]
    out = asyncio.run(prov._install_roles(MagicMock(), "vm-1", _dc(ad_forest_root=True)))
    assert out["ad-ds"]["status"] == "failed" and "guest ops refused" in out["ad-ds"]["detail"]
    assert out["dns"]["status"] == "ok"


def test_without_guest_credentials_nothing_runs(prov):
    prov._guest_password = ""
    out = asyncio.run(prov._install_roles(MagicMock(), "vm-1", _dc(ad_forest_root=True)))
    assert {v["status"] for v in out.values()} == {"skipped"}
    prov._run_guest_ps.assert_not_awaited()


def test_image_roles_need_no_guest_work(prov):
    out = asyncio.run(prov._install_roles(MagicMock(), "vm-1", {"roles": ["exchange"], "role_features": []}))
    assert out == {"exchange": {"status": "ok", "detail": "in role image"}}
    prov._run_guest_ps.assert_not_awaited()


def test_run_guest_ps_sends_encoded_powershell_and_returns_the_exit_code(monkeypatch):
    p = VsphereAPIProvisioner()
    p._guest_user, p._guest_password = "Administrator", "Pa55w0rd!"
    monkeypatch.setattr(vsphere_api.asyncio, "sleep", AsyncMock())
    p._api_post = AsyncMock(side_effect=[4242, {"finished": None}, {"finished": "now", "exit_code": 3010}])
    code = asyncio.run(p._run_guest_ps(MagicMock(), "vm-7", "exit 3010"))
    assert code == 3010
    create = p._api_post.await_args_list[0]
    assert create.args[1] == "/vcenter/vm/vm-7/guest/processes?action=create"
    spec = create.kwargs["json"]["spec"]
    assert spec["path"].endswith("powershell.exe")
    assert _decode(spec["arguments"].rsplit(" ", 1)[1]) == "exit 3010"
    assert p._api_post.await_args_list[1].args[1] == "/vcenter/vm/vm-7/guest/processes/4242?action=get"


def _provision_one(prov, vm_def, *, tools=True, grow=None):
    prov._find_library_item = AsyncMock(return_value="lib-1")
    prov._deploy_ovf = AsyncMock(return_value="vm-9")
    prov._api_patch = AsyncMock()
    prov._power_action = AsyncMock()
    prov._wait_tools = AsyncMock(return_value=tools)
    prov._get_vm_ip = AsyncMock(return_value="10.1.0.5")
    prov._grow_disk_sync = MagicMock(side_effect=grow) if grow else MagicMock(return_value=True)
    return asyncio.run(prov._provision_one_vm(MagicMock(), vm_def, "r-dc01", "srv2022", "f", "rp", "ds"))


def test_disk_is_grown_before_power_on(prov):
    out = _provision_one(prov, {"name": "web", "cores": 2, "memory": 4096, "disk_gb": 120})
    prov._grow_disk_sync.assert_called_once_with("vm-9", 120)
    assert "errors" not in out


def test_disk_and_role_problems_come_back_as_errors(prov):
    prov._run_guest_ps.return_value = 1
    out = _provision_one(prov, {**_dc(ad_forest_root=True), "disk_gb": 100}, grow=RuntimeError("no pyvmomi"))
    assert any("disk not resized" in e for e in out["errors"])
    assert any(e.startswith("role ad-ds: failed") for e in out["errors"])
    assert out["roles"]["dns"]["status"] == "failed"


def test_roles_are_skipped_when_tools_never_start(prov):
    out = _provision_one(prov, _dc(ad_forest_root=True), tools=False)
    assert {v["status"] for v in out["roles"].values()} == {"skipped"}
    prov._run_guest_ps.assert_not_awaited()
