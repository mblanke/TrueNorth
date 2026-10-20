"""A vCenter's VMs with TrueNorth's tn-* tags, for the Infrastructure page (plain httpx).

Read only. The tags are the ones the worker's provisioner (range VMs) and the template kit
(golden templates, host copies, test clones; scripts/lab/vcenter_organize.py) apply:
``tn-type``, ``tn-os``, ``tn-template``, ``tn-range``. Read through the vSphere Automation
(CIS) tagging API in the session's dialect:

    /api/cis/tagging/category, .../category/{id}                       (vSphere 7.0U2+)
    POST /api/cis/tagging/tag?action=list-tags-for-category {category_id}
    GET  /api/cis/tagging/tag/{id}
    POST /api/cis/tagging/tag-association?action=list-attached-objects-on-tags {tag_ids}
    /rest/com/vmware/cis/tagging/... with id:{id} and ~action          (legacy, vcsim)

VMs come from ``/vcenter/vm`` per host (all power states, one host at a time so a large
cluster stays under the list call's 4000-VM cap). A tagged VM that listing does not return
(an inventory template, where a vCenter leaves templates out) is read by id; one that cannot
be read is still shown, by id.

Nothing here counts VMs: the hosts' running-VM counts stay discovery's (templates are never
counted there, vsphere.discover).
"""

from __future__ import annotations

import httpx

CATEGORIES = ("tn-type", "tn-os", "tn-template", "tn-range")

_BASE = {"api": "/cis/tagging", "rest": "/com/vmware/cis/tagging"}


def _id(dialect: str, value: str) -> str:
    return f"id:{value}" if dialect == "rest" else value


def _action(dialect: str, name: str) -> str:
    return f"~action={name}" if dialect == "rest" else f"action={name}"


def read_tags(s) -> dict[str, dict[str, str]]:
    """VM id -> {tn category: tag name}. Categories that do not exist yet are simply absent."""
    d, base = s.dialect, _BASE[s.dialect]
    names: dict[str, tuple[str, str]] = {}  # tag id -> (category, tag name)
    for cid in s.get(f"{base}/category") or []:
        cat = (s.get(f"{base}/category/{_id(d, cid)}") or {}).get("name")
        if cat not in CATEGORIES:
            continue
        if d == "rest":
            tag_ids = s.post(f"{base}/tag/{_id(d, cid)}?{_action(d, 'list-tags-for-category')}")
        else:
            tag_ids = s.post(f"{base}/tag?{_action(d, 'list-tags-for-category')}", json={"category_id": cid})
        for tid in tag_ids or []:
            tag = s.get(f"{base}/tag/{_id(d, tid)}") or {}
            if tag.get("name"):
                names[tid] = (cat, tag["name"])
    out: dict[str, dict[str, str]] = {}
    if not names:
        return out
    attached = s.post(f"{base}/tag-association?{_action(d, 'list-attached-objects-on-tags')}",
                      json={"tag_ids": sorted(names)}) or []
    for row in attached:
        cat, name = names.get(row.get("tag_id"), (None, None))
        for obj in row.get("object_ids") or []:
            if cat and obj.get("type") == "VirtualMachine":
                out.setdefault(obj["id"], {})[cat] = name
    return out


def list_vms(s, host_filter: str) -> tuple[list[dict], str]:
    """``([{vm_id, name, power_state, host, tags}], note)``; ``note`` says what could not be read."""
    notes: list[str] = []
    vms: dict[str, dict] = {}
    for h in s.get("/vcenter/host") or []:
        try:
            listed = s.get("/vcenter/vm", params={host_filter: h["host"]})
        except httpx.HTTPError as exc:
            notes.append(f"VMs on {h.get('name', h.get('host'))} not read ({exc.__class__.__name__})")
            continue
        for v in listed or []:
            vms[v["vm"]] = {"vm_id": v["vm"], "name": v.get("name") or v["vm"],
                            "power_state": v.get("power_state"), "host": h.get("name")}
    try:
        tags = read_tags(s)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        tags = {}
        notes.append(f"tags not read ({exc.__class__.__name__}): every VM is shown as Other")
    for vm_id in sorted(set(tags) - set(vms)):
        row = {"vm_id": vm_id, "name": vm_id, "power_state": None, "host": None}
        try:
            info = s.get(f"/vcenter/vm/{vm_id}") or {}
            row.update(name=info.get("name") or vm_id, power_state=info.get("power_state"))
        except httpx.HTTPError:
            pass
        vms[vm_id] = row
    for vm_id, row in vms.items():
        row["tags"] = tags.get(vm_id, {})
    return sorted(vms.values(), key=lambda r: str(r["name"]).lower()), "; ".join(notes)
