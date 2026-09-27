"""Range Designer edits must be what gets provisioned.

Before this, the designer saved a JointJS graph the worker never read, and its YAML
export used keys (`os_template`, `vcpu`, ...) the renderer ignored, so every node fell
back to its stencil type as an OS and default specs. These tests hold the bridge:
template -> diagram -> template is lossless for what the worker reads, the save
endpoint repoints the range at a range-owned template without touching shared ones,
and legacy exports still render.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from app import range_topology as rt
from app.models import Range, RangeState, Template
from worker import render as worker_render

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
ROOT = Path(__file__).resolve().parents[2]
FULL_RANGES = sorted(
    p for p in (ROOT / "content" / "ranges").glob("*/template.yaml")
    if yaml.safe_load(p.read_text(encoding="utf-8-sig")).get("nodes")
)


def _rendered(tpl: dict) -> dict[str, tuple]:
    """What provisioning actually consumes, per node (ids are range-prefixed, so strip)."""
    out = worker_render.render_topology(tpl, "r-test", lambda a: a)
    cidr = {n["vlan_id"]: n["cidr"] for n in out["network_definitions"]}
    return {
        v["node_id"]: (v["os"], v["cores"], v["memory_mb"], v["disk_gb"], cidr.get(v["vlan_id"]), v["vlan_id"],
                       v["ip"], tuple(v["services"]))
        for v in out["vm_definitions"]
    }


def _cell(cid, ntype, x, y, w=120, h=80, **data):
    return {"type": "standard.Rectangle", "id": cid, "nodeType": ntype, "position": {"x": x, "y": y},
            "size": {"width": w, "height": h}, "nodeData": data}


def _zone(cid, label, cidr, x, y, w=600, h=200, **extra):
    return _cell(cid, "subnet", x, y, w, h, label=label, cidr=cidr, **extra)


# ── round trip: the property that makes the designer safe to provision from ──


@pytest.mark.parametrize("path", FULL_RANGES, ids=lambda p: p.parent.name)
def test_template_diagram_template_round_trip_preserves_what_provisioning_reads(path):
    original = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    diagram = rt.build_template_diagram(path.read_text(encoding="utf-8-sig"))
    back = rt.diagram_to_template(diagram, original.get("name", "x"))
    assert back["warnings"] == []
    assert _rendered(back["template"]) == _rendered(original)


def test_starter_diagram_is_not_capped_for_large_templates():
    text = (ROOT / "content/ranges/large-enterprise/template.yaml").read_text(encoding="utf-8-sig")
    n_nodes = len(yaml.safe_load(text)["nodes"])
    cells = rt.build_template_diagram(text)["cells"]
    assert n_nodes > 24
    assert len([c for c in cells if c["nodeType"] not in ("subnet", "dmz")]) == n_nodes


def test_asset_style_templates_keep_the_old_starter_layout():
    cells = rt.build_template_diagram("name: s\nassets:\n  - role: dc\n    count: 1\n")["cells"]
    assert {c["nodeType"] for c in cells} >= {"firewall", "subnet", "dc"}


# ── diagram -> template ───────────────────────────────────────────────────


def test_zones_become_vlans_and_nodes_join_the_zone_they_sit_in():
    diagram = {"cells": [
        _zone("z1", "Corporate LAN", "10.20.1.0/24", 0, 0, vlan="120"),
        _zone("z2", "DMZ", "172.16.5.0/24", 0, 300),
        _cell("a", "workstation", 50, 50, label="WS 1", os_template="windows-11", vcpu="2", ram_mb="4096",
              disk_gb="60", ip="10.20.1.10"),
        _cell("b", "server", 50, 350, label="Web", os_template="ubuntu-22.04", services="nginx, php"),
        _cell("sw", "switch", 400, 50, label="core"),
        _cell("net", "cloud", 400, 350, label="Internet"),
        {"type": "standard.Link", "id": "l1", "source": {"id": "a"}, "target": {"id": "sw"}},
    ]}
    out = rt.diagram_to_template(diagram, "demo")
    tpl = out["template"]
    assert tpl["network"]["vlans"] == [
        {"id": 120, "name": "corporate-lan", "cidr": "10.20.1.0/24"},
        {"id": 101, "name": "dmz", "cidr": "172.16.5.0/24"},
    ]
    ws, web = tpl["nodes"]
    assert ws == {"id": "ws-1", "name": "WS 1", "role": "workstation", "os": "windows-11",
                  "vlan": "corporate-lan", "ip": "10.20.1.10",
                  "specs": {"cores": 2, "memory_mb": 4096, "disk_gb": 60}}
    assert web["vlan"] == "dmz"
    assert web["os"] == "ubuntu-24.04"  # deprecated alias written out under its current name
    assert web["services"] == ["nginx", "php"]
    assert len(tpl["nodes"]) == 2  # switch, cloud, zones, link are not VMs
    assert tpl["source"]["tool"] == "range-designer"


def test_node_outside_zones_uses_its_vlan_id_field():
    diagram = {"cells": [
        _zone("z1", "Servers", "10.1.2.0/24", 0, 0, vlan="200"),
        _cell("a", "server", 900, 900, label="s1", os_template="rocky-9", vlan="200"),
        _cell("b", "server", 900, 1100, label="s2", os_template="rocky-9", vlan="300"),
    ]}
    out = rt.diagram_to_template(diagram)
    nodes = {n["id"]: n for n in out["template"]["nodes"]}
    assert nodes["s1"]["vlan"] == "servers"
    assert nodes["s2"]["vlan"] == "vlan300"
    assert {"id": 300, "name": "vlan300"} in out["template"]["network"]["vlans"]
    assert any("s2" in w for w in out["warnings"])


def test_node_without_os_is_skipped_with_a_warning_and_ids_are_unique():
    diagram = {"cells": [
        _cell("a", "server", 0, 0, label="web"),
        _cell("b", "server", 0, 0, label="Web", os_template="ubuntu-24.04"),
        _cell("c", "server", 0, 0, label="web", os_template="ubuntu-24.04"),
    ]}
    out = rt.diagram_to_template(diagram)
    assert [n["id"] for n in out["template"]["nodes"]] == ["web", "web-2"]
    assert "web: no OS template set; skipped" in out["warnings"]


def test_garbage_specs_are_dropped_not_crashed_on():
    diagram = {"cells": [_cell("a", "server", 0, 0, label="x", os_template="kali-2024", vcpu="lots", ram_mb="-1")]}
    node = rt.diagram_to_template(diagram)["template"]["nodes"][0]
    assert "specs" not in node


# ── worker: legacy exports (before the designer emitted os/specs) ─────────


def test_worker_renders_a_legacy_designer_export():
    legacy = yaml.safe_load("""
