"""A picture for every range: its topology drawn from its template when none was saved.

The 2D views and the 3D topology viewer render a Range Designer graph
(``Range.diagram_json``). Ranges created from a template have none, so they showed
nothing. ``GET /ranges/{id}/diagram`` now answers with a diagram generated here when
none is saved, flagged ``generated``; a saved designer diagram always wins.

Generation is server-side because everything it needs is here: the template YAML (the
web app has no YAML parser) and what the last build recorded
(``provisioner_output["vms"]``). One generator means the 2D and 3D views agree.

The cells come from ``range_topology.template_to_diagram`` (lossless: opening one in
the designer and saving it provisions the same range), re-laid out for reading:

* one zone per network (VLAN, CIDR), VMs gridded inside the zone they are on;
* firewalls and routers with a NIC on two or more networks in a band between the zone
  rows, linked to each network they have a NIC on (the worker's ``interfaces`` rule,
  so a firewall that declares no ``interfaces`` stays in its one zone, because that
  is what gets built);
* other multi-NIC hosts (a sensor's monitor NIC) linked to the extra network;
* each VM labelled with name, OS and address; ``nodeData.runtime`` says whether it was
  built and what the build recorded.

Power state is the range's state: the worker records one state per range, not per VM.
"""

from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import range_topology as rt

NODE_W, NODE_H = 170, 64
GAP_X, GAP_Y = 20, 18
PAD_X, HEAD, PAD_B = 20, 52, 20
ZONE_GAP_X, ZONE_GAP_Y = 40, 40
BAND_GAP = 70  # space above and below the router band, for the links
TOP = 40
MAX_LABEL = 24

_ZONE_TYPES = frozenset({"subnet", "dmz"})
_ROUTER_ROLES = frozenset({"firewall", "router", "gateway"})  # worker/render.py _ROUTER_ROLES
_ROUTER_OS = ("pfsense", "opnsense", "vyos")  # worker/render.py _ROUTER_OS_PREFIXES


class RangeDiagramOut(BaseModel):
    """A range's topology for the 2D and 3D views."""

    range_id: str
    diagram_json: dict[str, Any] = Field(description="JointJS graph JSON: {cells: [...]}")
    generated: bool = Field(False, description="True when drawn from the template, not saved in the designer")
    source: Literal["saved", "template", "none"] = Field(
        description="saved: the designer diagram; template: generated from the range's template; none: nothing to draw")
    notes: list[str] = Field(default_factory=list, description="What a generated picture can and cannot show")


# ── what the template declares ──────────────────────────────────────────


def _is_router(node: dict) -> bool:
    os_name = str(node.get("os") or node.get("os_template") or "").lower()
    return str(node.get("role", "")).lower() in _ROUTER_ROLES or os_name.startswith(_ROUTER_OS)


def _nics(node: dict, networks: set[str]) -> list[dict]:
    """The node's NICs as the worker builds them (render._render_nics): its own VLAN
    first, then each declared interface on a network the template defines."""
    own = str(node.get("vlan", "default"))
    out: list[dict] = []
    seen: set[str] = set()
    for iface in node.get("interfaces") or []:
        name = str(iface.get("vlan")) if isinstance(iface, dict) else ""
        if (name == own or name in networks) and name not in seen:
            seen.add(name)
            out.append({"network": name, "ip": "" if name == own else str(iface.get("ip") or ""),
                        "monitor": bool(iface.get("monitor"))})
    if own not in seen:
        out.insert(0, {"network": own, "ip": "", "monitor": False})
    for nic in out:
        if nic["network"] == own:
            nic["ip"] = str(node.get("ip") or "") if int(node.get("count", 1) or 1) == 1 else ""
    return out


def _instances(doc: dict) -> dict[str, dict]:
    """Instance id -> template node, expanded by ``count`` exactly as template_to_diagram is."""
    out: dict[str, dict] = {}
    for node in doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        count = max(1, int(node.get("count", 1) or 1))
        base = str(node.get("id") or node.get("name") or "node")
        for r in range(count):
            out.setdefault(f"{base}-{r}" if count > 1 else base, node)
    return out


# ── layout ──────────────────────────────────────────────────────────────


def _short(text: str) -> str:
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def node_label(data: dict) -> str:
    """Name, OS and address, one per line; the full name stays in ``nodeData.label``."""
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    ip = str(runtime.get("ip") or data.get("ip") or "")
    lines = [_short(str(data.get("label") or "")), str(data.get("os_template") or ""), ip]
    return "\n".join(line for line in lines if line)


