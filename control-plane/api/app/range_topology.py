"""Generate a JointJS `diagram_json` topology for a PO's range.

The 2D range-designer (`graph.fromJSON`) and the 3D topology viewer both render
`Range.diagram_json = {"cells": [...]}`. No generator existed; this builds a
representative topology per PO from its domain (network-analysis / malware /
red-team / incident-response), laid into VLAN subnet zones. Deterministic, no LLM.
"""

from __future__ import annotations

import json

# JointJS stencil nodeType -> colour (mirrors range-designer nodeColors) so the
# generated cells match what the 2D designer's own createNodeShape() produces.
_NODE_COLORS: dict[str, str] = {
    "workstation": "#42A5F5",
    "server": "#66BB6A",
    "dc": "#AB47BC",
    "kali": "#EF5350",
    "switch": "#FFA726",
    "router": "#26C6DA",
    "cloud": "#78909C",
    "firewall": "#FF7043",
    "seconion": "#5C6BC0",
    "subnet": "#29B6F6",
    "dmz": "#FFCA28",
}
_ZONE_W, _ZONE_H = 640, 150
_NODE_W, _NODE_H = 120, 80


def _cell_zone(cid: str, label: str, cidr: str, x: int, y: int, dmz: bool = False,
               width: int = _ZONE_W, height: int = _ZONE_H, vlan: int | None = None) -> dict:
    color = _NODE_COLORS["dmz" if dmz else "subnet"]
    data: dict = {"label": label, "cidr": cidr}
    if vlan is not None:
        data["vlan"] = vlan
    return {
        "type": "standard.Rectangle",
        "id": cid,
        "position": {"x": x, "y": y},
        "size": {"width": width, "height": height},
        "angle": 0,
        "nodeType": "dmz" if dmz else "subnet",
        "nodeData": data,
        "attrs": {
            "body": {
                "fill": color + "15",
                "stroke": color,
                "strokeWidth": 2,
                "strokeDasharray": "8 4",
                "rx": 12,
                "ry": 12,
            },
            "label": {
                "text": f"{label}  ({cidr})",
                "fill": color,
                "fontSize": 14,
                "fontFamily": "Calibri, Segoe UI, sans-serif",
                "fontWeight": "bold",
                "textAnchor": "start",
                "textVerticalAnchor": "top",
                "refX": 12,
                "refY": 8,
            },
        },
    }


def _cell_node(cid: str, label: str, node_type: str, os_template: str, ip: str, x: int, y: int) -> dict:
    color = _NODE_COLORS.get(node_type, "#66BB6A")
    return {
        "type": "standard.Rectangle",
        "id": cid,
        "position": {"x": x, "y": y},
        "size": {"width": _NODE_W, "height": _NODE_H},
        "angle": 0,
        "nodeType": node_type,
        "nodeData": {"label": label, "ip": ip, "os_template": os_template},
        "attrs": {
            "body": {
                "fill": "var(--bg-card)",
                "stroke": color,
                "strokeWidth": 2,
                "rx": 8,
                "ry": 8,
                "filter": "none",
            },
            "label": {
                "text": f"{label}\n{ip}",
                "fill": "var(--text-primary)",
                "fontSize": 12,
                "fontFamily": "Calibri, Segoe UI, sans-serif",
                "textAnchor": "middle",
                "textVerticalAnchor": "top",
                "refX": "50%",
                "refY": "62%",
            },
        },
        "ports": {
            "groups": {
                "in": {
                    "position": "left",
                    "attrs": {
                        "circle": {"fill": color, "stroke": "var(--border)", "strokeWidth": 1, "r": 5, "magnet": True}
                    },
                    "label": {"position": "outside"},
                },
                "out": {
                    "position": "right",
                    "attrs": {
                        "circle": {"fill": color, "stroke": "var(--border)", "strokeWidth": 1, "r": 5, "magnet": True}
                    },
                    "label": {"position": "outside"},
                },
            },
            "items": [{"group": "in", "id": "in1"}, {"group": "out", "id": "out1"}],
        },
    }


