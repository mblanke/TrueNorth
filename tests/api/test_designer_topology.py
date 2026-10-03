"""Range Designer edits must be what gets provisioned.

Before this, the designer saved a JointJS graph the worker never read, and its YAML
export used keys (`os_template`, `vcpu`, ...) the renderer ignored, so every node fell
back to its stencil type as an OS and default specs. These tests hold the bridge:
template -> diagram -> template is lossless for what the worker reads, the save
endpoint repoints the range at a range-owned template without touching shared ones,
and legacy exports still render.
"""

from __future__ import annotations

import ipaddress
import time
import uuid
from pathlib import Path

import pytest
import yaml
from app import range_topology as rt
from app.golden_images import canonical_os
from app.models import Range, RangeState, Template
from worker import render as worker_render

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
ROOT = Path(__file__).resolve().parents[2]
FULL_RANGES = sorted(
    p for p in (ROOT / "content" / "ranges").glob("*/template.yaml")
    if yaml.safe_load(p.read_text(encoding="utf-8-sig")).get("nodes")
)


def _rendered(tpl: dict) -> dict:
    """Everything provisioning consumes, in order: every VM field and every network."""
    out = worker_render.render_topology(tpl, "r-test", lambda a: a)
    return {"vms": out["vm_definitions"], "networks": out["network_definitions"], "vlan_map": out["vlan_map"]}


def _invalid_cidr_warnings_only(warnings: list[str]) -> bool:
    # medium-enterprise ships 10.10.300.0/24 (docs/vm-build-sheet.md §7): flagged, kept as written.
    return all("is not valid" in w for w in warnings)


def _make_po(db_session) -> uuid.UUID:
    """A real qualification + PO, built like tests/api/test_qsp_curriculum_map.py does."""
    from app.models import PerformanceObjective, POTier, Qualification

    qual = Qualification(id=uuid.uuid4(), qsp_code="ALJQ", nqual="NQ-T", dp_order=1, track="t",
                         rank_level=1, title="Topology test qualification")
    db_session.add(qual)
    db_session.flush()
    po = PerformanceObjective(id=uuid.uuid4(), qualification_id=qual.id, po_code="PO_999",
                              title="Analyse traffic", tier=POTier("core"), duration_min=120, critical_events="[]")
    db_session.add(po)
    db_session.flush()
    return po.id


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
    assert _invalid_cidr_warnings_only(back["warnings"]), back["warnings"]
    assert _rendered(back["template"]) == _rendered(original)
    # Not just what the worker reads today: every authored key survives, in order.
    assert back["template"]["nodes"] == [{**n, "os": canonical_os(n["os"])} for n in original["nodes"]]
    assert back["template"]["network"]["vlans"] == original["network"]["vlans"]


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
        {"id": 100, "name": "dmz", "cidr": "172.16.5.0/24"},
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
    assert {"id": 300, "name": "vlan300", "cidr": "10.200.0.0/24"} in out["template"]["network"]["vlans"]
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


# ── adversarial review fixes ──────────────────────────────────────────────


def test_count_expands_to_one_node_per_instance_with_identical_vm_names():
    tpl = {"name": "c", "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.3.0.0/24"}]},
           "nodes": [{"id": "ws", "os": "windows-11", "vlan": "lan", "count": 3, "specs": {"cores": 2}}]}
    back = rt.diagram_to_template(rt.template_to_diagram(tpl), "c")["template"]
    assert [n["id"] for n in back["nodes"]] == ["ws-0", "ws-1", "ws-2"]
    assert [v["name"] for v in _rendered(tpl)["vms"]] == [v["name"] for v in _rendered(back)["vms"]]


def test_starter_layout_gateway_outside_zones_gets_a_collision_free_vlan():
    """build_diagram / asset starters put fw01 above the zones: it must not share a tag or subnet."""
    diagram = {"cells": [
        _cell("fw01", "firewall", 280, 20, label="Gateway", os_template="pfsense", ip="10.50.0.1"),
        _zone("z0", "attacker-infra", "10.50.30.0/24", 40, 130),
        _zone("z1", "victims", "10.100.3.0/24", 40, 400),
        _cell("k", "kali", 70, 175, label="kali", os_template="kali-2024"),
    ]}
    out = rt.diagram_to_template(diagram)
    vlans = out["template"]["network"]["vlans"]
    ids = [v["id"] for v in vlans]
    assert len(ids) == len(set(ids)), vlans
    nets = [ipaddress.ip_network(v["cidr"]) for v in vlans]
    assert not any(a.overlaps(b) for i, a in enumerate(nets) for b in nets[i + 1:]), vlans
    fw = out["template"]["nodes"][0]
    assert "ip" not in fw  # 10.50.0.1 is not in its VLAN's subnet
    assert any("single NIC" in w for w in out["warnings"])
    rendered = worker_render.render_topology(out["template"], "r", lambda a: a)
    assert len(set(rendered["vlan_map"].values())) == len(rendered["vlan_map"])