def _restyle_node(cell: dict, x: int, y: int) -> None:
    cell["position"] = {"x": x, "y": y}
    cell["size"] = {"width": NODE_W, "height": NODE_H}
    cell["attrs"]["label"].update({"text": node_label(cell["nodeData"]), "refY": "50%",
                                   "textVerticalAnchor": "middle", "fontFamily": "Inter, Segoe UI, sans-serif"})


def _zone_cols(members: int) -> int:
    return 2 if members <= 4 else 3


def _zone_size(members: int) -> tuple[int, int]:
    cols = _zone_cols(members)
    rows = max(1, math.ceil(members / cols))
    return (2 * PAD_X + cols * NODE_W + (cols - 1) * GAP_X,
            HEAD + rows * NODE_H + (rows - 1) * GAP_Y + PAD_B)


def _link(cid: str, src: str, dst: str, nic: dict) -> dict:
    """A NIC: the host to the network (zone) it is on. Undirected, so no arrowheads."""
    return {
        "type": "standard.Link", "id": cid, "source": {"id": src}, "target": {"id": dst}, "z": 2,
        "linkData": {"network": nic["network"], "ip": nic["ip"], "monitor": nic["monitor"]},
        "attrs": {"line": {"stroke": "#8892a6", "strokeWidth": 2, "targetMarker": {"type": "path", "d": "M 0 0"},
                           **({"strokeDasharray": "6 4"} if nic["monitor"] else {})}},
        "labels": [{"position": 0.5, "attrs": {"text": {"text": nic["ip"]}}}] if nic["ip"] else [],
    }


