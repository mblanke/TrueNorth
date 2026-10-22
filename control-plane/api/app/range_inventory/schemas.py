"""Request and response shapes of the range inventory endpoints (docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["pass", "fail", "incomplete", "error"]


class InventoryItemOut(BaseModel):
    """One item a teardown report names: a VM, a disk file, a port group or a folder."""

    kind: str = Field(description="vm | disk | portgroup | folder")
    moid: str | None = Field(None, description="vCenter managed-object id (unidentified items)")
    id: str | None = Field(None, description="The recorded id (a moid, or a disk file's path)")
    name: str = ""
    path: str | None = Field(None, description="A disk file: [datastore] dir/file.vmdk")
    datastore: str | None = None
    instance_uuid: str | None = None
    key: str | None = Field(None, description="A port group's key")
    vlan: int | None = None
    marks: list[str] = Field(default_factory=list,
                             description="What ties an unidentified item to the range: annotation, tag, owner_mark")
    why: str | None = None


class SkippedItemOut(BaseModel):
    moid: str | None = None
    why: str


class TeardownCheckOut(BaseModel):
    status: CheckStatus
    summary: str
    checked_at: datetime
    changed_at: datetime
    verified_at: str | None = None
    legacy: bool = Field(False, description="Built before inventory recording: VMs matched by annotation and name")
    gaps: list[str] = Field(default_factory=list)
    deleted: list[InventoryItemOut] = Field(default_factory=list, description="Deleted by recorded id")
    remaining: list[InventoryItemOut] = Field(default_factory=list, description="Recorded ids still present")
    unidentified: list[InventoryItemOut] = Field(
        default_factory=list, description="Carry the range's mark but are not recorded: listed, not deleted")
    not_checked: list[str] = Field(default_factory=list)
    confirmed_deleted: list[InventoryItemOut] = Field(
        default_factory=list, description="Unidentified items an administrator confirmed by moid, then deleted")
    confirm_skipped: list[SkippedItemOut] = Field(default_factory=list)


class DriftFindingOut(BaseModel):
    kind: str = Field(description="vm_missing, vm_replaced, cpu_changed, memory_changed, nic_portgroup_changed, "
                                  "nic_mac_changed, nic_added, nic_removed, portgroup_missing, "
                                  "portgroup_vlan_changed, snapshot_unexpected, snapshot_missing")
    object: str
    expected: Any = None
    actual: Any = None
    name: str | None = None
    moid: str | None = None


class DriftCheckOut(BaseModel):
    status: CheckStatus
    summary: str
    checked_at: datetime
    changed_at: datetime
    findings: list[DriftFindingOut] = Field(default_factory=list)
    checked: dict[str, int] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    error: str | None = None


class RangeInventoryChecksOut(BaseModel):
    range_id: uuid.UUID
    teardown: TeardownCheckOut | None = None
    drift: DriftCheckOut | None = None
    can_confirm: bool = Field(description="Whether the caller may confirm unidentified items (range:admin) now")
    confirm_blocked: str | None = Field(None, description="Why confirming is not possible now, if it is not")


class ConfirmItemIn(BaseModel):
    moid: str = Field(..., min_length=1, max_length=80, description="An unidentified item's moid, as listed")
    confirm_moid: str = Field(..., min_length=1, max_length=80,
                              description="The same moid, typed back by the administrator; must match exactly")


class ConfirmUnidentifiedIn(BaseModel):
    items: list[ConfirmItemIn] = Field(..., min_length=1, max_length=50)
    reason: str = Field(..., min_length=10, max_length=500)


class ConfirmUnidentifiedOut(BaseModel):
    range_id: uuid.UUID
    accepted: list[InventoryItemOut]
    task_id: str | None = None
    dispatched: bool
    message: str
