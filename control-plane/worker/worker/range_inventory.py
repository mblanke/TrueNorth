"""A range's independently identified inventory, and what is compared with it (ADR 0013 §5,
range certification S5, issue #243). Pure functions over dicts: no database, no SDK.

**Recorded at clone.** A backend that supports it (vSphere: ``vsphere_inventory.py``)
records, in the range's ``provisioner_output``:

* per VM, ``vms[].identity``: managed-object id (``moid``), ``instance_uuid``, the VMX path,
  the datastores and every disk file (``disks[]``: datastore moid + ``[ds] dir/file.vmdk``),
  the folder moid, CPU and memory, and each NIC's port group key and MAC;
* per range network, ``networks[].moid`` / ``key``: the port group TrueNorth created;
* ``inventory``: the range folder (moid, name), the VLAN and MAC reservations, and when.

Snapshot ids are recorded as each snapshot is taken (``range_snapshots.snapshot_data``
``snapshot_ids``: VM moid -> snapshot moid).

**Teardown** deletes only what the record identifies (moid *and* instance UUID); anything
else carrying the range's annotation, ``tn-range`` tag or port group owner mark is listed
as *unidentified* and left alone. Afterwards the recorded ids are looked for again. The
backend returns a report (``DestroyResult.teardown``); ``teardown_status`` grades it:

=============  ===============================================================
``pass``       every recorded VM, disk file, port group and the folder is gone,
               nothing unidentified carries the range's mark, and the record was
               complete (every VM had an instance UUID)
``fail``       a recorded id is still there (a VM, a disk file left on its
               datastore, a port group, the folder)
``incomplete`` none remains, but something carrying the range's mark was not
               deleted (unidentified, waiting for an admin), something could not
               be checked, or the record predates S5 (legacy)
=============  ===============================================================

**Drift** (``compare``): live VM ids, CPU/RAM, NIC port groups and MACs, port group VLANs
and snapshots against the record. A change that can break isolation or identity is
``fail``; a resource or snapshot change is ``incomplete`` (``drift_status``).
"""

from __future__ import annotations

from collections.abc import Iterable

PASS, FAIL, INCOMPLETE = "pass", "fail", "incomplete"

# Drift findings that can break a range's isolation or the identity of what it runs.
FAIL_KINDS = frozenset({
    "vm_replaced", "nic_portgroup_changed", "nic_mac_changed", "nic_added", "nic_removed",
    "portgroup_missing", "portgroup_vlan_changed",
})
INCOMPLETE_KINDS = frozenset({
    "vm_missing", "cpu_changed", "memory_changed", "snapshot_unexpected", "snapshot_missing",
})


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def recorded(output: dict) -> dict:
    """The inventory the output records, normalised:
    ``{"vms", "portgroups", "folder", "complete", "gaps"}``. ``complete`` is False for a range
    built before S5 (no identity, or no instance UUID: ownership then falls back to the
    annotation/name rule) and says why in ``gaps``."""
    output = _dict(output)
    vms, gaps = [], []
    for vm in _list(output.get("vms")):
        if not isinstance(vm, dict) or not vm.get("vm_id"):
            continue
        ident = _dict(vm.get("identity"))
        if not ident.get("instance_uuid"):
            gaps.append(f"VM {vm.get('name') or vm['vm_id']}: no instance UUID recorded at clone")
        vms.append({
            "moid": str(vm["vm_id"]), "name": str(vm.get("name") or ""),
            "instance_uuid": ident.get("instance_uuid") or None,
            "disks": [d for d in _list(ident.get("disks")) if isinstance(d, dict) and d.get("path")],
            "datastores": [str(d) for d in _list(ident.get("datastores"))],
            "folder": ident.get("folder") or None,
            "num_cpu": ident.get("num_cpu"), "memory_mb": ident.get("memory_mb"),
            "nics": [n for n in _list(ident.get("nics")) if isinstance(n, dict)],
            "identified": bool(ident.get("instance_uuid")),
        })
    portgroups = []
    for net in _list(output.get("networks")):
        if not isinstance(net, dict) or not net.get("portgroup"):
            continue
        if not net.get("moid"):
            gaps.append(f"port group {net['portgroup']}: no managed-object id recorded")
        portgroups.append({"name": str(net["portgroup"]), "moid": net.get("moid") or None, "key": net.get("key"),
                           "vlan": net.get("physical_vlan"), "switch_mode": net.get("switch_mode")})
    inventory = _dict(output.get("inventory"))
    folder = _dict(inventory.get("folder")) or None
    if vms and folder is None:
        gaps.append("range folder: no managed-object id recorded")
    return {"vms": vms, "portgroups": portgroups, "folder": folder, "complete": not gaps, "gaps": gaps}