def layout(doc: dict, cells: list[dict]) -> list[dict]:
    """Lay ``template_to_diagram`` cells out for reading and add the NIC links."""
    zones = [c for c in cells if c.get("nodeType") in _ZONE_TYPES]
    zone_by_name = {str(z["nodeData"].get("label")): z for z in zones}
    others = [c for c in cells if c.get("nodeType") not in _ZONE_TYPES]
    instances = _instances(doc)
    networks = set(zone_by_name)

    band: list[dict] = []  # multi-homed routers, and the greyspace cloud
    members: dict[str, list[dict]] = {name: [] for name in zone_by_name}
    nics: dict[str, list[dict]] = {}
    for cell in others:
        node = instances.get(str(cell["nodeData"].get("hostname") or ""))
        if node is None:  # the greyspace cloud
            band.append(cell)
            continue
        cell_nics = _nics(node, networks)
        nics[cell["id"]] = cell_nics
        if _is_router(node) and len({n["network"] for n in cell_nics}) >= 2:
            band.append(cell)
        else:
            members.setdefault(cell_nics[0]["network"], []).append(cell)
    for group in members.values():  # a zone's own gateway first
        group.sort(key=lambda c: 0 if c.get("nodeType") in ("firewall", "router") else 1)

    order = [str(z["nodeData"].get("label")) for z in zones]
    per_row = len(order) if len(order) <= 2 else min(4, math.ceil(len(order) / 2))
    rows = [order[i:i + per_row] for i in range(0, len(order), per_row)] or [[]]
    sizes = {name: _zone_size(len(members.get(name, []))) for name in order}
    widths = [sum(sizes[n][0] for n in row) + ZONE_GAP_X * (len(row) - 1) for row in rows]
    band_w = len(band) * NODE_W + max(0, len(band) - 1) * 60
    total_w = max([*widths, band_w, NODE_W])
    split = (math.ceil(len(rows) / 2) if len(rows) > 1 else 0) if band else -1

    y = TOP
    for ri, row in enumerate(rows):
        if ri == split:
            y = _place_band(band, total_w, y)
        x = 40 + (total_w - widths[ri]) // 2
        height = max((sizes[n][1] for n in row), default=0)
        for name in row:
            zone = zone_by_name[name]
            w = sizes[name][0]
            zone["position"] = {"x": x, "y": y}
            zone["size"] = {"width": w, "height": height}
            cols = _zone_cols(len(members.get(name, [])))
            for i, cell in enumerate(members.get(name, [])):
                _restyle_node(cell, x + PAD_X + (i % cols) * (NODE_W + GAP_X), y + HEAD + (i // cols) * (NODE_H + GAP_Y))
            x += w + ZONE_GAP_X
        y += height + ZONE_GAP_Y

    links: list[dict] = []
    band_ids = {c["id"] for c in band}
    for cell in others:
        cell_nics = nics.get(cell["id"], [])
        for nic in cell_nics if cell["id"] in band_ids else cell_nics[1:]:
            zone = zone_by_name.get(nic["network"])
            if zone is not None:
                links.append(_link(f"nic-{cell['id']}-{nic['network']}", cell["id"], zone["id"], nic))
    return zones + others + links


def _place_band(band: list[dict], total_w: int, y: int) -> int:
    """The router band, centred; returns the y below it."""
    y += BAND_GAP - ZONE_GAP_Y if y > TOP else 0
    width = len(band) * NODE_W + max(0, len(band) - 1) * 60
    x = 40 + (total_w - width) // 2
    for cell in band:
        if cell.get("nodeType") == "cloud":
            cell["position"] = {"x": x, "y": y}
        else:
            _restyle_node(cell, x, y)
        x += NODE_W + 60
    return y + NODE_H + BAND_GAP


# ── the range ───────────────────────────────────────────────────────────


def _recorded_vms(rng) -> dict[str, dict]:
    """Node id -> what the last build recorded for it (provisioner_output["vms"])."""
    try:
        out = json.loads(rng.provisioner_output or "{}")
    except ValueError:
        return {}
    vms = out.get("vms") if isinstance(out, dict) else None
    prefix = f"{str(rng.id)[:8]}-"
    built: dict[str, dict] = {}
    for vm in vms if isinstance(vms, list) else []:
        if isinstance(vm, dict):
            built.setdefault(str(vm.get("node_id") or str(vm.get("name") or "").removeprefix(prefix)), vm)
    return built


def _overlay(cells: list[dict], built: dict[str, dict], state: str) -> None:
    """What the build recorded, on each VM cell. No recorded VMs: everything is planned."""
    for cell in cells:
        if cell.get("nodeType") in rt._NON_VM_TYPES or cell.get("type") == "standard.Link":
            continue
        data = cell["nodeData"]
        vm = built.get(str(data.get("hostname") or cell.get("id")))
        if not built:
            runtime: dict = {"status": "planned", "built": False}
        elif vm is None:
            runtime = {"status": "not built", "built": False}
        else:
            nics = [{"network": str(n.get("network") or ""), "ip": str(n.get("ip") or "")}
                    for n in vm.get("nics") or [] if isinstance(n, dict)]
            runtime = {"status": state, "built": True, "ip": str(vm.get("ip") or ""), "nics": nics}
        data["runtime"] = runtime
        cell["attrs"]["label"]["text"] = node_label(data)


def template_picture(template_yaml: str) -> tuple[dict, list[str]]:
    """``({cells}, notes)`` for a template; empty cells when it declares no hosts."""
    import yaml as pyyaml

    from . import safe_yaml

    try:
        doc = safe_yaml.load(template_yaml or "")
    except pyyaml.YAMLError:
        return {"cells": []}, ["The range's template does not parse, so there is nothing to draw."]
    if not isinstance(doc, dict) or not (doc.get("nodes") or doc.get("assets")):
        return {"cells": []}, ["The range's template declares no hosts, so there is nothing to draw."]
    if any(isinstance(n, dict) for n in doc.get("nodes") or []) and not doc.get("assets"):
        cells = rt.template_to_diagram(doc)["cells"]
        return {"cells": layout(doc, cells)}, []
    diagram = rt.build_template_diagram(template_yaml)
    return diagram, ["The template lists assets, not hosts: this is a sketch with default OS and addresses."]


def range_diagram(rng) -> dict:
    """The saved designer diagram, else one generated from the template and the last build."""
    saved = rng.diagram_json if isinstance(rng.diagram_json, dict) else None
    if saved and saved.get("cells"):
        return {"range_id": str(rng.id), "diagram_json": saved, "generated": False, "source": "saved", "notes": []}
    template = rng.template
    diagram, notes = template_picture(template.yaml if template else "")
    if not diagram["cells"]:
        return {"range_id": str(rng.id), "diagram_json": {"cells": []}, "generated": False, "source": "none",
                "notes": notes}
    state = rng.state.value if hasattr(rng.state, "value") else str(rng.state or "")
    built = _recorded_vms(rng)
    _overlay(diagram["cells"], built, state)
    name = f"{template.name} v{template.version}" if template else "its template"
    notes.insert(0, f"Drawn from {name}; not a saved designer diagram.")
    if built:
        notes.append(f"{len(built)} VM(s) recorded by the last build. Status is the range's state "
                     "(the worker does not record power per VM).")
    else:
        notes.append("Not built yet: names, OS and addresses are what the template plans.")
    return {"range_id": str(rng.id), "diagram_json": diagram, "generated": True, "source": "template", "notes": notes}