id: range-design-export
name: "Range Design Export"
nodes:
  - id: z1
    type: subnet
    label: "LAN"
    cidr: "10.0.0.0/24"
  - id: sw
    type: switch
    label: "core"
  - id: n1
    type: server
    label: "WEB01"
    ip: "10.0.0.20"
    os_template: ubuntu-22.04
    vcpu: 4
    ram_mb: 8192
    disk_gb: 100
""")
    out = worker_render.render_topology(legacy, "r-test", lambda a: "tpl-" + a)
    (vm,) = out["vm_definitions"]
    assert vm["os"] == "ubuntu-24.04"
    assert vm["template_name"] == "tpl-ubuntu-24.04"
    assert (vm["cores"], vm["memory_mb"], vm["disk_gb"]) == (4, 8192, 100)


# ── endpoints ─────────────────────────────────────────────────────────────


def _seed(db_session, *, state=RangeState.created, public=False, tenant=DEV_TENANT):
    tpl = Template(name="shared", version="1.0", yaml="name: shared\nnodes: []\n", tenant_id=tenant, is_public=public)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="Blue Lab", template_id=tpl.id, tenant_id=tenant, state=state)
    db_session.add(rng)
    db_session.commit()
    return rng, tpl


_DIAGRAM = {"cells": [
    _zone("z1", "LAN", "10.9.0.0/24", 0, 0),
    _cell("a", "dc", 50, 50, label="DC01", os_template="windows-server-2022", vcpu="4"),
]}


def test_save_topology_creates_a_range_owned_template_then_updates_it(client, db_session):
    rng, shared = _seed(db_session)
    first = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True and body["node_count"] == 1
    db_session.refresh(rng)
    assert str(rng.template_id) == body["template_id"] != str(shared.id)
    assert rng.diagram_json == _DIAGRAM
    tpl = db_session.get(Template, rng.template_id)
    assert tpl.is_public is False
    saved = yaml.safe_load(tpl.yaml)
    assert saved["source"]["range_id"] == str(rng.id)
    assert saved["nodes"][0]["os"] == "windows-server-2022"

    # Second save edits the range's own template in place; no template sprawl.
    edited = {"cells": [*_DIAGRAM["cells"],
                        _cell("b", "workstation", 200, 50, label="WS", os_template="windows-11")]}
    second = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": edited}).json()
    assert second["created"] is False and second["template_id"] == body["template_id"]
    assert second["node_count"] == 2
    assert db_session.get(Template, shared.id).yaml == "name: shared\nnodes: []\n"  # untouched


def test_save_topology_uses_the_saved_diagram_when_no_body(client, db_session):
    rng, _ = _seed(db_session)
    rng.diagram_json = _DIAGRAM
    db_session.commit()
    res = client.post(f"/ranges/{rng.id}/topology")
    assert res.status_code == 200, res.text
    assert res.json()["node_count"] == 1


def test_save_topology_never_rewrites_a_public_template(client, db_session):
    rng, shared = _seed(db_session, public=True)
    # Even if a public template claims this range, it is copied, not edited.
    shared.yaml = yaml.safe_dump({"name": "x", "nodes": [], "source": {"range_id": str(rng.id)}})
    db_session.commit()
    before = shared.yaml
    body = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM}).json()
    assert body["created"] is True
    assert db_session.get(Template, shared.id).yaml == before


@pytest.mark.parametrize("state", [RangeState.provisioning, RangeState.ready, RangeState.running,
                                   RangeState.stopped, RangeState.destroying])
def test_save_topology_refused_while_the_range_has_vms(client, db_session, state):
    rng, shared = _seed(db_session, state=state)
    res = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM})
    assert res.status_code == 409
    db_session.refresh(rng)
    assert rng.template_id == shared.id


def test_save_topology_with_no_vms_is_422(client, db_session):
    rng, _ = _seed(db_session)
    res = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": {"cells": [_zone("z", "L", "", 0, 0)]}})
    assert res.status_code == 422


def test_save_topology_is_tenant_scoped(client, db_session):
    from app.models import Tenant

    other = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:6]}")
    db_session.add(other)
    db_session.flush()
    rng, _ = _seed(db_session, tenant=other.id)
    assert client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM}).status_code == 404


def test_from_diagram_export_matches_what_save_would_provision(client, db_session):
    res = client.post("/templates/from-diagram", json={"diagram_json": _DIAGRAM, "name": "Blue Lab"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert yaml.safe_load(body["yaml"]) == body["template"]
    assert body["template"] == rt.diagram_to_template(_DIAGRAM, "Blue Lab")["template"]