def vm_owned(record_vm: dict, live_uuid: str | None) -> bool | None:
    """Whether what vCenter holds under a recorded moid is that VM, by its instance UUID.
    None: no UUID was recorded, so the caller's older rule (annotation, name) decides."""
    if not record_vm.get("instance_uuid"):
        return None
    return bool(live_uuid) and str(live_uuid).lower() == str(record_vm["instance_uuid"]).lower()


# ── teardown ────────────────────────────────────────────────────────────────
def teardown_status(report: dict | None) -> tuple[str, str]:
    """(status, summary) of a backend's teardown report (module docstring)."""
    if not report:
        return INCOMPLETE, "the backend did not verify its teardown against a recorded inventory"
    remaining = _list(report.get("remaining"))
    unidentified = _list(report.get("unidentified"))
    not_checked = _list(report.get("not_checked"))
    if remaining:
        kinds = ", ".join(sorted({f"{r.get('kind')} {r.get('id') or r.get('path') or ''}".strip()
                                  for r in remaining}))[:300]
        return FAIL, f"{len(remaining)} recorded item(s) still present after teardown: {kinds}"
    notes = []
    if unidentified:
        notes.append(f"{len(unidentified)} unidentified item(s) carry this range's mark and were not deleted "
                     "(an administrator may confirm them by managed-object id)")
    if not_checked:
        notes.append(f"{len(not_checked)} check(s) could not run")
    if report.get("legacy"):
        notes.append("the range was built before inventory recording; VMs were matched by annotation and name")
    if notes:
        return INCOMPLETE, "zero recorded ids remain; " + "; ".join(notes)
    return PASS, "zero recorded ids remain (VMs, disk files, port groups, folder); nothing unidentified"


# ── drift ───────────────────────────────────────────────────────────────────
def _finding(kind: str, obj: str, expected=None, actual=None, **extra) -> dict:
    out = {"kind": kind, "object": obj}
    if expected is not None or actual is not None:
        out.update(expected=expected, actual=actual)
    out.update(extra)
    return out


def _mac(value) -> str:
    return str(value or "").lower()


def known_snapshots(rows: Iterable[dict]) -> tuple[set[str], set[str], dict[str, set[str]]]:
    """From the range's snapshot rows (``{"state", "data", "backend_name"}``, data the parsed snapshot_data):
    (snapshot moids TrueNorth took, their backend names, VM moid -> moids that must exist).
    Rows that are gone or failed count for nothing; a ``creating`` or older row (no ids
    recorded) is known by its name only."""
    ids, names, expected = set(), set(), {}
    for row in rows:
        state, data = row.get("state"), _dict(row.get("data"))
        if state in ("deleted", "failed"):
            continue
        if data.get("snapshot_name"):
            names.add(str(data["snapshot_name"]))
        if row.get("backend_name"):  # the name the snapshot task gives it (tasks._backend_snapshot_name)
            names.add(str(row["backend_name"]))
        for vm_id, snap_id in _dict(data.get("snapshot_ids")).items():
            if snap_id:
                ids.add(str(snap_id))
                if state in ("ready", "restoring"):
                    expected.setdefault(str(vm_id), set()).add(str(snap_id))
    return ids, names, expected