def test_duplicate_explicit_vlan_id_is_renumbered_with_a_warning():
    diagram = {"cells": [_zone("a", "A", "10.1.0.0/24", 0, 0, vlan="100"),
                         _zone("b", "B", "10.2.0.0/24", 0, 500, vlan="100")]}
    out = rt.diagram_to_template(diagram)
    assert [v["id"] for v in out["template"]["network"]["vlans"]] == [100, 101]
    assert any("renumbered" in w for w in out["warnings"])


def test_explicit_vlan_id_is_not_stolen_by_an_earlier_auto_zone():
    diagram = {"cells": [_zone("a", "A", "10.1.0.0/24", 0, 0), _zone("b", "B", "10.2.0.0/24", 0, 500, vlan="100")]}
    vlans = rt.diagram_to_template(diagram)["template"]["network"]["vlans"]
    assert [v["id"] for v in vlans] == [101, 100]


def test_node_joins_the_smallest_zone_containing_it():
    diagram = {"cells": [
        _zone("corp", "Corp", "10.1.0.0/16", 0, 0, 1000, 1000),
        _zone("dmz", "DMZ", "10.1.9.0/24", 100, 100, 300, 300),
        _cell("w", "server", 150, 150, label="web", os_template="ubuntu-24.04"),
    ]}
    assert rt.diagram_to_template(diagram)["template"]["nodes"][0]["vlan"] == "dmz"


@pytest.mark.parametrize("bad", [
    {"position": {"x": "abc", "y": 0}}, {"position": {"x": None, "y": 0}}, {"position": [1, 2]},
    {"nodeData": "x"}, {"nodeData": [1]}, {"nodeType": ["x"]}, {"size": "big"},
])
def test_malformed_cells_are_warned_about_not_500s(client, bad):
    cell = {**_cell("a", "server", 0, 0, label="a", os_template="ubuntu-24.04"), **bad}
    res = client.post("/templates/from-diagram", json={"diagram_json": {"cells": [cell, "junk", 7]}})
    assert res.status_code == 200, res.text


def test_oversized_or_shapeless_diagrams_are_422(client):
    big = {"cells": [_cell(f"c{i}", "server", 0, 0, label="x") for i in range(rt.MAX_DIAGRAM_CELLS + 1)]}
    assert client.post("/templates/from-diagram", json={"diagram_json": big}).status_code == 422
    assert client.post("/templates/from-diagram", json={"diagram_json": {"cells": "nope"}}).status_code == 422


def test_many_duplicate_labels_convert_in_linear_time():
    diagram = {"cells": [_cell(f"c{i}", "server", 0, 0, label="web", os_template="ubuntu-24.04")
                         for i in range(rt.MAX_DIAGRAM_CELLS)]}
    t0 = time.perf_counter()
    ids = [n["id"] for n in rt.diagram_to_template(diagram)["template"]["nodes"]]
    assert len(set(ids)) == len(ids)
    assert time.perf_counter() - t0 < 2.0


@pytest.mark.parametrize("ip,ntype,kept,warned", [
    ("10.2.0.50", "server", True, False), ("192.168.9.9", "server", False, True),
    ("10.2.0.1", "server", False, True), ("10.2.0.1", "firewall", True, False),
    ("10.2.0.0", "server", False, True),
    ("not-an-ip", "server", True, True),  # kept as written and flagged, like a bad CIDR
])
def test_ip_validation(ip, ntype, kept, warned):
    diagram = {"cells": [_zone("z", "LAN", "10.2.0.0/24", 0, 0),
                         _cell("a", ntype, 50, 50, label="n", os_template="pfsense", ip=ip)]}
    out = rt.diagram_to_template(diagram)
    assert ("ip" in out["template"]["nodes"][0]) is kept
    assert bool(out["warnings"]) is warned


def test_loose_nodes_without_a_vlan_share_one_default_vlan():
    diagram = {"cells": [_cell(f"c{i}", "server", 0, 0, label=f"s{i}", os_template="rocky-9") for i in range(5)]}
    tpl = rt.diagram_to_template(diagram)["template"]
    assert len(tpl["network"]["vlans"]) == 1
    assert {n["vlan"] for n in tpl["nodes"]} == {tpl["network"]["vlans"][0]["name"]}


def test_duplicate_ip_is_dropped_on_the_second_node():
    diagram = {"cells": [_zone("z", "LAN", "10.2.0.0/24", 0, 0),
                         _cell("a", "server", 50, 50, label="a", os_template="rocky-9", ip="10.2.0.9"),
                         _cell("b", "server", 50, 50, label="b", os_template="rocky-9", ip="10.2.0.9")]}
    nodes = rt.diagram_to_template(diagram)["template"]["nodes"]
    assert nodes[0]["ip"] == "10.2.0.9" and "ip" not in nodes[1]