def _cell_link(cid: str, src: str, dst: str) -> dict:
    return {
        "type": "standard.Link",
        "id": cid,
        "source": {"id": src},
        "target": {"id": dst},
        "z": 2,
        "attrs": {
            "line": {
                "stroke": "#8892a6",
                "strokeWidth": 2,
                "targetMarker": {"type": "path", "d": "M 10 -5 0 0 10 5 z", "fill": "#8892a6"},
            }
        },
        "router": {"name": "manhattan", "args": {"step": 20}},
        "connector": {"name": "rounded", "args": {"radius": 8}},
    }


def _plan(po) -> tuple[str, list[dict]]:
    """Return (base_octet, zones). Each zone: {name, cidr, dmz, nodes:[(label,type,os)]}."""
    title = (po.title or "").lower()
    role = (po.target_role or "").lower()
    env = po.environment.value if po.environment else "COTE"
    # deterministic base octet from po_code digits
    digits = "".join(c for c in po.po_code if c.isdigit()) or "60"
    base = 50 + (int(digits[:2]) % 40)  # 10.<base>.*

    malware = (
        any(
            k in title for k in ("malware", "static analysis", "dynamic analysis", "memory forensics", "low level code")
        )
        or "reverse engineer" in role
    )
    red = ("adversary" in role) or any(
        k in title for k in ("reconnaissance", "exploitation", "post-exploitation", "threat emulation")
    )
    ir = (
        any(k in title for k in ("respond", "defend a network", "incident"))
        or "capstone" in (po.assessment_type or "").lower()
    )

    if malware:
        return str(base), [
            {
                "name": "Sterile Analysis",
                "cidr": f"10.{base}.30.0/24",
                "dmz": False,
                "nodes": [
                    ("remnux", "server", "remnux", 10),
                    ("sift-fx", "workstation", "sift", 11),
                    ("detonation", "workstation", "detonation-host", 12),
                ],
            },
            {
                "name": "Management",
                "cidr": f"10.{base}.40.0/24",
                "dmz": False,
                "nodes": [
                    ("analyst-ws", "workstation", "win10-22h2", 5),
                ],
            },
        ]
    if red:
        return str(base), [
            {
                "name": "Attacker Infra",
                "cidr": f"10.{base}.30.0/24",
                "dmz": False,
                "nodes": [
                    ("kali-op", "kali", "kali", 10),
                    ("c2-server", "server", "c2-server", 11),
                    ("redir", "server", "ubuntu-lts", 12),
                ],
            },
            {
                "name": "Target DMZ",
                "cidr": f"10.{base}.10.0/24",
                "dmz": True,
                "nodes": [
                    ("web01", "server", "ubuntu-lts", 20),
                    ("mail01", "server", "ubuntu-lts", 21),
                ],
            },
            {
                "name": "Target Corp",
                "cidr": f"10.{base}.11.0/24",
                "dmz": False,
                "nodes": [
                    ("dc01", "dc", "srv2019", 10),
                    ("ws01", "workstation", "win10-22h2", 30),
                    ("ws02", "workstation", "win11-24h2", 31),
                ],
            },
        ]
    if ir:
        return str(base), [
            {
                "name": "Attacker Infra",
                "cidr": f"10.{base}.30.0/24",
                "dmz": False,
                "nodes": [
                    ("kali-op", "kali", "kali", 10),
                ],
            },
            {
                "name": "Victim Network",
                "cidr": f"10.{base}.11.0/24",
                "dmz": False,
                "nodes": [
                    ("dc01", "dc", "srv2019", 10),
                    ("fs01", "server", "srv2019", 11),
                    ("ws01", "workstation", "win10-22h2", 30),
                    ("ws02", "workstation", "win11-24h2", 31),
                ],
            },
            {
                "name": "SOC / Monitoring",
                "cidr": f"10.{base}.20.0/24",
                "dmz": False,
                "nodes": [
                    ("securityonion", "seconion", "securityonion", 10),
                    ("siem", "server", "ubuntu-lts", 11),
                    ("dfir-ws", "workstation", "sift", 12),
                ],
            },
        ]
    # default: network / log analysis (defensive)
    return str(base), [
        {
            "name": "Attacker Infra",
            "cidr": f"10.{base}.30.0/24",
            "dmz": False,
            "nodes": [
                ("kali-op", "kali", "kali", 10),
            ],
        },
        {
            "name": "Victim Network",
            "cidr": f"10.{base}.11.0/24",
            "dmz": False,
            "nodes": [
                ("dc01", "dc", "srv2019", 10),
                ("precomp", "workstation", "precomp-host", 20),
                ("ws01", "workstation", "win10-22h2", 30),
            ],
        },
        {
            "name": "Monitoring",
            "cidr": f"10.{base}.20.0/24",
            "dmz": False,
            "nodes": [
                ("securityonion", "seconion", "securityonion", 10),
                ("usersim", "server", "usersim", 40),
            ],
        },
    ]


