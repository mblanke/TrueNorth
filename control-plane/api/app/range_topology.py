"""Generate a JointJS `diagram_json` topology for a PO's range.

The 2D range-designer (`graph.fromJSON`) and the 3D topology viewer both render
`Range.diagram_json = {"cells": [...]}`. No generator existed; this builds a
representative topology per PO from its domain (network-analysis / malware /
red-team / incident-response), laid into VLAN subnet zones. Deterministic, no LLM.
"""

from __future__ import annotations

import ipaddress
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


def _cell_zone(
    cid: str,
    label: str,
    cidr: str,
    x: int,
    y: int,
    dmz: bool = False,
    width: int = _ZONE_W,
    height: int = _ZONE_H,
    vlan: int | None = None,
) -> dict:
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
                    "label": {"position": {"name": "outside"}},
                },
                "out": {
                    "position": "right",
                    "attrs": {
                        "circle": {"fill": color, "stroke": "var(--border)", "strokeWidth": 1, "r": 5, "magnet": True}
                    },
                    "label": {"position": {"name": "outside"}},
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
            hosts.append(
                (
                    str(node.get("label") or node.get("name") or "node"),
                    str(node.get("role") or node.get("type") or "server"),
                )
            )
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
        cells.append(
            _cell_node(f"n-{i}", label, node_type, os_t, f"10.0.0.{10 + i}", 70 + (i % 4) * 150, 175 + (i // 4) * 95)
        )
    return {"cells": cells}


def count_template_hosts(template_yaml: str | None) -> int | None:
    """VMs a template provisions, counted the way the worker builds them.

    Mirrors worker/render.py (`_extract_nodes` + `render_topology`; the API may not
    import the worker): `nodes` when non-empty, else `assets`; each entry times its
    `count`; switch/cloud/zone entries without an OS are drawn, not built. None when
    the YAML does not parse or declares neither list, so a list view can tell "no
    hosts declared" from "zero hosts". Never raises.
    """
    import yaml as pyyaml

    try:
        doc = pyyaml.safe_load(template_yaml or "")
    except pyyaml.YAMLError:
        return None
    if not isinstance(doc, dict) or ("assets" not in doc and "nodes" not in doc):
        return None
    entries = doc.get("nodes") or doc.get("assets") or []
    total = 0
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            total += 1
        elif isinstance(entry, dict):
            if not entry.get("os") and not entry.get("os_template") and str(entry.get("type", "")) in _NON_VM_TYPES:
                continue
            try:
                total += max(0, int(entry.get("count", 1) or 1))
            except (TypeError, ValueError):
                total += 1
    return total


# ── Designer diagram <-> provisionable template ─────────────────────────
#
# The Range Designer edits a JointJS graph (`Range.diagram_json`); the worker only
# provisions from a template's `nodes` + `network.vlans` (worker/render.py). These two
# functions are the bridge, in both directions, so a designed topology is what gets
# built. JSON and YAML carry the same dict: the worker reads either.
#
# Round trip is lossless for templates: keys the designer has no field for ride along
# in `nodeData.template_extra`, zones keep their exact VLAN name and id, and nodes keep
# their template order. Only `count` is not preserved: each instance becomes a node.


MAX_DIAGRAM_CELLS = 2000

_ZONE_TYPES = frozenset({"subnet", "dmz"})
# Drawn for readability, never built: a switch is the port group, the cloud is outside.
_NON_VM_TYPES = frozenset({"switch", "cloud"}) | _ZONE_TYPES
# Get one NIC per zone they are linked to (`interfaces`); unlinked, they have one NIC.
_MULTI_HOMED_TYPES = frozenset({"firewall", "router"})

_TYPE_ROLE: dict[str, str] = {
    "dc": "domain_controller",
    "workstation": "workstation",
    "server": "server",
    "kali": "attack_platform",
    "router": "router",
    "firewall": "firewall",
    "seconion": "network_monitor",
}

_NODE_FIELDS = frozenset({"id", "name", "role", "os", "vlan", "ip", "specs", "services", "count"})
_VLAN_FIELDS = frozenset({"id", "name", "cidr"})

_GRID_COLS = 4
_CELL_W, _CELL_H = 150, 95


class TopologyError(ValueError):
    """The diagram cannot be converted at all (as opposed to per-node warnings)."""


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
    """Lay a full-node template out as a designer diagram, keeping everything.

    One zone per VLAN (exact name, CIDR, id), every node expanded by `count`, each
    node's OS, specs, services, IP and role in `nodeData`, and any other template
    keys in `nodeData.template_extra`. Zones come first, then nodes in template
    order. Inverse of `diagram_to_template`.
    """
    network = doc.get("network") if isinstance(doc.get("network"), dict) else {}
    vlans = [v for v in (network.get("vlans") or []) if isinstance(v, dict)]
    nodes = [n for n in (doc.get("nodes") or []) if isinstance(n, dict)]
    vlan_names = [str(v.get("name")) for v in vlans]

    # Expand counts once, remembering each instance's zone.
    instances: list[tuple[str, dict, str]] = []
    for node in nodes:
        count = max(1, int(node.get("count", 1) or 1))
        base = str(node.get("id") or node.get("name") or "node")
        zone = str(node.get("vlan", "default"))
        for r in range(count):
            instances.append((f"{base}-{r}" if count > 1 else base, node, zone))
    zone_order = vlan_names + sorted({z for _, _, z in instances} - set(vlan_names))

    zone_w = 40 + _GRID_COLS * _CELL_W
    zone_cells: list[dict] = []
    slot: dict[str, tuple[int, int]] = {}  # zone -> (top y, next index)
    y = 40
    for zi, zname in enumerate(zone_order):
        meta = next((v for v in vlans if str(v.get("name")) == zname), {})
        members = sum(1 for _, _, z in instances if z == zname)
        rows = max(1, -(-members // _GRID_COLS))
        vid = _int_or_none(meta.get("id"))
        cell = _cell_zone(
            f"zone-{zi}",
            zname,
            str(meta.get("cidr") or ""),
            40,
            y,
            dmz="dmz" in zname.lower(),
            width=zone_w,
            height=50 + rows * _CELL_H,
            vlan=vid,
        )
        extra = {k: v for k, v in meta.items() if k not in _VLAN_FIELDS}
        if extra:
            cell["nodeData"]["template_extra"] = extra
        zone_cells.append(cell)
        slot[zname] = (y, 0)
        y += 50 + rows * _CELL_H + 40

    zone_vid = {c["nodeData"]["label"]: c["nodeData"].get("vlan") for c in zone_cells}
    node_cells: list[dict] = []
    for nid, node, zname in instances:
        top, idx = slot[zname]
        slot[zname] = (top, idx + 1)
        os_name = str(node.get("os") or "")
        role = str(node.get("role") or "")
        ip = str(node.get("ip") or "") if int(node.get("count", 1) or 1) == 1 else ""
        cell = _cell_node(
            nid,
            str(node.get("name") or nid),
            _node_type_for(role, os_name),
            os_name,
            ip,
            60 + (idx % _GRID_COLS) * _CELL_W,
            top + 40 + (idx // _GRID_COLS) * _CELL_H,
        )
        data = cell["nodeData"]
        data["hostname"] = nid
        if role:
            data["role"] = role
        specs = node.get("specs") if isinstance(node.get("specs"), dict) else {}
        for src, dst in (("cores", "vcpu"), ("memory_mb", "ram_mb"), ("disk_gb", "disk_gb")):
            if specs.get(src) is not None:
                data[dst] = str(specs[src])
        extra_specs = {k: v for k, v in specs.items() if k not in ("cores", "memory_mb", "disk_gb")}
        if zone_vid.get(zname) is not None:
            data["vlan"] = str(zone_vid[zname])
        services = node.get("services") or []
        if isinstance(services, list) and services:
            data["services"] = ",".join(str(x) for x in services)
        extra = {k: v for k, v in node.items() if k not in _NODE_FIELDS}
        if extra_specs:
            extra["specs"] = extra_specs
        if extra:
            data["template_extra"] = extra
        node_cells.append(cell)
    return {"cells": zone_cells + node_cells}


def _name(text: str) -> str:
    """Identifier-safe name that leaves already-clean names (a-z 0-9 _ -) untouched."""
    out = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(text).strip().lower())
    return "-".join(p for p in out.split("-") if p)[:63]


def _int_or_none(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e9 else None  # rejects NaN and absurd coordinates


def _box(cell: dict) -> tuple[float, float, float, float] | None:
    pos, size = cell.get("position"), cell.get("size")
    if not isinstance(pos, dict):
        return None
    x, y = _num(pos.get("x", 0)), _num(pos.get("y", 0))
    w = _num((size or {}).get("width", 0)) if isinstance(size, dict) else 0.0
    h = _num((size or {}).get("height", 0)) if isinstance(size, dict) else 0.0
    if None in (x, y, w, h):
        return None
    return x, y, w, h


def _free_cidr(taken: list) -> str:
    for third in range(200, 255):
        cand = ipaddress.ip_network(f"10.{third}.0.0/24")
        if not any(cand.overlaps(t) for t in taken):
            return str(cand)
    return "10.254.254.0/24"


def diagram_to_template(diagram: dict, name: str = "Range Design", *, range_id: str | None = None) -> dict:
    """Turn a designer diagram into a template the worker provisions from.

    Zones become `network.vlans`; each compute node becomes a `nodes` entry with
    `os`, `specs`, `vlan`, `ip`, `role` and `services` (plus any `template_extra`
    it carried). A node belongs to the smallest zone its centre sits in; a node
    outside every zone uses its VLAN ID field, matched to a zone with that ID or
    else given a VLAN and subnet of its own that collide with nothing. Switches,
    clouds and zones are layout, not VMs.

    Returns ``{"template": {...}, "warnings": [...]}``. Anything malformed in a
    single cell is skipped or dropped with a warning. Raises TopologyError only
    when the diagram as a whole is unusable (not a cell list, or too large).
    """
    from .golden_images import canonical_os

    raw_cells = (diagram or {}).get("cells") if isinstance(diagram, dict) else None
    if not isinstance(raw_cells, list):
        raise TopologyError("diagram must be an object with a 'cells' list")
    if len(raw_cells) > MAX_DIAGRAM_CELLS:
        raise TopologyError(f"diagram has {len(raw_cells)} cells; the limit is {MAX_DIAGRAM_CELLS}")
    warnings: list[str] = []
    cells: list[dict] = []
    links: list[tuple[str, str]] = []  # (source id, target id): what a firewall/router is cabled to
    for c in raw_cells:
        if isinstance(c, dict) and c.get("type") == "standard.Link":
            src, dst = c.get("source"), c.get("target")
            if isinstance(src, dict) and isinstance(dst, dict) and src.get("id") and dst.get("id"):
                links.append((str(src["id"]), str(dst["id"])))
            continue
        if not isinstance(c, dict):
            continue
        if not isinstance(c.get("nodeType"), str):
            if c.get("nodeType") is not None:
                warnings.append(f"cell {str(c.get('id'))[:40]}: unreadable node type; skipped")
            continue
        if not isinstance(c.get("nodeData"), dict):
            if c.get("nodeData") is not None:
                warnings.append(f"cell {str(c.get('id'))[:40]}: unreadable properties; treated as empty")
            c = {**c, "nodeData": {}}
        cells.append(c)

    # ── zones -> vlans ──
    zones: list[tuple[dict, tuple | None]] = [(z, _box(z)) for z in cells if z["nodeType"] in _ZONE_TYPES]
    used_ids: set[int] = set()
    used_names: set[str] = set()
    requested: list[int | None] = []
    for zone, _ in zones:
        vid = _int_or_none(zone["nodeData"].get("vlan"))
        requested.append(vid if vid is not None and 1 <= vid <= 4094 else None)
    for vid in requested:  # explicit ids first, so auto ids never steal them
        if vid is not None:
            used_ids.add(vid)

    def _next_id() -> int:
        vid = 100
        while vid in used_ids:
            vid += 1
        used_ids.add(vid)
        return vid

    vlans: list[dict] = []
    networks: dict[str, object] = {}
    zone_vlan: dict[int, str] = {}  # index into zones -> vlan name
    seen_explicit: set[int] = set()
    for zi, (zone, _) in enumerate(zones):
        data = zone["nodeData"]
        label = str(data.get("label") or "")
        vid = requested[zi]
        if vid is not None and vid in seen_explicit:
            new = _next_id()
            warnings.append(f"zone {label or zi}: VLAN {vid} already used by another zone; renumbered to {new}")
            vid = new
        elif vid is None:
            vid = _next_id()
        seen_explicit.add(vid)
        vname = _name(label) or f"zone{zi}"
        while vname in used_names:
            vname = f"{vname}-{vid}"
        used_names.add(vname)
        vlan: dict = {"id": vid, "name": vname}
        if data.get("cidr"):
            try:
                net = ipaddress.ip_network(str(data["cidr"]).strip(), strict=False)
                vlan["cidr"] = str(data["cidr"]).strip()
                networks[vname] = net
            except ValueError:
                # Kept as written so the worker renders exactly what it did before (its
                # own fallback); flagged so the author fixes it.
                vlan["cidr"] = str(data["cidr"]).strip()
                warnings.append(f"zone {label or vname}: CIDR {data['cidr']!r} is not valid")
        extra = data.get("template_extra")
        if isinstance(extra, dict):
            vlan = {**{k: v for k, v in extra.items() if k not in _VLAN_FIELDS}, **vlan}
        vlans.append(vlan)
        zone_vlan[zi] = vname

    def _zone_for(cell: dict) -> int | None:
        box = _box(cell)
        if box is None:
            return None
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        best, best_area = None, None
        for zi, (_, zb) in enumerate(zones):
            if zb is None:
                continue
            zx, zy, zw, zh = zb
            if zx <= cx <= zx + zw and zy <= cy <= zy + zh and (best_area is None or zw * zh < best_area):
                best, best_area = zi, zw * zh
        return best

    zone_index = {str(z.get("id")): zi for zi, (z, _) in enumerate(zones)}
    cell_by_id = {str(c.get("id")): c for c in cells}

    def _linked_zones(cell: dict) -> list[int]:
        """Zones a cell is cabled to: a linked zone itself, or the zone a linked cell sits in."""
        cid, out = str(cell.get("id")), []
        for a, b in links:
            other = cell_by_id.get(b if a == cid else a if b == cid else "")
            if other is None:
                continue
            zi = zone_index.get(str(other.get("id"))) if other["nodeType"] in _ZONE_TYPES else _zone_for(other)
            if zi is not None and zi not in out:
                out.append(zi)
        return out

    # ── compute nodes ──
    nodes: list[dict] = []
    default_vlan: str | None = None  # shared by loose nodes that name no VLAN
    id_counts: dict[str, int] = {}
    used_ips: set = set()
    for cell in cells:
        ntype = cell["nodeType"]
        if ntype in _NON_VM_TYPES:
            continue
        data = cell["nodeData"]
        label = str(data.get("label") or cell.get("id") or "node")[:120]
        os_name = canonical_os(str(data.get("os_template") or data.get("os") or ""))
        if not os_name:
            warnings.append(f"{label}: no OS template set; skipped")
            continue

        base = _name(data.get("hostname") or "") or _name(label) or _name(cell.get("id") or "") or "node"
        n = id_counts.get(base, 0) + 1
        id_counts[base] = n
        nid = base if n == 1 else f"{base}-{n}"
        while nid in id_counts and nid != base:  # a literal "web-2" elsewhere
            n += 1
            nid = f"{base}-{n}"
        id_counts.setdefault(nid, 1)

        zi = _zone_for(cell)
        linked = _linked_zones(cell) if ntype in _MULTI_HOMED_TYPES else []
        if zi is None and linked:
            zi = linked[0]  # a gateway drawn outside the zones it is cabled to
        if zi is not None:
            vlan_name = zone_vlan[zi]
        else:
            vid = _int_or_none(data.get("vlan"))
            match = next((v for v in vlans if v["id"] == vid), None) if vid is not None else None
            if match is not None:
                vlan_name = match["name"]
            elif vid is None and default_vlan is not None:
                vlan_name = default_vlan
            else:
                wants_default = vid is None
                vid = vid if vid is not None and 1 <= vid <= 4094 and vid not in used_ids else _next_id()
                used_ids.add(vid)
                vlan_name = f"vlan{vid}"
                while vlan_name in used_names:
                    vlan_name += "-x"
                used_names.add(vlan_name)
                cidr = _free_cidr(list(networks.values()))
                networks[vlan_name] = ipaddress.ip_network(cidr)
                vlans.append({"id": vid, "name": vlan_name, "cidr": cidr})
                if wants_default:
                    default_vlan = vlan_name
                warnings.append(f"{label}: outside every zone; placed on VLAN {vid} ({cidr})")

        extra = data.get("template_extra")
        node: dict = {k: v for k, v in extra.items() if k not in _NODE_FIELDS} if isinstance(extra, dict) else {}
        node.update(
            {
                "id": nid,
                "name": label,
                "role": str(data.get("role") or _TYPE_ROLE.get(ntype, ntype)),
                "os": os_name,
                "vlan": vlan_name,
            }
        )
        if ntype in _MULTI_HOMED_TYPES:
            # One NIC per zone it is cabled to, its own zone first. The worker gives a
            # router each zone's gateway address. A template's own `interfaces` (carried
            # in template_extra) stand when the diagram has no cabling for it.
            spans = [zone_vlan[z] for z in dict.fromkeys(([zi] if zi is not None else []) + linked)]
            if len(spans) > 1:
                node["interfaces"] = [{"vlan": v} for v in spans]
            elif not linked and not node.get("interfaces") and _zone_for(cell) is None:
                warnings.append(
                    f"{label}: a {ntype} outside the zones has a single NIC and will not route "
                    "between them; link it to each zone it serves"
                )

        ip_raw = str(data.get("ip") or "").strip()
        if ip_raw:
            net = networks.get(vlan_name)
            try:
                ip = ipaddress.ip_address(ip_raw)
            except ValueError:
                # Kept as written, like an invalid zone CIDR: the author must fix it.
                node["ip"] = ip_raw
                warnings.append(f"{label}: IP {ip_raw!r} is not valid")
            else:
                reason = None
                if net is not None and ip not in net:
                    reason = f"is outside {vlan_name} ({net})"
                elif net is not None and ip in (net.network_address, net.broadcast_address):
                    reason = "is the subnet's network or broadcast address"
                elif net is not None and ip == net.network_address + 1 and ntype not in _MULTI_HOMED_TYPES:
                    reason = "is the subnet gateway (.1), which only a firewall or router should hold"
                elif ip in used_ips:
                    reason = "is already used by another node"
                if reason:
                    warnings.append(f"{label}: IP {ip} {reason}; one will be allocated")
                else:
                    used_ips.add(ip)
                    node["ip"] = str(ip)

        specs = dict(extra.get("specs")) if isinstance(extra, dict) and isinstance(extra.get("specs"), dict) else {}
        for src, dst in (("vcpu", "cores"), ("ram_mb", "memory_mb"), ("disk_gb", "disk_gb")):
            val = _int_or_none(data.get(src))
            if val is not None and val > 0:
                specs[dst] = val
            elif data.get(src) not in (None, ""):
                warnings.append(f"{label}: {src} {data.get(src)!r} is not a positive number; default used")
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
