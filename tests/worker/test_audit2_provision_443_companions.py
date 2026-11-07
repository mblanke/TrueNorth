"""Companions for the audit #443 oracle tests (card #470, item 2).

One companion per REAL oracle in ``test_audit2_provision_443.py``: the same secure
behaviour pinned from a second angle (two-tenant matrix, second role or boundary value),
marked ``xfail(strict=True)`` so it fails at the base commit and turns into an XPASS error
once the finding is fixed. ``audit443`` is deliberately not registered in pytest.ini
(protected): its PytestUnknownMarkWarning is expected.

Everything here runs against fakes (httpx.MockTransport, fake pyVmomi objects); no vCenter.
"""

from __future__ import annotations

import pytest
import test_vsphere_provision as _vsp
from test_vsphere_provision import RANGE_ID, FakeVM, _prov, _run
from worker.provisioners import vsphere_api as mod

vc = _vsp.vc  # the fake vCenter fixture
R8 = RANGE_ID[:8]
# A second tenant (range) in the same vCenter, with its own recorded connection.
RANGE_B = "99998888-7777-6666-5555-444433332222"
R8_B = RANGE_B[:8]


def _tagged(vc, vm_id: str, range_id: str | None, name: str | None = None) -> FakeVM:
    """A VM in the fake vCenter carrying the range's annotation (the helper of
    test_vsphere_connection_binding, kept local so this companion file stands alone)."""
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
                                       "provisioners/vsphere_api.py:3211) — companion")
def test_two_tenants_re_registered_vm_credited_while_the_other_tenant_vm_is_left(vc):
    """Two-tenant matrix for the re-registered-VM finding: two ranges share one vCenter,
    each having recorded the connection that built it. Range A's VM was unregistered and
    re-registered under a new moid (still powered on, still carrying range A's annotation);
    range B's VM runs untouched alongside it. Destroying range A must not credit its absent
    moid while the re-registered VM still exists, and a fix that goes looking for that VM
    (by the range's own mark) must not touch the other tenant's VM."""
    _tagged(vc, "vm-A-new", RANGE_ID, f"{R8}-web01")  # range A: re-registered, new moid, still powered on
    _tagged(vc, "vm-B", RANGE_B, f"{R8_B}-web01")  # range B: another tenant in the same vCenter
    prov_a = _prov(vc)
    out_a = {"vms": [{"name": f"{R8}-web01", "vm_id": "vm-404"}], "connection": prov_a.connection_record()}
    result_a = _run(prov_a.destroy(RANGE_ID, out_a))
    # Today: nothing deletes vm-A-new (only vm-404 is recorded), yet the absent moid alone
    # makes resources_removed == 1 and the destroy "ok". Fails on this assertion, the same
    # false credit the oracle pins.
    assert "vm-A-new" not in vc.vms or result_a.resources_removed == 0, (
        f"range A's VM is still registered in this vCenter (moid vm-A-new) but its destroy "
        f"counted {result_a.resources_removed} resource(s) removed with status {result_a.status!r}")
    # And range B's VM, never recorded by range A, survives the whole teardown.
    assert "vm-B" in vc.vms, (
        "range A's destroy touched range B's VM (vm-B): the re-registered VM must be found "
        "by range A's own mark, not by anything broader")