def build_diagram(po) -> dict:
    """Build `{cells:[...]}` for a PO: VLAN subnet zones + host nodes + firewall gateway.

    Cells mirror exactly what the range-designer's own createNodeShape/createSubnetZone
    serialize to (standard.Rectangle with attrs.body/label + ports), so JointJS renders them.
    Links are intentionally omitted — the VLAN zones convey grouping and links are the most
    render-fragile cell type; the firewall node represents the gateway.
    """
    base, zones = _plan(po)
    cells: list[dict] = []

    # firewall gateway at top
    cells.append(_cell_node("fw01", "pfSense GW", "firewall", "pfsense", f"10.{base}.0.1", 280, 20))

    y = 130
    for zi, zone in enumerate(zones):
        cells.append(_cell_zone(f"zone-{zi}", zone["name"], zone["cidr"], 40, y, dmz=zone["dmz"]))
        octet3 = zone["cidr"].split(".")[2]
        for ni, (label, ntype, os_t, host) in enumerate(zone["nodes"]):
            ip = f"10.{base}.{octet3}.{host}"
            cells.append(_cell_node(f"n-{zi}-{ni}", label, ntype, os_t, ip, 70 + ni * 150, y + 45))
        y += _ZONE_H + 40

    return {"cells": cells}


def diagram_summary(po) -> str:
    """Compact human summary of the generated topology (for logs/debug)."""
    _, zones = _plan(po)
    n = sum(len(z["nodes"]) for z in zones)
    return json.dumps({"zones": [z["name"] for z in zones], "hosts": n})


# ── Template-driven starter topology ─────────────────────────────────────

# Template asset roles → the designer's node stencil types + a default image.
_ROLE_STENCIL: dict[str, tuple[str, str]] = {
    "dc": ("dc", "windows-server-2022"),
    "domain_controller": ("dc", "windows-server-2022"),
    "server": ("server", "ubuntu-24.04"),
    "workstation": ("workstation", "windows-11"),
    "client": ("workstation", "windows-11"),
    "kali": ("kali", "kali-2024"),
    "attacker": ("kali", "kali-2024"),
    "firewall": ("firewall", "pfsense"),
    "gateway": ("firewall", "pfsense"),
    "router": ("router", "vyos"),
    "sensor": ("seconion", "security-onion"),
    "ids": ("seconion", "security-onion"),
}


