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


def _cell_zone(cid: str, label: str, cidr: str, x: int, y: int, dmz: bool = False) -> dict:
    color = _NODE_COLORS["dmz" if dmz else "subnet"]
    return {
        "type": "standard.Rectangle",
        "id": cid,
        "position": {"x": x, "y": y},
        "size": {"width": _ZONE_W, "height": _ZONE_H},
        "angle": 0,
        "nodeType": "dmz" if dmz else "subnet",
        "nodeData": {"label": label, "cidr": cidr},
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
