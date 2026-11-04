"""Structural validation for range definitions, blueprints, site config and templates.

Errors are collected and reported together so an operator can fix a definition in
one pass. Semantic checks that need the composed range (references, uniqueness,
allocation) live in compose.py and allocate.py.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from .common import (
    ALIAS_RE,
    LOCAL_NAME_RE,
    RANGE_ID_RE,
    is_tbd,
    to_date,
)

GUEST_CONFIG = {"prebaked", "tools", "ssh", "winrm"}
CHECKS = {"isolation", "connectivity", "services"}
INTEGRATIONS = {"range_activity", "greyspace", "arc2"}
POWER = {"poweredon", "poweredoff"}  # poweredoff: validated powered on, then shut down
SECURITY_KEYS = {"promiscuous", "forged_transmits", "mac_changes"}
PARAM_TYPES = {"int", "str", "bool", "list"}
ISOLATION_MODES = {"uplink_none", "vlan_trunk_no_l3"}
CLONE_MODES = {"full", "linked"}

RANGE_TOP = {"range", "use", "networks", "vms", "validation", "integrations",
             "power_after_build", "source_scenario"}
RANGE_META = {"id", "owner", "exercise", "expires", "students", "description"}
VM_KEYS = {"name", "count", "template", "role", "cpu", "memory_gb", "disk_gb",
           "guest_config", "nics", "host", "vars"}
NET_KEYS = {"name", "gateway", "security"}


def _unknown(where: str, data: dict, allowed: set[str], errs: list[str]) -> None:
    for k in data:
        if k not in allowed:
            errs.append(f"{where}: unknown key '{k}' (allowed: {', '.join(sorted(allowed))})")


def _int_in(where: str, value: Any, lo: int, hi: int, errs: list[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        errs.append(f"{where}: must be an integer, got {value!r}")
    elif not lo <= value <= hi:
        errs.append(f"{where}: must be between {lo} and {hi}, got {value}")


def validate_range_doc(doc: Any, max_students: int = 40) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["range definition must be a mapping"]
    _unknown("<top>", doc, RANGE_TOP, errs)

    meta = doc.get("range")
    if not isinstance(meta, dict):
        errs.append("range: required mapping is missing")
        meta = {}
    _unknown("range", meta, RANGE_META, errs)
    rid = meta.get("id")
    if not isinstance(rid, str) or not RANGE_ID_RE.match(rid):
        errs.append(f"range.id: must match {RANGE_ID_RE.pattern}, got {rid!r}")
    if "expires" not in meta or to_date(meta.get("expires")) is None:
        errs.append("range.expires: required, format YYYY-MM-DD")
    _int_in("range.students", meta.get("students", 1), 1, max_students, errs)

    uses = doc.get("use", [])
    if not isinstance(uses, list):
        errs.append("use: must be a list")
        uses = []
    aliases: set[str] = set()
    for i, u in enumerate(uses):
        w = f"use[{i}]"
        if not isinstance(u, dict):
            errs.append(f"{w}: must be a mapping")
            continue
        _unknown(w, u, {"blueprint", "as", "params"}, errs)
        if not isinstance(u.get("blueprint"), str):
            errs.append(f"{w}.blueprint: required string")
        alias = u.get("as")
        if not isinstance(alias, str) or not ALIAS_RE.match(alias):
            errs.append(f"{w}.as: must match {ALIAS_RE.pattern}, got {alias!r}")
        elif alias in aliases:
            errs.append(f"{w}.as: alias '{alias}' used twice")
        else:
            aliases.add(alias)
        if "params" in u and not isinstance(u["params"], dict):
            errs.append(f"{w}.params: must be a mapping")

    for i, n in enumerate(doc.get("networks", []) or []):
        errs.extend(validate_network(f"networks[{i}]", n))
    for i, v in enumerate(doc.get("vms", []) or []):
        errs.extend(validate_vm(f"vms[{i}]", v, pre_expand=True))

    val = doc.get("validation", {}) or {}
    if not isinstance(val, dict):
        errs.append("validation: must be a mapping")
    else:
        _unknown("validation", val, {"probe_vm", "checks", "services"}, errs)
        for c in val.get("checks", []) or []:
            if c not in CHECKS:
                errs.append(f"validation.checks: unknown check '{c}' (allowed: {', '.join(sorted(CHECKS))})")
        for j, s in enumerate(val.get("services", []) or []):
            w = f"validation.services[{j}]"
            if not isinstance(s, dict) or not isinstance(s.get("vm"), str):
                errs.append(f"{w}: needs vm (string) and port (int)")
                continue
            _int_in(f"{w}.port", s.get("port"), 1, 65535, errs)

    integ = doc.get("integrations", {}) or {}
    if not isinstance(integ, dict):
        errs.append("integrations: must be a mapping")
    else:
        for k, v in integ.items():
            if k not in INTEGRATIONS:
                errs.append(f"integrations: unknown integration '{k}' (allowed: {', '.join(sorted(INTEGRATIONS))})")
            elif not isinstance(v, dict) or not isinstance(v.get("enabled", False), bool):
                errs.append(f"integrations.{k}: must be a mapping with boolean 'enabled'")

    if doc.get("power_after_build", "poweredon") not in POWER:
        errs.append(f"power_after_build: must be one of {', '.join(sorted(POWER))}")
    return errs


def validate_network(where: str, n: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(n, dict):
        return [f"{where}: must be a mapping"]
    _unknown(where, n, NET_KEYS, errs)
    name = n.get("name")
    if not isinstance(name, str) or not LOCAL_NAME_RE.match(name):
        errs.append(f"{where}.name: must match {LOCAL_NAME_RE.pattern}, got {name!r}")
    if not isinstance(n.get("gateway", True), bool):
        errs.append(f"{where}.gateway: must be true or false")
    sec = n.get("security", {}) or {}
    if not isinstance(sec, dict):
        errs.append(f"{where}.security: must be a mapping")
    else:
        _unknown(f"{where}.security", sec, SECURITY_KEYS, errs)
        for k, v in sec.items():
            if not isinstance(v, bool):
                errs.append(f"{where}.security.{k}: must be true or false")
    return errs


def validate_vm(where: str, v: Any, pre_expand: bool = False) -> list[str]:
    errs: list[str] = []
    if not isinstance(v, dict):
        return [f"{where}: must be a mapping"]
    _unknown(where, v, VM_KEYS, errs)
    name = v.get("name")
    if not isinstance(name, str):
        errs.append(f"{where}.name: required string")
    elif pre_expand:
        if not LOCAL_NAME_RE.match(name.replace("{nn}", "00")):
            errs.append(f"{where}.name: must match {LOCAL_NAME_RE.pattern} ('{{nn}}' allowed), got {name!r}")
    elif not LOCAL_NAME_RE.match(name):
        errs.append(f"{where}.name: must match {LOCAL_NAME_RE.pattern}, got {name!r}")
    if not isinstance(v.get("template"), str):
        errs.append(f"{where}.template: required template alias")
    if not isinstance(v.get("role"), str):
        errs.append(f"{where}.role: required string")
    _int_in(f"{where}.cpu", v.get("cpu"), 1, 64, errs)
    _int_in(f"{where}.memory_gb", v.get("memory_gb"), 1, 1024, errs)
    _int_in(f"{where}.disk_gb", v.get("disk_gb"), 1, 8192, errs)
    if v.get("guest_config", "tools") not in GUEST_CONFIG:
        errs.append(f"{where}.guest_config: must be one of {', '.join(sorted(GUEST_CONFIG))}")
    if "count" in v and not pre_expand:
        errs.append(f"{where}.count: should have been expanded")
    nics = v.get("nics")
    if not isinstance(nics, list) or not nics:
        errs.append(f"{where}.nics: at least one network is required")
    else:
        for j, nic in enumerate(nics):
            if isinstance(nic, str):
                continue
            if isinstance(nic, dict) and isinstance(nic.get("network"), str):
                _unknown(f"{where}.nics[{j}]", nic, {"network", "host"}, errs)
                if "host" in nic and nic["host"] != "auto":
                    _int_in(f"{where}.nics[{j}].host", nic["host"], 1, 254, errs)
                continue
            errs.append(f"{where}.nics[{j}]: must be a network name or {{network, host}}")
    if "host" in v and v["host"] != "auto":
        _int_in(f"{where}.host", v["host"], 1, 254, errs)
    if "vars" in v and not isinstance(v["vars"], dict):
        errs.append(f"{where}.vars: must be a mapping")
    return errs


def validate_blueprint_doc(name: str, doc: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return [f"blueprint {name}: must be a mapping"]
    _unknown(f"blueprint {name}", doc, {"blueprint", "networks", "vms"}, errs)
    meta = doc.get("blueprint")
    if not isinstance(meta, dict) or meta.get("name") != name:
        errs.append(f"blueprint {name}: blueprint.name must equal the file name '{name}'")
        meta = meta if isinstance(meta, dict) else {}
    params = meta.get("params", {}) or {}
    if not isinstance(params, dict):
        errs.append(f"blueprint {name}: params must be a mapping")
    else:
        for p, spec in params.items():
            if not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES:
                errs.append(f"blueprint {name}: param '{p}' needs type in {sorted(PARAM_TYPES)}")
            elif "default" not in spec:
                errs.append(f"blueprint {name}: param '{p}' needs a default")
    if not isinstance(doc.get("networks", []) or [], list):
        errs.append(f"blueprint {name}: networks must be a list")
    if not isinstance(doc.get("vms", []) or [], list):
        errs.append(f"blueprint {name}: vms must be a list")
    return errs


def validate_site(site: Any) -> tuple[list[str], list[str]]:
    """Return (errors, tbd_fields). TBD fields block a real build but not planning."""
    errs: list[str] = []
    tbd: list[str] = []
    if not isinstance(site, dict) or not isinstance(site.get("site"), dict):
        return ["config/site.yml: top-level 'site' mapping is missing"], tbd
    s = site["site"]
    vc = s.get("vcenter", {}) or {}
    for k in ("datacenter", "cluster", "dvs"):
        if is_tbd(vc.get(k)) or not vc.get(k):
            tbd.append(f"site.vcenter.{k}")
    ds = vc.get("datastores_allowed") or []
    if not ds or any(is_tbd(d) for d in ds):
        tbd.append("site.vcenter.datastores_allowed")
    mode = s.get("isolation_mode")
    if is_tbd(mode):
        tbd.append("site.isolation_mode")
    elif mode not in ISOLATION_MODES:
        errs.append(f"site.isolation_mode: must be one of {sorted(ISOLATION_MODES)}")
    if not s.get("isolation_probe_targets"):
        tbd.append("site.isolation_probe_targets (isolation cannot be proven without targets)")
    if s.get("clone_mode", "full") not in CLONE_MODES:
        errs.append(f"site.clone_mode: must be one of {sorted(CLONE_MODES)}")

    pool = s.get("vlan_pool", {}) or {}
    start, end = pool.get("start"), pool.get("end")
    if not (isinstance(start, int) and isinstance(end, int) and 1 <= start < end <= 4094):
        errs.append("site.vlan_pool: start and end must be integers with 1 <= start < end <= 4094")
    per = s.get("vlans_per_slot")
    sp, np_ = s.get("slot_prefix"), s.get("network_prefix")
    if not isinstance(per, int) or per < 1:
        errs.append("site.vlans_per_slot: must be a positive integer")
    if not (isinstance(sp, int) and isinstance(np_, int) and 8 <= sp < np_ <= 29):
        errs.append("site.slot_prefix/network_prefix: need 8 <= slot_prefix < network_prefix <= 29")
    elif isinstance(per, int) and per > 2 ** (np_ - sp):
        errs.append(f"site.vlans_per_slot ({per}) exceeds networks per slot (2^({np_}-{sp}) = {2 ** (np_ - sp)})")
    try:
        net = ipaddress.ip_network(s.get("cidr_pool"))
        if isinstance(sp, int) and net.prefixlen > sp:
            errs.append(f"site.cidr_pool {net} is smaller than one slot (/{sp})")
    except (ValueError, TypeError):
        errs.append(f"site.cidr_pool: invalid network {s.get('cidr_pool')!r}")
    for c in s.get("reserved_cidrs", []) or []:
        try:
            ipaddress.ip_network(c)
        except ValueError:
            errs.append(f"site.reserved_cidrs: invalid network {c!r}")
    off = s.get("first_host_offset", 10)
    if isinstance(off, bool) or not isinstance(off, int) or not 2 <= off <= 200:
        errs.append("site.first_host_offset: must be an integer from 2 to 200 (.0 is the network, .1 the gateway)")
    for v in s.get("reserved_vlans", []) or []:
        if not isinstance(v, int) or not 0 <= v <= 4095:
            errs.append(f"site.reserved_vlans: invalid VLAN {v!r}")
    return errs, tbd


TEMPLATE_PROVENANCE = ("vcenter_name", "build_id", "build_date", "source_sha256")


def template_blockers(alias: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return [f"template '{alias}': not in config/templates.yml"]
    problems = []
    if entry.get("validated") is not True:
        problems.append("not validated")
    missing = [k for k in TEMPLATE_PROVENANCE if is_tbd(entry.get(k)) or not entry.get(k)]
    if missing:
        problems.append(f"missing provenance: {', '.join(missing)}")
    return [f"template '{alias}': {'; '.join(problems)}"] if problems else []