def build_template_diagram(template_yaml: str) -> dict:
    """Render a range template's declared assets into a starter JointJS diagram.

    Templates declare ``assets: [{role, type, count}]`` and/or ``nodes: [...]``
    (template.schema.json); this lays one host cell per declared instance into a
    single zone so the Range Designer can open a template as an editable
    starting point instead of a blank canvas. Cells reuse the same helpers the
    PO generator uses, so the designer renders them identically.

    Raises ValueError on unparseable / non-mapping YAML so the caller can 422.
    """
    import yaml as pyyaml

    try:
        doc = pyyaml.safe_load(template_yaml)
    except pyyaml.YAMLError as exc:
        raise ValueError(f"template YAML did not parse: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("template must be a YAML mapping")

    name = str(doc.get("name", "template"))
    cidr = str(((doc.get("network") or {}) if isinstance(doc.get("network"), dict) else {}).get("cidr", "10.0.0.0/24"))

    # Flatten declared hosts from assets[] (role+count) and nodes[] (explicit).
    hosts: list[tuple[str, str]] = []  # (label, role)
    for asset in doc.get("assets", []) or []:
        if not isinstance(asset, dict):
            continue
        role = str(asset.get("role") or asset.get("type") or "server")
        count = int(asset.get("count", 1) or 1)
        for i in range(max(1, count)):
            hosts.append((f"{role}{i + 1}" if count > 1 else role, role))
    for node in doc.get("nodes", []) or []:
        if isinstance(node, dict):
            hosts.append((str(node.get("label") or node.get("name") or "node"), str(node.get("role") or node.get("type") or "server")))
        elif isinstance(node, str):
            hosts.append((node, "server"))

    # Templates that declare full nodes (content/ranges/*) open losslessly: every node,
    # its OS, specs, services and VLAN zone, so Save topology can round-trip them.
    if any(isinstance(n, dict) for n in doc.get("nodes", []) or []) and not doc.get("assets"):
        return template_to_diagram(doc)

    cells: list[dict] = [_cell_node("fw01", "Gateway", "firewall", "pfsense", "10.0.0.1", 280, 20)]
    cells.append(_cell_zone("zone-0", name, cidr, 40, 130, dmz=False))
    for i, (label, role) in enumerate(hosts[:24]):  # cap so a huge template stays legible
        node_type, os_t = _ROLE_STENCIL.get(role.lower(), ("server", "ubuntu-24.04"))
        cells.append(_cell_node(f"n-{i}", label, node_type, os_t, f"10.0.0.{10 + i}", 70 + (i % 4) * 150, 175 + (i // 4) * 95))
    return {"cells": cells}


# ── Designer diagram <-> provisionable template ─────────────────────────
#
# The Range Designer edits a JointJS graph (`Range.diagram_json`); the worker only
# provisions from a template's `nodes` + `network.vlans` (worker/render.py). These two
# functions are the bridge, in both directions, so a designed topology is what gets
# built. JSON and YAML carry the same dict: the worker reads either.

_ZONE_TYPES = frozenset({"subnet", "dmz"})
# Drawn for readability, never built: a switch is the port group, the cloud is outside.
_NON_VM_TYPES = frozenset({"switch", "cloud"}) | _ZONE_TYPES

_TYPE_ROLE: dict[str, str] = {
    "dc": "domain_controller",
    "workstation": "workstation",
    "server": "server",
    "kali": "attack_platform",
    "router": "router",
    "firewall": "firewall",
    "seconion": "network_monitor",
}

_GRID_COLS = 4
_CELL_W, _CELL_H = 150, 95


def _node_type_for(role: str, os_name: str) -> str:
    """Designer stencil type for a template node, from its role, else its OS."""
    role_l, os_l = role.lower(), os_name.lower()
    if role_l in _ROLE_STENCIL:
        return _ROLE_STENCIL[role_l][0]
    if os_l.startswith("kali"):
        return "kali"
    if os_l.startswith("pfsense"):
        return "firewall"
    if os_l.startswith("vyos"):
        return "router"
    if os_l.replace("-", "").startswith("securityonion"):
        return "seconion"
    if os_l.startswith("windows") and "server" not in os_l:
        return "workstation"
    return "server"


def template_to_diagram(doc: dict) -> dict:
    """Lay a full-node template out as a designer diagram, keeping everything provisioning reads.

    One zone per VLAN (with its CIDR and VLAN id), every node expanded by `count`,
    and each node's OS, specs, services, IP and role carried in `nodeData`.
    Inverse of `diagram_to_template`.
    """
    network = doc.get("network") if isinstance(doc.get("network"), dict) else {}
    vlans = [v for v in (network.get("vlans") or []) if isinstance(v, dict)]
    nodes = [n for n in (doc.get("nodes") or []) if isinstance(n, dict)]

    by_vlan: dict[str, list[dict]] = {}
    for node in nodes:
        by_vlan.setdefault(str(node.get("vlan", "default")), []).append(node)
    zone_order = [str(v.get("name")) for v in vlans] + sorted(k for k in by_vlan if k not in {str(v.get("name")) for v in vlans})

    cells: list[dict] = []
    y = 40
    zone_w = 40 + _GRID_COLS * _CELL_W
    for zi, zname in enumerate(zone_order):
        meta = next((v for v in vlans if str(v.get("name")) == zname), {})
        members: list[tuple[str, dict]] = []
        for node in by_vlan.get(zname, []):
            count = max(1, int(node.get("count", 1) or 1))
            base = str(node.get("id") or node.get("name") or "node")
            for r in range(count):
                members.append((f"{base}-{r}" if count > 1 else base, node))
        rows = max(1, -(-len(members) // _GRID_COLS))
        vid = meta.get("id")
        cells.append(_cell_zone(
            f"zone-{zi}", zname, str(meta.get("cidr") or ""), 40, y,
            dmz="dmz" in zname.lower(), width=zone_w, height=50 + rows * _CELL_H,
            vlan=int(vid) if str(vid).isdigit() else None,
        ))
        for mi, (nid, node) in enumerate(members):
            os_name = str(node.get("os") or "")
            role = str(node.get("role") or "")
            ip = str(node.get("ip") or "") if int(node.get("count", 1) or 1) == 1 else ""
            cell = _cell_node(
                nid, str(node.get("name") or nid), _node_type_for(role, os_name), os_name, ip,
                60 + (mi % _GRID_COLS) * _CELL_W, y + 40 + (mi // _GRID_COLS) * _CELL_H,
            )
            data = cell["nodeData"]
            data["hostname"] = nid
            if role:
                data["role"] = role
            specs = node.get("specs") if isinstance(node.get("specs"), dict) else {}
            for src, dst in (("cores", "vcpu"), ("memory_mb", "ram_mb"), ("disk_gb", "disk_gb")):
                if specs.get(src) is not None:
                    data[dst] = str(specs[src])
            if isinstance(vid, int) or str(vid).isdigit():
                data["vlan"] = str(vid)
            services = node.get("services") or []
            if isinstance(services, list) and services:
                data["services"] = ",".join(str(x) for x in services)
            cells.append(cell)
        y += 50 + rows * _CELL_H + 40
    return {"cells": cells}


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() or c == "-" else "-" for c in text.strip().lower())
    return "-".join(p for p in out.split("-") if p)[:48]


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _centre_in(cell: dict, zone: dict) -> bool:
    pos, size = cell.get("position") or {}, cell.get("size") or {}
    zpos, zsize = zone.get("position") or {}, zone.get("size") or {}
    cx = float(pos.get("x", 0)) + float(size.get("width", 0)) / 2
    cy = float(pos.get("y", 0)) + float(size.get("height", 0)) / 2
    zx, zy = float(zpos.get("x", 0)), float(zpos.get("y", 0))
    return zx <= cx <= zx + float(zsize.get("width", 0)) and zy <= cy <= zy + float(zsize.get("height", 0))


def diagram_to_template(diagram: dict, name: str = "Range Design", *, range_id: str | None = None) -> dict:
    """Turn a designer diagram into a template the worker provisions from.

    Zones become `network.vlans`; each compute node becomes a `nodes` entry with
    `os`, `specs`, `vlan`, `ip`, `role` and `services`. A node belongs to the zone
    its centre sits in; a node outside every zone uses its VLAN ID field, matched to a
    zone with that ID or else given a VLAN of its own. Switches, clouds and zones
    are layout, not VMs, so they are not emitted.

    Returns ``{"template": {...}, "warnings": [...]}``. Warnings describe what was
    dropped or guessed; they never stop the conversion.
    """
    from .golden_images import canonical_os

    cells = [c for c in (diagram or {}).get("cells", []) or [] if isinstance(c, dict)]
    zones = [c for c in cells if c.get("nodeType") in _ZONE_TYPES]
    warnings: list[str] = []

    vlans: list[dict] = []
    zone_vlan: dict[str, str] = {}  # zone cell id -> vlan name
    used_ids: set[int] = set()
    used_names: set[str] = set()
    for zi, zone in enumerate(zones):
        data = zone.get("nodeData") or {}
        vid = _int_or_none(data.get("vlan"))
        if vid is None or vid in used_ids:
            vid = 100 + zi
            while vid in used_ids:
                vid += 1
        used_ids.add(vid)
        vname = _slug(str(data.get("label") or "")) or f"zone{zi}"
        while vname in used_names:
            vname += f"-{vid}"
        used_names.add(vname)
        vlan = {"id": vid, "name": vname}
        if data.get("cidr"):
            vlan["cidr"] = str(data["cidr"])
        vlans.append(vlan)
        zone_vlan[str(zone.get("id"))] = vname

    nodes: list[dict] = []
    seen_ids: set[str] = set()
    for cell in cells:
        ntype = str(cell.get("nodeType") or "")
        if cell.get("type") == "standard.Link" or not ntype or ntype in _NON_VM_TYPES:
            continue
        data = cell.get("nodeData") or {}
        label = str(data.get("label") or cell.get("id") or "node")
        os_name = canonical_os(str(data.get("os_template") or data.get("os") or ""))
        if not os_name:
            warnings.append(f"{label}: no OS template set; skipped")
            continue

        nid = _slug(str(data.get("hostname") or "")) or _slug(label) or _slug(str(cell.get("id"))) or "node"
        base, n = nid, 2
        while nid in seen_ids:
            nid, n = f"{base}-{n}", n + 1
        seen_ids.add(nid)

        zone = next((z for z in zones if _centre_in(cell, z)), None)
        if zone is not None:
            vlan_name = zone_vlan[str(zone.get("id"))]
        else:
            vid = _int_or_none(data.get("vlan"))
            match = next((v for v in vlans if v["id"] == vid), None) if vid is not None else None
            if match is not None:
                vlan_name = match["name"]
            else:
                vid = vid if vid is not None else 100
                vlan_name = f"vlan{vid}"
                if vlan_name not in used_names:
                    used_names.add(vlan_name)
                    used_ids.add(vid)
                    vlans.append({"id": vid, "name": vlan_name})
                    warnings.append(f"{label}: outside every zone; placed on {vlan_name} with an allocated subnet")

        node: dict = {"id": nid, "name": label, "role": str(data.get("role") or _TYPE_ROLE.get(ntype, ntype)),
                      "os": os_name, "vlan": vlan_name}
        if data.get("ip"):
            node["ip"] = str(data["ip"])
        specs = {}
        for src, dst in (("vcpu", "cores"), ("ram_mb", "memory_mb"), ("disk_gb", "disk_gb")):
            val = _int_or_none(data.get(src))
            if val is not None and val > 0:
                specs[dst] = val
        if specs:
            node["specs"] = specs
        services = data.get("services")
        if isinstance(services, str):
            services = [s.strip() for s in services.split(",") if s.strip()]
        if isinstance(services, list) and services:
            node["services"] = [str(s) for s in services]
        nodes.append(node)

    template: dict = {"name": name, "version": "1.0", "network": {"vlans": vlans}, "nodes": nodes}
    template["source"] = {"tool": "range-designer", **({"range_id": range_id} if range_id else {})}
    return {"template": template, "warnings": warnings}
