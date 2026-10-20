"""The Infrastructure page's live VM list (``GET /hypervisors/connections/{id}/vms``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

VmGroup = Literal["golden", "copies", "ranges", "other"]


class InventoryVmOut(BaseModel):
    vm_id: str
    name: str
    power_state: str | None = None
    host: str | None = None
    group: VmGroup = Field(description="golden: tn-type golden-template; copies: template-copy; "
                                       "ranges: range-vm; other: anything else (test clones, depot, untagged)")
    tags: dict[str, str] = Field(default_factory=dict, description="TrueNorth's tn-* vCenter tags: category -> value")


class VmInventoryOut(BaseModel):
    supported: bool = Field(description="False: this hypervisor type cannot list its VMs here")
    message: str = ""
    groups: dict[VmGroup, int] = Field(default_factory=dict, description="VMs per group")
    vms: list[InventoryVmOut] = Field(default_factory=list)
