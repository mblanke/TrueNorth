"""A noise-enabled range is refused on vSphere until its management NIC is ported.

Independent review of the candidate (2026-10-05): render.py adds a `noise_mgmt` network and
a `mgmt` NIC per agent VM (#29), but the vSphere provisioner from #33 never reads `mgmt`.
A noisy range came up `ready` with no management NIC, so its agents could never be
reached, and nothing said so. Until the NIC is ported (handoff-2026-10-05.md §1), the
provisioner refuses such a range at once, before touching vCenter.
"""

from __future__ import annotations

import asyncio

import pytest
from worker.fencing import PermanentError
from worker.provisioners.vsphere_api import VsphereAPIProvisioner


def test_a_range_with_noise_management_nics_is_refused_before_vcenter_is_touched():
    prov = VsphereAPIProvisioner.__new__(VsphereAPIProvisioner)  # no connection: none must be made
    template = {"vms": [{"name": "r-lnx01", "mgmt": {"vlan_id": 4001, "ip": "10.255.0.10", "prefix": 24}}]}
    with pytest.raises(PermanentError, match="noise"):
        asyncio.run(prov.provision("r1", template, {}))
