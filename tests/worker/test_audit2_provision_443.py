"""Audit #443 oracle tests (card TN10X-AUDIT2-tests-provision): the vSphere destroy path.

One strict-xfail test per audit finding reproduced at the base commit (d5851f6d). Each
test pins the secure behaviour and fails today, so it is marked
``xfail(strict=True, reason="audit #443: <finding> (<file:line>)")``; when the finding is
fixed the xfail turns into an XPASS error and the mark must be removed with the fix.
``audit443`` is deliberately not registered in pytest.ini (protected): its
PytestUnknownMarkWarning is expected.

Everything here runs against fakes (httpx.MockTransport, fake pyVmomi objects); no vCenter.
"""

from __future__ import annotations

import pytest
import test_vsphere_provision as _vsp
from test_vsphere_provision import RANGE_ID, FakeVM, _prov, _run
from worker.provisioners import vsphere_api as mod

vc = _vsp.vc  # the fake vCenter fixture
R8 = RANGE_ID[:8]


def _tagged(vc, vm_id: str, range_id: str | None, name: str | None = None) -> FakeVM:
    """A VM in the fake vCenter carrying the range's annotation (the helper of
    test_vsphere_connection_binding, kept local so this oracle file stands alone)."""
    vm = FakeVM(name or vm_id, [])
    vm._moId = vm_id
    if range_id:
        vm.config.annotation = mod.range_annotation(range_id)
    vc.vms[vm._moId] = vm
    vc.power[vm._moId] = "POWERED_ON"
    return vm


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: a recorded VM that vCenter no longer has under its moid is "
                                       "counted as deleted when a connection is recorded, even though it may have "
                                       "been re-registered and still run (control-plane/worker/worker/"
                                       "provisioners/vsphere_api.py:3211)")
def test_a_re_registered_vm_still_running_is_counted_as_deleted(vc):
    """A recorded moid vCenter lacks is counted as deleted ("gone counts as deleted",
    vsphere_api.py:3211) as soon as the range's connection is recorded, which also skips
    the wrong-vCenter refusal for an all-missing range (vsphere_api.py:3187). But absent
    under the old moid is not gone: the VM was unregistered and re-registered, so it runs
    in this same vCenter under a new moid, still carrying the range's annotation. Secure
    behaviour: a destroy may not count a resource as removed that is still registered in
    the vCenter it is tearing down -- either the re-registered VM is found (by instance
    UUID or mark) and really deleted, or the destroy holds and reports it; either way
    ``resources_removed`` may not credit the absent moid while the VM still exists."""
    _tagged(vc, "vm-new", RANGE_ID, f"{R8}-x")  # re-registered: a new moid, still powered on
    prov = _prov(vc)
    out = {"vms": [{"name": f"{R8}-x", "vm_id": "vm-404"}], "connection": prov.connection_record()}
    result = _run(prov.destroy(RANGE_ID, out))
    # Today: nothing deletes vm-new (only vm-404 is recorded), yet the absent moid alone
    # makes resources_removed == 1 and the destroy "ok". Fails on this assertion.
    assert "vm-new" not in vc.vms or result.resources_removed == 0, (
        f"the VM is still registered in this vCenter (moid vm-new) but the destroy counted "
        f"{result.resources_removed} resource(s) removed with status {result.status!r}")
