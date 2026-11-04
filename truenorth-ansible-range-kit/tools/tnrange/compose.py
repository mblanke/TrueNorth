"""Compose a range definition from blueprints into one flat, resolved range.

Naming:
  - Blueprint networks and VMs are namespaced with the 'as' alias: corp-lan, corp-dc01.
  - Inside a blueprint, a nic 'lan' means the blueprint's own network (corp-lan).
  - '@name' refers to a fully qualified network anywhere in the range (@red-net).
  - Range-level networks and VMs are not namespaced.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .common import (
    QUALIFIED_NAME_RE,
    RangeError,
    load_yaml,
    sha256_obj,
)
from .schema import validate_blueprint_doc, validate_network, validate_vm

FULL_REF = re.compile(r"^\$\{([a-z_][a-z0-9_]*)\}$")
PART_REF = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")


def _substitute(obj: Any, params: dict[str, Any], where: str, errs: list[str]) -> Any:
    if isinstance(obj, str):
        m = FULL_REF.match(obj)
        if m:
            if m.group(1) not in params:
                errs.append(f"{where}: unknown parameter '${{{m.group(1)}}}'")
                return obj
            return copy.deepcopy(params[m.group(1)])

        def repl(mm: re.Match) -> str:
            if mm.group(1) not in params:
                errs.append(f"{where}: unknown parameter '${{{mm.group(1)}}}'")
                return mm.group(0)
            return str(params[mm.group(1)])

        return PART_REF.sub(repl, obj)
    if isinstance(obj, list):
        return [_substitute(x, params, where, errs) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute(v, params, f"{where}.{k}", errs) for k, v in obj.items()}
    return obj


def _resolve_params(bp_name: str, spec: dict, given: dict, where: str, errs: list[str]) -> dict:
    out: dict[str, Any] = {}
    for k in given:
        if k not in spec:
            errs.append(f"{where}.params.{k}: blueprint '{bp_name}' has no parameter '{k}' "
                        f"(has: {', '.join(sorted(spec)) or 'none'})")
    for p, s in spec.items():
        val = given.get(p, s.get("default"))
        t = s.get("type")
        ok = {
            "int": isinstance(val, int) and not isinstance(val, bool),
            "str": isinstance(val, str),
            "bool": isinstance(val, bool),
            "list": isinstance(val, list),
        }.get(t, False)
        if not ok:
            errs.append(f"{where}.params.{p}: expected {t}, got {val!r}")
            continue
        if t == "int":
            if "min" in s and val < s["min"]:
                errs.append(f"{where}.params.{p}: {val} is below minimum {s['min']}")
            if "max" in s and val > s["max"]:
                errs.append(f"{where}.params.{p}: {val} is above maximum {s['max']}")
        out[p] = val
    return out


def _expand_counts(vms: list[dict], where: str, errs: list[str]) -> list[dict]:
    out = []
    for i, vm in enumerate(vms):
        vm = dict(vm)
        count = vm.pop("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            errs.append(f"{where}.vms[{i}].count: must be a non-negative integer, got {count!r}")
            continue
        name = vm.get("name", "")
        if count == 0:
            continue
        if "{nn}" in name:
            for n in range(1, count + 1):
                v = copy.deepcopy(vm)
                v["name"] = name.replace("{nn}", f"{n:02d}")
                out.append(v)
        elif count == 1:
            out.append(vm)
        else:
            errs.append(f"{where}.vms[{i}]: count {count} needs '{{nn}}' in the name")
    return out


def _normalise_nics(vm: dict, resolve) -> list[dict]:
    default_host = vm.get("host", "auto")
    nics = []
    for nic in vm.get("nics", []):
        if isinstance(nic, str):
            nics.append({"network": resolve(nic), "host": default_host})
        else:
            nics.append({"network": resolve(nic["network"]), "host": nic.get("host", default_host)})
    return nics


def load_blueprint(root: Path, name: str) -> dict:
    if not re.match(r"^[a-z][a-z0-9_]{0,40}$", name):
        raise RangeError(f"invalid blueprint name {name!r}")
    doc = load_yaml(root / "blueprints" / f"{name}.yml")
    errs = validate_blueprint_doc(name, doc)
    if errs:
        raise RangeError(errs)
    return doc


def compose(doc: dict, root: Path) -> dict:
    """Return the resolved range: flat networks and VMs with qualified names."""
    errs: list[str] = []
    networks: list[dict] = []
    vms: list[dict] = []
    blueprint_hashes: dict[str, str] = {}

    for i, use in enumerate(doc.get("use", []) or []):
        where = f"use[{i}]"
        bp_name, alias = use["blueprint"], use["as"]
        try:
            bp = load_blueprint(root, bp_name)
        except RangeError as exc:
            errs.extend(f"{where}: {e}" for e in exc.errors)
            continue
        blueprint_hashes[bp_name] = sha256_obj(bp)
        spec = (bp.get("blueprint", {}) or {}).get("params", {}) or {}
        params = _resolve_params(bp_name, spec, use.get("params", {}) or {}, where, errs)
        body = _substitute({"networks": bp.get("networks", []) or [], "vms": bp.get("vms", []) or []},
                           params, f"{where}({bp_name})", errs)

        local_nets = set()
        for j, n in enumerate(body["networks"]):
            e = validate_network(f"{where}({bp_name}).networks[{j}]", n)
            if e:
                errs.extend(e)
                continue
            local_nets.add(n["name"])
            n = dict(n)
            n["name"] = f"{alias}-{n['name']}"
            n["source"] = f"blueprint:{bp_name}:{alias}"
            networks.append(n)

        def resolve(ref: str, _alias=alias, _local=local_nets) -> str:
            if ref.startswith("@"):
                return ref[1:]
            if ref in _local:
                return f"{_alias}-{ref}"
            return f"{_alias}-{ref}"  # reported as unknown later

        for j, vm in enumerate(_expand_counts(body["vms"], f"{where}({bp_name})", errs)):
            e = validate_vm(f"{where}({bp_name}).vms[{j}]", vm)
            if e:
                errs.extend(e)
                continue
            vm = dict(vm)
            vm["nics"] = _normalise_nics(vm, resolve)
            vm["name"] = f"{alias}-{vm['name']}"
            vm["source"] = f"blueprint:{bp_name}:{alias}"
            vm.pop("host", None)
            vms.append(vm)

    for j, n in enumerate(doc.get("networks", []) or []):
        n = dict(n)
        n["source"] = "range"
        networks.append(n)
    for j, vm in enumerate(_expand_counts(doc.get("vms", []) or [], "<range>", errs)):
        e = validate_vm(f"vms[{j}]", vm)
        if e:
            errs.extend(e)
            continue
        vm = dict(vm)
        vm["nics"] = _normalise_nics(vm, lambda r: r[1:] if r.startswith("@") else r)
        vm["source"] = "range"
        vm.pop("host", None)
        vms.append(vm)

    # Defaults and semantic checks.
    net_names: dict[str, dict] = {}
    for n in networks:
        n.setdefault("gateway", True)
        sec = {"promiscuous": False, "forged_transmits": False, "mac_changes": False}
        sec.update(n.get("security", {}) or {})
        n["security"] = sec
        if not QUALIFIED_NAME_RE.match(n["name"]):
            errs.append(f"network '{n['name']}': qualified name is invalid")
        if n["name"] in net_names:
            errs.append(f"network '{n['name']}': defined twice")
        net_names[n["name"]] = n

    vm_names: set[str] = set()
    for vm in vms:
        vm.setdefault("guest_config", "tools")
        vm.setdefault("vars", {})
        if vm["name"] in vm_names:
            errs.append(f"vm '{vm['name']}': defined twice")
        vm_names.add(vm["name"])
        seen = set()
        for nic in vm["nics"]:
            if nic["network"] not in net_names:
                errs.append(f"vm '{vm['name']}': nic references unknown network '{nic['network']}' "
                            f"(known: {', '.join(sorted(net_names)) or 'none'})")
            if nic["network"] in seen:
                errs.append(f"vm '{vm['name']}': two nics on network '{nic['network']}'")
            seen.add(nic["network"])
            if nic["host"] == 1 and nic["network"] in net_names and not net_names[nic["network"]]["gateway"]:
                pass  # .1 on a non-gateway network is allowed

    if not vms:
        errs.append("range has no VMs")

    val = dict(doc.get("validation", {}) or {})
    checks = list(dict.fromkeys(["isolation"] + list(val.get("checks", []) or [])))
    val["checks"] = checks  # isolation is always tested
    probe = val.get("probe_vm")
    if probe is None:
        errs.append("validation.probe_vm: required (isolation must be tested from inside the range)")
    elif probe not in vm_names:
        errs.append(f"validation.probe_vm: '{probe}' is not a VM in this range")
    else:
        pvm = next(v for v in vms if v["name"] == probe)
        if pvm["guest_config"] == "prebaked":
            errs.append(f"validation.probe_vm: '{probe}' is prebaked; the probe needs guest access (tools/ssh/winrm)")
    for s in val.get("services", []) or []:
        if s["vm"] not in vm_names:
            errs.append(f"validation.services: '{s['vm']}' is not a VM in this range")
    val.setdefault("services", [])

    if errs:
        raise RangeError(errs)

    meta = doc["range"]
    return {
        "range": {
            "id": meta["id"],
            "owner": meta.get("owner", "TBD"),
            "exercise": meta.get("exercise", "TBD"),
            "expires": str(meta["expires"]),
            "students": meta.get("students", 1),
            "description": meta.get("description", ""),
        },
        "networks": networks,
        "vms": vms,
        "validation": val,
        "integrations": doc.get("integrations", {}) or {},
        "power_after_build": doc.get("power_after_build", "poweredon"),
        "source_scenario": doc.get("source_scenario"),
        "blueprints": blueprint_hashes,
    }
