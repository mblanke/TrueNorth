"""Build, render, write and verify the compiled plan for a range."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from . import __version__
from .allocate import address_instance, assign_slots, save_registry
from .common import (
    RangeError,
    find_tbd,
    instance_ids,
    load_json,
    load_yaml,
    now_utc,
    sha256_obj,
    to_date,
    write_json,
)
from .compose import compose
from .schema import template_blockers, validate_range_doc, validate_site

# Not part of the plan hash: timestamps, advisory text, and buildability (which can
# change with the calendar, e.g. expiry, without the plan itself changing).
VOLATILE = ("compiled_at", "plan_sha256", "warnings", "blockers", "buildable")


def range_path(root: Path, ref: str) -> Path:
    p = Path(ref)
    if p.suffix in (".yml", ".yaml") and p.exists():
        return p.resolve()
    return root / "ranges" / f"{ref}.yml"


def load_context(root: Path) -> tuple[dict, dict]:
    site_doc = load_yaml(root / "config" / "site.yml")
    tmpl_doc = load_yaml(root / "config" / "templates.yml")
    return site_doc, (tmpl_doc.get("templates", {}) or {})


def build_plan(root: Path, ref: str, *, write_allocation: bool = False) -> dict[str, Any]:
    path = range_path(root, ref)
    doc = load_yaml(path)
    site_doc, templates = load_context(root)
    site_errs, site_tbd = validate_site(site_doc)
    if site_errs:
        raise RangeError(site_errs)
    site = site_doc["site"]

    errs = validate_range_doc(doc, max_students=site.get("max_students", 40))
    if errs:
        raise RangeError(errs)
    rid = doc["range"]["id"]
    if path.stem != rid:
        raise RangeError(f"{path.name}: file name must match range.id '{rid}'")

    resolved = compose(doc, root)
    blockers: list[str] = [f"{f} is TBD" for f in site_tbd]

    used_templates = sorted({vm["template"] for vm in resolved["vms"]})
    tmpl_info = {}
    for alias in used_templates:
        entry = templates.get(alias)
        if entry is None:
            raise RangeError(f"template alias '{alias}' is not in config/templates.yml "
                             f"(known: {', '.join(sorted(templates))})")
        blockers.extend(template_blockers(alias, entry))
        tmpl_info[alias] = {k: entry.get(k) for k in (
            "vcenter_name", "os_family", "os", "build_id", "build_date",
            "source_sha256", "validated", "guest_user_var")}
        tmpl_info[alias]["nic_device_type"] = entry.get("nic_device_type", "vmxnet3")

    exp = to_date(resolved["range"]["expires"])
    if exp and exp < _dt.date.today():
        blockers.append(f"range expired on {exp}; update range.expires to rebuild")
    if resolved["range"]["owner"] in (None, "", "TBD"):
        blockers.append("range.owner is TBD")

    ids = instance_ids(rid, resolved["range"]["students"])
    slots, warnings, pending_registry = assign_slots(root, site, ids, write=write_allocation)
    if any("proposed" in w for w in warnings):
        blockers.append("slot allocation not recorded (compile with --write-allocation)")

    vc = site["vcenter"]
    probe_alias = next(v["template"] for v in resolved["vms"]
                       if v["name"] == resolved["validation"]["probe_vm"])
    if tmpl_info[probe_alias].get("os_family") != "linux":
        raise RangeError(f"validation.probe_vm uses template '{probe_alias}'; probes run bash, "
                         f"so the probe must be a linux template")

    # Folder chain (created top-down, removed bottom-up) and resource pools.
    root_parts = [p for p in str(vc.get("folder_root", "")).split("/") if p]
    folders: list[dict] = []

    def add_folder(parts: list[str]) -> str:
        for i in range(len(parts)):
            f = {"parent": "/".join(parts[:i]), "name": parts[i], "path": "/".join(parts[: i + 1]),
                 "owned": i >= len(root_parts)}
            if f not in folders:
                folders.append(f)
        return "/".join(parts)

    range_pool = f"{vc.get('resource_pool_root')}-{rid}"
    instances = []
    for iid in ids:
        addr = address_instance(site, resolved, iid, slots[iid])
        for n in addr["networks"]:
            if n["gateway"] and not n["has_router"]:
                warnings.append(f"{iid}: network {n['name']} has a gateway (.1) but no VM holds it")
        parts = root_parts + [rid] + ([iid] if iid != rid else [])
        folder = add_folder(parts)
        obj_tags = [{"name": rid, "category_name": site["tags"]["range_category"]},
                    {"name": iid, "category_name": site["tags"]["instance_category"]}]
        for n in addr["networks"]:
            n["tags"] = obj_tags
        for vm in addr["vms"]:
            vm["tags"] = obj_tags
            t = tmpl_info[vm["template_alias"]]
            if t.get("os_family") == "windows" and len(vm["name"]) > 15:
                raise RangeError(f"vm '{vm['name']}': Windows hostnames are limited to 15 characters "
                                 f"(shorten the 'as' alias or VM name)")
            vm["template"] = t["vcenter_name"]
            vm["os_family"] = t["os_family"]
            vm["guest_user_var"] = t["guest_user_var"]
            vm["instance"] = iid
            vm["folder"] = folder
            vm["resource_pool"] = range_pool
            vm["vmware_networks"] = [{"name": n["portgroup"], "device_type": t["nic_device_type"],
                                      "start_connected": True, "connected": True} for n in vm["nics"]]
            # Static part of the in-guest environment. MAC addresses are appended at
            # run time (TN_NIC_<n>=<mac>|<ip>/<prefix>|<gateway>).
            vm["guest_env"] = ([f"TN_HOSTNAME={vm['name']}", f"TN_ROLE={vm['role']}",
                                f"TN_INSTANCE={iid}", f"TN_NIC_COUNT={len(vm['nics'])}"]
                               + [f"TN_VAR_{k.upper()}={v}" for k, v in sorted(vm["vars"].items())])
        probes = _probes(site, resolved, addr, iid)
        instances.append({
            "id": iid,
            "probes": probes,
            "slot": slots[iid],
            "cidr_block": addr["block"],
            "folder": folder,
            "resource_pool": range_pool,
            "drs_rule": f"TN-{iid}-affinity",
            "networks": [dict(n, instance=iid) for n in addr["networks"]],
            "vms": addr["vms"],
            "probe_vm": f"{iid}-{resolved['validation']['probe_vm']}",
        })

    totals = {
        "instances": len(instances),
        "networks": sum(len(i["networks"]) for i in instances),
        "vms": sum(len(i["vms"]) for i in instances),
        "vcpu": sum(v["cpu"] for i in instances for v in i["vms"]),
        "memory_gb": sum(v["memory_mb"] for i in instances for v in i["vms"]) // 1024,
        "disk_gb": sum(v["disk_gb"] for i in instances for v in i["vms"]),
    }
    headroom = site.get("capacity_headroom_pct", 20)
    totals["disk_required_gb"] = int(totals["disk_gb"] * (1 + headroom / 100))

    site_out = {
        "datacenter": vc.get("datacenter"),
        "cluster": vc.get("cluster"),
        "dvs": vc.get("dvs"),
        "datastores_allowed": vc.get("datastores_allowed"),
        "folder_root": vc.get("folder_root"),
        "resource_pool_root": vc.get("resource_pool_root"),
        "tags": site.get("tags"),
        "isolation_mode": site.get("isolation_mode"),
        "isolation_probe_targets": site.get("isolation_probe_targets", []),
        "reserved_vlans": site.get("reserved_vlans", []),
        "clone_mode": site.get("clone_mode", "full"),
        "clone_parallelism": site.get("clone_parallelism", 6),
        "validate_certs": site.get("validate_certs", True),
        "capacity_headroom_pct": headroom,
    }

    source = {
        "range_doc": doc,
        "blueprints": resolved["blueprints"],
        "site": site,
        "templates": tmpl_info,
        "slots": slots,
    }

    plan = {
        "plan_version": 1,
        "compiler_version": __version__,
        "range_id": rid,
        "range": resolved["range"],
        "source_file": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
        "source_sha256": sha256_obj(source),
        "site": site_out,
        "templates": tmpl_info,
        "folders": folders,
        "resource_pools": [{"name": str(vc.get("resource_pool_root")), "parent": None, "owned": False},
                           {"name": range_pool, "parent": str(vc.get("resource_pool_root")), "owned": True}],
        "vsphere_tags": [{"name": rid, "category_name": site["tags"]["range_category"],
                          "description": f"TrueNorth range {rid}"}]
                        + [{"name": i["id"], "category_name": site["tags"]["instance_category"],
                            "description": f"TrueNorth range instance {i['id']}"} for i in instances],
        "instances": instances,
        "validation": resolved["validation"],
        "integrations": resolved["integrations"],
        "power_after_build": resolved["power_after_build"],
        "source_scenario": resolved["source_scenario"],
        "totals": totals,
        "blockers": sorted(set(blockers)),
        "buildable": not blockers,
    }
    plan["plan_sha256"] = sha256_obj({k: v for k, v in plan.items() if k not in VOLATILE})
    plan["compiled_at"] = now_utc().isoformat(timespec="seconds")
    plan["warnings"] = sorted(set(warnings))
    tbd = [p for p in find_tbd(plan["instances"])]
    if tbd:
        plan["warnings"].append(f"{len(tbd)} TBD values remain inside instances (template names)")
    # Record new slots only once the whole compile has succeeded (no orphan slots).
    if write_allocation and pending_registry is not None:
        save_registry(root, pending_registry)
    return plan


def content_sha256(plan: dict) -> str:
    return sha256_obj({k: v for k, v in plan.items() if k not in VOLATILE})


def _probes(site: dict, resolved: dict, addr: dict, iid: str) -> list[dict]:
    """Probe list run from the probe VM. The self-test proves the probe itself works;
    without a passing self-test, 'unreachable' isolation results are not trusted."""
    checks = resolved["validation"]["checks"]
    probe_name = resolved["validation"]["probe_vm"]
    probe_vm = next(v for v in addr["vms"] if v["name"] == probe_name)
    own_ips = {n["ip"] for n in probe_vm["nics"]}
    out: list[dict] = []

    first = probe_vm["nics"][0]
    peer = first["gateway"] or next(
        (n["ip"] for v in addr["vms"] if v["name"] != probe_name for n in v["nics"]
         if n["network"] == first["network"]), None)
    if peer is None:
        raise RangeError(f"{iid}: probe {probe_name} has no gateway or peer on {first['network']} "
                         f"to self-test against")
    out.append({"check": "selftest", "host": peer, "expect_reachable": True})

    for t in site.get("isolation_probe_targets", []) or []:
        p = {"check": "isolation", "host": str(t["host"]), "expect_reachable": False}
        if t.get("port"):
            p["port"] = int(t["port"])
        out.append(p)
    if "connectivity" in checks:
        for n in addr["networks"]:
            if n["gateway"] and n["gateway"] not in own_ips and n["gateway"] != peer:
                out.append({"check": "connectivity", "host": n["gateway"], "expect_reachable": True})
    if "services" in checks:
        for s in resolved["validation"].get("services", []) or []:
            vm = next(v for v in addr["vms"] if v["name"] == s["vm"])
            out.append({"check": "services", "host": vm["nics"][0]["ip"], "port": int(s["port"]),
                        "vm": vm["vm_name"], "expect_reachable": True})
    return out


def plan_dir(root: Path, rid: str) -> Path:
    return root / "build" / rid


def write_plan(root: Path, plan: dict) -> Path:
    d = plan_dir(root, plan["range_id"])
    write_json(d / "plan.json", plan)
    (d / "plan.txt").write_text(render(plan))
    return d / "plan.json"


def verify_plan(root: Path, rid: str, require_buildable: bool = False) -> dict:
    p = plan_dir(root, rid) / "plan.json"
    if not p.exists():
        raise RangeError(f"no compiled plan for {rid}: run `tools/range compile {rid}`")
    on_disk = load_json(p)
    # 1. The file must not have been edited: its content must hash to its own stamp.
    if content_sha256(on_disk) != on_disk.get("plan_sha256"):
        raise RangeError(f"{rid}: build/{rid}/plan.json content does not match its plan_sha256 "
                         f"(the file was edited). Re-run `tools/range compile {rid}`")
    # 2. It must equal what the compiler produces from the current inputs.
    fresh = build_plan(root, rid, write_allocation=False)
    if on_disk["plan_sha256"] != fresh["plan_sha256"]:
        raise RangeError(f"{rid}: build/{rid}/plan.json is stale (definition, blueprints, site, "
                         f"templates or allocation changed). Re-run `tools/range compile {rid}`")
    # 3. Buildability is judged now, not when the file was written.
    if require_buildable and not fresh["buildable"]:
        raise RangeError([f"{rid}: plan is not buildable:"] + fresh["blockers"])
    on_disk["buildable"], on_disk["blockers"] = fresh["buildable"], fresh["blockers"]
    return on_disk


def render(plan: dict) -> str:
    L: list[str] = []
    r = plan["range"]
    t = plan["totals"]
    L.append(f"RANGE PLAN  {plan['range_id']}   plan {plan['plan_sha256'][:12]}   compiled {plan['compiled_at']}")
    L.append(f"owner {r['owner']} | exercise {r['exercise']} | expires {r['expires']} | students {r['students']}")
    L.append(f"site  dc={plan['site']['datacenter']} cluster={plan['site']['cluster']} "
             f"dvs={plan['site']['dvs']} isolation={plan['site']['isolation_mode']} "
             f"clone={plan['site']['clone_mode']}")
    L.append(f"totals {t['instances']} instance(s), {t['networks']} portgroup(s), {t['vms']} VM(s), "
             f"{t['vcpu']} vCPU, {t['memory_gb']} GB RAM, {t['disk_gb']} GB disk "
             f"({t['disk_required_gb']} GB with headroom)")
    L.append("")
    L.append("BUILDABLE: yes" if plan["buildable"] else "BUILDABLE: NO")
    for b in plan["blockers"]:
        L.append(f"  blocker: {b}")
    for w in plan["warnings"]:
        L.append(f"  warning: {w}")
    for inst in plan["instances"]:
        L.append("")
        L.append(f"== {inst['id']}  slot {inst['slot']}  block {inst['cidr_block']}  folder {inst['folder']}")
        L.append(f"   {'PORTGROUP':<34} {'VLAN':>5}  {'CIDR':<18} {'GATEWAY':<16} PROMISC")
        for n in inst["networks"]:
            L.append(f"   {n['portgroup']:<34} {n['vlan']:>5}  {n['cidr']:<18} "
                     f"{(n['gateway'] or '-'):<16} {'yes' if n['security']['promiscuous'] else 'no'}")
        L.append(f"   {'VM':<30} {'TEMPLATE':<12} {'CPU':>3} {'RAM':>5} {'DISK':>5}  ADDRESSES")
        for v in inst["vms"]:
            addrs = ", ".join(f"{n['ip']}/{n['prefix']}" for n in v["nics"])
            L.append(f"   {v['vm_name']:<30} {v['template_alias']:<12} {v['cpu']:>3} "
                     f"{v['memory_mb'] // 1024:>4}G {v['disk_gb']:>4}G  {addrs}")
        L.append(f"   probe: {inst['probe_vm']}")
    L.append("")
    L.append(f"validation: {', '.join(plan['validation']['checks'])}; "
             f"{len(plan['site']['isolation_probe_targets'])} isolation target(s); "
             f"{len(plan['validation'].get('services', []))} service check(s)")
    enabled = [k for k, v in (plan.get("integrations") or {}).items() if v.get("enabled")]
    L.append(f"integrations enabled: {', '.join(enabled) or 'none'}")
    return "\n".join(L) + "\n"