def test_a_template_shared_with_another_range_is_copied_not_rewritten(client, db_session):
    rng_a, _ = _seed(db_session)
    t_a = client.post(f"/ranges/{rng_a.id}/topology", json={"diagram_json": _DIAGRAM}).json()["template_id"]
    # Range B is created on A's designer template and built.
    rng_b = Range(name="B", template_id=uuid.UUID(t_a), tenant_id=DEV_TENANT, state=RangeState.running)
    db_session.add(rng_b)
    db_session.commit()
    before = db_session.get(Template, uuid.UUID(t_a)).yaml

    edited = {"cells": [*_DIAGRAM["cells"], _cell("x", "kali", 200, 50, label="evil", os_template="kali-2024")]}
    res = client.post(f"/ranges/{rng_a.id}/topology", json={"diagram_json": edited}).json()
    assert res["created"] is True and res["template_id"] != t_a
    assert db_session.get(Template, uuid.UUID(t_a)).yaml == before  # B's template untouched
    db_session.refresh(rng_b)
    assert str(rng_b.template_id) == t_a


def test_po_links_follow_the_range_to_its_new_template(client, db_session):
    from app.models import RangeObjectiveMap

    rng, shared = _seed(db_session)
    po_id = _make_po(db_session)
    db_session.add(RangeObjectiveMap(template_id=shared.id, po_id=po_id, source="catalogue", tenant_id=DEV_TENANT))
    db_session.commit()

    before = client.get(f"/qsp/ranges/{rng.id}/curriculum").json()
    new_id = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM}).json()["template_id"]
    after = client.get(f"/qsp/ranges/{rng.id}/curriculum").json()
    assert after["objective_count"] == before["objective_count"] == 1
    maps = db_session.query(RangeObjectiveMap).filter(RangeObjectiveMap.template_id == uuid.UUID(new_id)).all()
    assert [(m.po_id, m.source) for m in maps] == [(po_id, "catalogue")]
    # The source template keeps its own links.
    assert db_session.query(RangeObjectiveMap).filter(RangeObjectiveMap.template_id == shared.id).count() == 1


def test_saved_copy_keeps_the_source_templates_other_keys(client, db_session):
    rng, shared = _seed(db_session)
    shared.yaml = yaml.safe_dump({"name": "s", "description": "ROE: no egress", "policies": {"egress": "deny"},
                                  "network": {"vlans": [], "dns": "10.0.0.53"}, "nodes": []})
    db_session.commit()
    tpl = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM}).json()["template"]
    assert tpl["description"] == "ROE: no egress"
    assert tpl["policies"] == {"egress": "deny"}
    assert tpl["network"]["dns"] == "10.0.0.53"


def test_failed_range_with_built_vms_is_409_but_a_never_built_one_is_editable(client, db_session):
    rng, _ = _seed(db_session, state=RangeState.failed)
    rng.provisioner_output = '{"vms": [{"name": "x"}]}'
    db_session.commit()
    assert client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM}).status_code == 409
    rng.provisioner_output = None
    db_session.commit()
    assert client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM}).status_code == 200


def test_state_change_between_check_and_write_is_409_and_writes_nothing(client, db_session, monkeypatch):
    """A provision that commits after the state check must not have its template swapped."""
    rng, shared = _seed(db_session)
    real = rt.diagram_to_template

    def racing(*a, **k):
        db_session.query(Range).filter(Range.id == rng.id).update({Range.state: RangeState.provisioning})
        return real(*a, **k)

    monkeypatch.setattr(rt, "diagram_to_template", racing)
    before = db_session.query(Template).count()
    res = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DIAGRAM})
    assert res.status_code == 409
    assert db_session.query(Template).count() == before


# ── a firewall cabled to several zones gets a NIC in each ─────────────────


def test_linked_firewall_spans_the_zones_it_is_cabled_to():
    """The designer's links used to be dropped, so every firewall had one NIC and routed nothing."""
    diagram = {"cells": [
        _cell("fw", "firewall", 280, 20, label="Gateway", os_template="pfsense"),
        _zone("z0", "corp", "10.1.0.0/24", 0, 130),
        _zone("z1", "dmz", "10.2.0.0/24", 0, 400),
        _cell("web", "server", 50, 450, label="web", os_template="ubuntu-24.04"),
        {"type": "standard.Link", "id": "l1", "source": {"id": "fw"}, "target": {"id": "z0"}},
        {"type": "standard.Link", "id": "l2", "source": {"id": "web"}, "target": {"id": "fw"}},  # via a node
    ]}
    out = rt.diagram_to_template(diagram)
    fw = out["template"]["nodes"][0]
    assert fw["vlan"] == "corp"  # outside every zone, so it joins the first one it is cabled to
    assert fw["interfaces"] == [{"vlan": "corp"}, {"vlan": "dmz"}]
    assert not any("single NIC" in w for w in out["warnings"])
    assert [v["name"] for v in out["template"]["network"]["vlans"]] == ["corp", "dmz"]  # no stray VLAN

    vms = {v["node_id"]: v for v in worker_render.render_topology(out["template"], "r", lambda a: a)["vm_definitions"]}
    assert [(n["network"], n["ip"]) for n in vms["gateway"]["nics"]] == [("corp", "10.1.0.1"), ("dmz", "10.2.0.1")]
    assert vms["web"]["gateway"] == "10.2.0.1"