def compare(output: dict, live: dict, snapshot_rows: Iterable[dict] = ()) -> dict:
    """Drift of the live range from its record.

    ``live`` (the backend's ``live_inventory``): ``{"vms": {moid: {"exists", "instance_uuid",
    "name", "num_cpu", "memory_mb", "nics": [{"portgroup_key", "network", "mac"}],
    "snapshots": [{"moid", "name"}]}}, "portgroups": {moid: {"exists", "name", "vlan"}},
    "errors": [...]}``. Returns ``{"status", "summary", "findings", "checked", "gaps"}``,
    findings sorted so an unchanged range gives an identical result every run."""
    inv = recorded(output)
    live_vms, live_pgs = _dict(live.get("vms")), _dict(live.get("portgroups"))
    ids, names, expected = known_snapshots(snapshot_rows)
    findings: list[dict] = []
    checked = {"vms": 0, "portgroups": 0}
    for vm in inv["vms"]:
        seen = live_vms.get(vm["moid"])
        if seen is None:
            continue  # not read this run (an error says why)
        label = vm["name"] or vm["moid"]
        checked["vms"] += 1
        if not seen.get("exists"):
            findings.append(_finding("vm_missing", label, vm["moid"], None))
            continue
        if vm_owned(vm, seen.get("instance_uuid")) is False:
            findings.append(_finding("vm_replaced", label, vm["instance_uuid"], seen.get("instance_uuid"),
                                     moid=vm["moid"]))
            continue
        if not vm["identified"]:
            continue  # built before S5: nothing recorded to compare its hardware with
        for field, kind in (("num_cpu", "cpu_changed"), ("memory_mb", "memory_changed")):
            if vm.get(field) is not None and seen.get(field) != vm[field]:
                findings.append(_finding(kind, label, vm[field], seen.get(field)))
        want, have = vm["nics"], _list(seen.get("nics"))
        for i in range(max(len(want), len(have))):
            nic = f"{label} NIC {i}"
            if i >= len(have):
                findings.append(_finding("nic_removed", nic, want[i].get("mac"), None))
            elif i >= len(want):
                findings.append(_finding("nic_added", nic, None, have[i].get("mac")))
            else:
                w, h = want[i], have[i]
                if (w.get("portgroup_key") or w.get("network")) != (h.get("portgroup_key") or h.get("network")):
                    findings.append(_finding("nic_portgroup_changed", nic, w.get("portgroup_key") or w.get("network"),
                                             h.get("portgroup_key") or h.get("network")))
                if _mac(w.get("mac")) != _mac(h.get("mac")):
                    findings.append(_finding("nic_mac_changed", nic, _mac(w.get("mac")), _mac(h.get("mac"))))
        snaps = [s for s in _list(seen.get("snapshots")) if isinstance(s, dict)]
        live_ids = {str(s.get("moid")) for s in snaps}
        for s in snaps:
            if str(s.get("moid")) not in ids and str(s.get("name")) not in names:
                findings.append(_finding("snapshot_unexpected", label, None, s.get("moid"), name=s.get("name")))
        for snap_id in sorted(expected.get(vm["moid"], set()) - live_ids):
            findings.append(_finding("snapshot_missing", label, snap_id, None))
    for pg in inv["portgroups"]:
        seen = live_pgs.get(pg["moid"]) if pg["moid"] else None
        if seen is None:
            continue
        checked["portgroups"] += 1
        if not seen.get("exists"):
            findings.append(_finding("portgroup_missing", pg["name"], pg["moid"], None))
        elif pg.get("vlan") is not None and seen.get("vlan") != pg["vlan"]:
            findings.append(_finding("portgroup_vlan_changed", pg["name"], pg["vlan"], seen.get("vlan")))
    findings.sort(key=lambda f: (f["kind"], str(f["object"]), str(f.get("expected")), str(f.get("actual"))))
    status, summary = drift_status(findings, inv["gaps"])
    return {"status": status, "summary": summary, "findings": findings, "checked": checked, "gaps": inv["gaps"]}


def drift_status(findings: list[dict], gaps: list[str] = ()) -> tuple[str, str]:
    """``fail`` for any isolation/identity change, else ``incomplete`` for any other change or
    a record too old to compare in full, else ``pass``."""
    kinds = sorted({f["kind"] for f in findings})
    if any(k in FAIL_KINDS for k in kinds):
        return FAIL, f"{len(findings)} change(s) from the record, incl. {', '.join(k for k in kinds if k in FAIL_KINDS)}"
    if findings:
        return INCOMPLETE, f"{len(findings)} change(s) from the record: {', '.join(kinds)}"
    if gaps:
        return INCOMPLETE, f"no drift in what was recorded; {len(gaps)} item(s) were not recorded at clone (built before S5)"
    return PASS, "the range matches its record (VM ids, CPU/RAM, NICs, port group VLANs, snapshots)"
