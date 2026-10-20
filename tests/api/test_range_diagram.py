"""Every range has a picture: GET /ranges/{id}/diagram draws one from the template.

Ranges created from a template (all of them, until someone opens the designer and
saves) had no diagram_json, so the 2D and 3D views said "No saved diagram". The
endpoint now generates one: networks as zones, VMs inside the zone they are on,
multi-homed firewalls between the zones with a link per NIC, and what the last build
recorded. A saved designer diagram still wins.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import yaml
from app import range_diagram as rd
from app import range_topology as rt
from app.golden_images import canonical_os
from app.models import Range, RangeState, Template
from worker import render as worker_render

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
ROOT = Path(__file__).resolve().parents[2]
RANGES = ROOT / "content" / "ranges"
FULL_RANGES = sorted(
    p for p in RANGES.glob("*/template.yaml") if yaml.safe_load(p.read_text(encoding="utf-8-sig")).get("nodes")
)


def _text(name: str) -> str:
    return (RANGES / name / "template.yaml").read_text(encoding="utf-8-sig")


def _seed(db_session, template_yaml: str, *, state=RangeState.created, tenant=DEV_TENANT, output=None):
    tpl = Template(name="Medium Enterprise Network", version="1.0", yaml=template_yaml, tenant_id=tenant)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="Blue Lab", template_id=tpl.id, tenant_id=tenant, state=state,
                provisioner_output=json.dumps(output) if output is not None else None)
    db_session.add(rng)
    db_session.commit()
    return rng


def _split(cells):
    zones = [c for c in cells if c.get("nodeType") in ("subnet", "dmz")]
    links = [c for c in cells if c.get("type") == "standard.Link"]
    nodes = [c for c in cells if c not in zones and c not in links]
    return zones, nodes, links


def _box(c):
    return c["position"]["x"], c["position"]["y"], c["size"]["width"], c["size"]["height"]


def _inside(node, zone) -> bool:
    x, y, w, h = _box(node)
    zx, zy, zw, zh = _box(zone)
    return zx <= x and zy <= y and x + w <= zx + zw and y + h <= zy + zh


def _overlap(a, b) -> bool:
    ax, ay, aw, ah = _box(a)
    bx, by, bw, bh = _box(b)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


# ── the endpoint ──────────────────────────────────────────────────────────


def test_a_template_range_gets_a_generated_diagram(client, db_session):
    rng = _seed(db_session, _text("medium-enterprise"))
    res = client.get(f"/ranges/{rng.id}/diagram")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["generated"] is True and body["source"] == "template"
    assert "Medium Enterprise Network v1.0" in body["notes"][0]
    assert any("Not built yet" in n for n in body["notes"])
    zones, nodes, _ = _split(body["diagram_json"]["cells"])
    assert sorted(z["nodeData"]["label"] for z in zones) == ["corporate", "dmz", "management", "servers"]
    assert len(nodes) == 8
    dc = next(n for n in nodes if n["id"] == "dc01")
    assert dc["attrs"]["label"]["text"] == "Domain Controller\nwindows-server-2022\n10.10.200.10"
    assert dc["nodeData"]["runtime"] == {"status": "planned", "built": False}


def test_a_saved_designer_diagram_still_wins(client, db_session):
    rng = _seed(db_session, _text("medium-enterprise"))
    saved = {"cells": [{"type": "standard.Rectangle", "id": "a", "nodeType": "server",
                        "position": {"x": 0, "y": 0}, "nodeData": {"label": "Mine"}}]}
    assert client.put(f"/ranges/{rng.id}/diagram", json=saved).status_code == 200
    body = client.get(f"/ranges/{rng.id}/diagram").json()
    assert body == {"range_id": str(rng.id), "diagram_json": saved, "generated": False, "source": "saved",
                    "notes": []}


def test_a_template_with_no_hosts_says_so(client, db_session):
    rng = _seed(db_session, "name: empty\nnodes: []\n")
    body = client.get(f"/ranges/{rng.id}/diagram").json()
    assert body["source"] == "none" and body["generated"] is False
    assert body["diagram_json"] == {"cells": []}
    assert "declares no hosts" in body["notes"][0]


def test_the_diagram_is_tenant_scoped(client, db_session):
    from app.models import Tenant

    other = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:6]}")
    db_session.add(other)
    db_session.flush()
    rng = _seed(db_session, _text("medium-enterprise"), tenant=other.id)
    assert client.get(f"/ranges/{rng.id}/diagram").status_code == 404


def test_what_the_build_recorded_is_drawn(client, db_session):
    rng = _seed(db_session, _text("medium-enterprise"), state=RangeState.running, output={"vms": []})
    prefix = str(rng.id)[:8]
    rng.provisioner_output = json.dumps({"vms": [
        {"node_id": "dc01", "name": f"{prefix}-dc01", "ip": "10.10.200.10",
         "nics": [{"network": "tn-servers", "ip": "10.10.200.10"}]},
        # Older records carry only the VM name.
        {"name": f"{prefix}-ws01", "ip": "10.10.100.57", "nics": []},
    ]})
    db_session.commit()
    body = client.get(f"/ranges/{rng.id}/diagram").json()
    _, nodes, _ = _split(body["diagram_json"]["cells"])
    by_id = {n["id"]: n for n in nodes}
    assert by_id["dc01"]["nodeData"]["runtime"]["status"] == "running"
    assert by_id["dc01"]["nodeData"]["runtime"]["nics"] == [{"network": "tn-servers", "ip": "10.10.200.10"}]
    # The address the build recorded, not the one the template planned.
    assert by_id["ws01"]["nodeData"]["runtime"]["ip"] == "10.10.100.57"
    assert by_id["ws01"]["attrs"]["label"]["text"].endswith("10.10.100.57")
    assert by_id["web01"]["nodeData"]["runtime"] == {"status": "not built", "built": False}
    assert any("2 VM(s) recorded" in n and "power per VM" in n for n in body["notes"])


# ── the layout ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", FULL_RANGES, ids=lambda p: p.parent.name)
def test_layout_is_readable(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    diagram, _ = rd.template_picture(path.read_text(encoding="utf-8-sig"))
    zones, nodes, _ = _split(diagram["cells"])
    zone_by_name = {z["nodeData"]["label"]: z for z in zones}
    instances = rd._instances(doc)
    networks = set(zone_by_name)
    for i, a in enumerate(zones):
        assert not any(_overlap(a, b) for b in zones[i + 1:]), a["id"]
    for i, a in enumerate(nodes):
        assert not any(_overlap(a, b) for b in nodes[i + 1:]), a["id"]
    for node in nodes:
        tmpl = instances.get(node["nodeData"].get("hostname", ""))
        if tmpl is None:
            continue  # the greyspace cloud
        nics = rd._nics(tmpl, networks)
        multi_router = rd._is_router(tmpl) and len({n["network"] for n in nics}) > 1
        home = zone_by_name[nics[0]["network"]]
        if multi_router:  # between the zones, in no zone
            assert not any(_overlap(node, z) for z in zones), node["id"]
        else:  # inside its own network's zone
            assert _inside(node, home), node["id"]


def test_multi_homed_firewall_sits_between_networks_with_a_link_per_nic():
    diagram, _ = rd.template_picture(_text("soc-training"))
    zones, nodes, links = _split(diagram["cells"])
    zone_names = {z["id"]: z["nodeData"]["label"] for z in zones}
    fw = {zone_names[link["target"]["id"]]: link["linkData"] for link in links if link["source"]["id"] == "fw01"}
    assert fw == {
        "attacker_infra": {"network": "attacker_infra", "ip": "10.60.200.1", "monitor": False},
        "victim_network": {"network": "victim_network", "ip": "10.60.201.1", "monitor": False},
        "soc_tools": {"network": "soc_tools", "ip": "10.60.202.1", "monitor": False},
        "network_monitoring": {"network": "network_monitoring", "ip": "10.60.203.1", "monitor": False},
        "management": {"network": "management", "ip": "10.60.204.1", "monitor": False},
    }
    # Security Onion stays on soc_tools; its monitor NIC is a dashed link to the tapped network.
    (so,) = [link for link in links if link["source"]["id"] == "seconion01"]
    assert so["linkData"]["monitor"] is True and zone_names[so["target"]["id"]] == "network_monitoring"
    assert so["attrs"]["line"]["strokeDasharray"]
    fw01 = next(n for n in nodes if n["id"] == "fw01")
    top_row = [z for z in zones if z["position"]["y"] < fw01["position"]["y"]]
    below = [z for z in zones if z["position"]["y"] > fw01["position"]["y"]]
    assert top_row and below  # in a band between the zone rows


def test_a_single_nic_firewall_is_drawn_where_it_is_built():
    # red-vs-blue's perimeter firewalls declare no `interfaces` (off the path until issue
    # #235): the worker gives each one NIC, on management, so that is where they are drawn.
    diagram, _ = rd.template_picture(_text("red-vs-blue"))
    zones, nodes, links = _split(diagram["cells"])
    mgmt = next(z for z in zones if z["nodeData"]["label"] == "management")
    for fw_id in ("fw01", "fw02"):
        assert not [link for link in links if link["source"]["id"] == fw_id]
        assert _inside(next(n for n in nodes if n["id"] == fw_id), mgmt)


def test_large_enterprise_is_drawn_whole():
    diagram, notes = rd.template_picture(_text("large-enterprise"))
    zones, nodes, links = _split(diagram["cells"])
    assert (len(zones), len(nodes), notes) == (9, 50, [])
    assert {link["source"]["id"] for link in links} == {"fw01", "ids01"}
    zone_names = {z["id"]: z["nodeData"]["label"] for z in zones}
    (tap,) = [link for link in links if link["source"]["id"] == "ids01"]
    assert tap["linkData"] == {"network": "network_monitoring", "ip": "", "monitor": True}
    assert zone_names[tap["target"]["id"]] == "network_monitoring"
    # ot_scada is an enclave until issue #235: no firewall link reaches it.
    assert "ot_scada" not in {zone_names[link["target"]["id"]] for link in links}


def test_an_assets_template_is_a_labelled_sketch():
    # No shipped template uses `assets` since small-enterprise moved to `nodes`; its old form.
    sketch = ("name: sketch\ninputs: {cidr_mgmt: 10.10.0.0/24, cidr_user: 10.20.0.0/24}\nassets:\n"
              "  - {role: jump, type: vm, os: ubuntu-2404, count: 1, tags: [mgmt]}\n"
              "  - {role: user, type: vm, os: windows-11, count: 3, tags: [user]}\n")
    diagram, notes = rd.template_picture(sketch)
    assert diagram["cells"] and "sketch" in notes[0]


def test_small_enterprise_is_three_networks_behind_one_firewall():
    diagram, notes = rd.template_picture(_text("small-enterprise"))
    zones, nodes, links = _split(diagram["cells"])
    assert notes == [] and sorted(z["nodeData"]["label"] for z in zones) == ["mgmt", "srv", "user"]
    zone_names = {z["id"]: z["nodeData"]["label"] for z in zones}
    assert {zone_names[link["target"]["id"]]: link["linkData"]["ip"] for link in links
            if link["source"]["id"] == "fw-01"} == {"mgmt": "10.0.1.1", "srv": "10.0.2.1", "user": "10.0.3.1"}
    assert sorted(n["id"] for n in nodes) == ["dc-01", "fw-01", "jump-box", "ws-01", "ws-02", "ws-03"]


@pytest.mark.parametrize("path", FULL_RANGES, ids=lambda p: p.parent.name)
def test_saving_the_generated_diagram_provisions_the_same_range(path):
    """Open in the designer, Save: the range must not change."""
    original = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    diagram, _ = rd.template_picture(path.read_text(encoding="utf-8-sig"))
    back = rt.diagram_to_template(diagram, original.get("name", "x"))
    assert all("is not valid" in w for w in back["warnings"]), back["warnings"]
    rendered = [worker_render.render_topology(t, "r-test", lambda a: a) for t in (back["template"], original)]
    assert rendered[0]["vm_definitions"] == rendered[1]["vm_definitions"]
    assert rendered[0]["network_definitions"] == rendered[1]["network_definitions"]
    assert back["template"]["nodes"] == [{**n, "os": canonical_os(n["os"])} for n in original["nodes"]]
