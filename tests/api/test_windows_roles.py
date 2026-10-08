"""Windows Server roles: ticking one sizes the VM, picks how it is installed, and is checked.

The designer's role grid used to be decoration: the ids it wrote matched nothing the
templates used, nothing read them, and specs never moved. These tests hold the
catalogue (one copy in the API, one in the worker, identical), the sizing rule
(largest role minimum wins), the placement rules, and their use on save and render.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from app import range_topology as rt
from app import windows_roles as api_roles
from app.golden_images import canonical_os
from worker import render as worker_render
from worker import windows_roles as worker_roles

ROOT = Path(__file__).resolve().parents[2]
API_FILE = ROOT / "control-plane" / "api" / "app" / "windows_roles.py"
WORKER_FILE = ROOT / "control-plane" / "worker" / "worker" / "windows_roles.py"
RANGES = sorted((ROOT / "content" / "ranges").glob("*/template.yaml"))
WS22 = "windows-server-2022"


def _nodes(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    return [{**n, "os": canonical_os(str(n.get("os", "")))} for n in doc.get("nodes") or []]


# ── catalogue ────────────────────────────────────────────────────────────


def test_api_and_worker_copies_are_identical():
    assert API_FILE.read_bytes() == WORKER_FILE.read_bytes(), (
        "copy control-plane/api/app/windows_roles.py over the worker's copy"
    )


def test_catalogue_is_consistent():
    ids = [r["id"] for r in api_roles.ROLES]
    assert len(ids) == len(set(ids))
    aliases = [a for r in api_roles.ROLES for a in r.get("aliases", ())]
    assert len(aliases) == len(set(aliases)), "an alias names two roles"
    assert not set(aliases) & set(ids)
    for r in api_roles.ROLES:
        assert r["group"] in api_roles.GROUPS
        for key in ("vcpu", "ram_mb", "disk_gb"):
            assert r["min"][key] <= r.get("recommended", r["min"])[key], r["id"]
        for kind, other in r.get("requires", ()):
            assert kind == "in_range" and other in api_roles.BY_ID, r["id"]
        assert set(r.get("conflicts", ())) <= set(ids)
        inst = r["install"]
        assert (inst["method"] == "feature" and inst["features"]) or (inst["method"] == "image" and inst["images"])


def test_the_roles_people_ask_for_are_offered():
    for rid in ("ad-ds", "exchange", "sharepoint", "mecm", "sql-server", "ad-cs", "dns", "dhcp", "file", "wsus"):
        assert rid in api_roles.BY_ID


@pytest.mark.parametrize("name,role", [
    ("active_directory", "ad-ds"), ("exchange_2019", "exchange"), ("sharepoint_2019", "sharepoint"),
    ("mssql_2022", "sql-server"), ("sccm", "mecm"), ("smb", "file"), ("print_spooler", "print"),
    ("adcs", "ad-cs"), ("ad_cs", "ad-cs"), ("osisoft_pi_sim", "historian"),
    # the ids the designer grid wrote before the catalogue
    ("ca", "ad-cs"), ("file-print", "file"), ("sql-server", "sql-server"), ("Exchange", "exchange"),
])
def test_template_and_legacy_names_select_their_role(name, role):
    assert api_roles.role_id(name) == role


def test_non_role_services_are_left_alone():
    assert api_roles.roles_of(["rdp", "owa", "smtp", "gpo", "nginx"]) == []
    assert api_roles.roles_of("dns,active_directory,ssh") == ["ad-ds", "dns"]


# ── sizing, install, rules ───────────────────────────────────────────────


def test_largest_minimum_wins_per_resource():
    specs = api_roles.min_specs(["file", "sql-server", "ad-cs"])
    want = {k: max(api_roles.BY_ID[r]["min"][k] for r in ("file", "sql-server", "ad-cs"))
            for k in ("vcpu", "ram_mb", "disk_gb")}
    assert specs == want
    assert api_roles.min_specs([]) == api_roles.BASE_SPECS


def test_image_role_replaces_the_os_template_and_features_are_merged():
    assert api_roles.role_image(["exchange"], WS22) == "srv2022-exchange2019"
    assert api_roles.role_image(["sharepoint"], "windows-server-2019") == "srv2019-sharepoint2019"
    assert api_roles.role_image(["dns"], WS22) is None
    feats = api_roles.role_features(["ad-ds", "dns"])
    assert feats.count("DNS") == 1 and "AD-Domain-Services" in feats


def test_two_product_images_on_one_vm_is_an_error():
    errors, _ = api_roles.check_nodes([{"name": "app", "os": WS22, "services": ["exchange", "mssql_2022"]}])
    assert any("separate VMs" in e for e in errors)


def test_exchange_on_a_dc_is_an_error():
    errors, _ = api_roles.check_nodes([{"name": "dc", "os": WS22, "services": ["active_directory", "exchange"]}])
    assert any("same VM" in e for e in errors)


def test_image_missing_for_the_os_is_an_error():
    errors, _ = api_roles.check_nodes([{"name": "x", "os": "windows-server-2019", "services": ["exchange"]}])
    assert any("no exchange image" in e for e in errors)


def test_missing_dependency_is_only_a_warning():
    errors, warnings = api_roles.check_nodes([
        {"name": "dc", "os": WS22, "services": ["ad-ds"]},
        {"name": "sp", "os": WS22, "services": ["sharepoint"]},
    ])
    assert errors == []
    assert any("sp: sharepoint expects a sql-server" in w for w in warnings)


def test_roles_only_count_on_windows_server():
    # vpn on a firewall and rsat on a workstation are just service names there
    errors, warnings = api_roles.check_nodes([
        {"name": "fw", "os": "pfsense-2.7", "services": ["vpn"]},
        {"name": "ws", "os": "windows-11", "services": ["rsat", "exchange", "sql"]},
    ])
    assert errors == [] and warnings == []


@pytest.mark.parametrize("path", RANGES, ids=lambda p: p.parent.name)
def test_shipped_ranges_pass_the_role_rules(path):
    errors, _ = worker_roles.check_nodes(_nodes(path))
    assert errors == []


# ── designer save ────────────────────────────────────────────────────────


def _cell(cid, x, y, **data):
    return {"type": "standard.Rectangle", "id": cid, "nodeType": "server", "position": {"x": x, "y": y},
            "size": {"width": 120, "height": 80}, "nodeData": data}


def _diagram(*nodes):
    zone = {"type": "standard.Rectangle", "id": "z", "nodeType": "subnet", "position": {"x": 0, "y": 0},
            "size": {"width": 900, "height": 400}, "nodeData": {"label": "LAN", "cidr": "10.9.0.0/24"}}
    return {"cells": [zone, *nodes]}


def test_save_raises_specs_to_the_role_minimum_and_keeps_higher_ones():
    out = rt.diagram_to_template(_diagram(
        _cell("a", 20, 20, label="EXCH", os_template=WS22, vcpu="2", ram_mb="4096", disk_gb="500",
              services="exchange,owa"),
        _cell("d", 200, 20, label="DC", os_template=WS22, services="active_directory"),
    ))
    exch, dc = out["template"]["nodes"]
    assert exch["specs"] == {"cores": 4, "memory_mb": 16384, "disk_gb": 500}
    assert exch["services"] == ["exchange", "owa"]  # written as given
    assert dc["specs"] == {"cores": 2, "memory_mb": 4096, "disk_gb": 60}
    assert out["errors"] == []
    assert any("EXCH: raised to the minimum" in w for w in out["warnings"])


def test_save_reports_role_errors():
    out = rt.diagram_to_template(_diagram(
        _cell("a", 20, 20, label="APP", os_template=WS22, services="exchange,sql-server"),
    ))
    assert out["errors"] and "separate VMs" in out["errors"][0]


def test_template_role_names_survive_the_designer_round_trip():
    tpl = {"name": "t", "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.1.0.0/24"}]},
           "nodes": [{"id": "dc01", "os": WS22, "vlan": "lan", "services": ["active_directory", "dns"],
                      "specs": {"cores": 4, "memory_mb": 8192, "disk_gb": 100}}]}
    back = rt.diagram_to_template(rt.template_to_diagram(tpl), "t")["template"]["nodes"][0]
    assert back["services"] == ["active_directory", "dns"]
    assert back["specs"] == {"cores": 4, "memory_mb": 8192, "disk_gb": 100}


def test_save_topology_endpoint_refuses_role_errors(client, db_session):
    from app.models import Range, RangeState, Template

    tpl = Template(name="s", version="1.0", yaml="name: s\nnodes: []\n",
                   tenant_id="00000000-0000-0000-0000-000000000001", is_public=False)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(name="R", template_id=tpl.id, tenant_id=tpl.tenant_id, state=RangeState.created)
    db_session.add(rng)
    db_session.commit()
    bad = _diagram(_cell("a", 20, 20, label="DC", os_template=WS22, services="ad-ds,exchange"))
    resp = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": bad})
    assert resp.status_code == 422
    assert any("same VM" in e for e in resp.json()["detail"]["errors"])


def test_catalogue_endpoint(client):
    resp = client.get("/templates/windows-roles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["groups"] == list(api_roles.GROUPS)
    exch = next(r for r in body["roles"] if r["id"] == "exchange")
    assert exch["method"] == "image" and exch["images"] == [WS22] and "ad-ds" in exch["conflicts"]


def test_validate_endpoint_includes_role_errors(client):
    text = yaml.safe_dump({"name": "t", "version": "1.0",
                           "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.1.0.0/24"}]},
                           "nodes": [{"id": "a", "name": "a", "os": WS22, "vlan": "lan",
                                      "services": ["exchange", "mssql_2022"]}]})
    body = client.post("/templates/validate", json={"yaml": text}).json()
    assert body["valid"] is False
    assert any("separate VMs" in e["message"] for e in body["errors"])


# ── worker render ────────────────────────────────────────────────────────


def _tpl(*nodes, **top):
    return {"name": "t", "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.1.0.0/24"}]},
            "nodes": [{"vlan": "lan", **n} for n in nodes], **top}


def test_render_sizes_picks_the_role_image_and_plans_features():
    out = worker_render.render_topology(_tpl(
        {"id": "dc01", "os": WS22, "services": ["active_directory", "dns"], "ad_domain": "corp.test"},
        {"id": "dc02", "os": WS22, "services": ["active_directory"], "ad_domain": "corp.test"},
        {"id": "sql", "os": WS22, "services": ["mssql_2022"], "specs": {"cores": 2}},
        {"id": "lnx", "os": "ubuntu-2404", "services": ["sql"]},
    ), "r-test", lambda a: "tpl-" + a)
    dc1, dc2, sql, lnx = out["vm_definitions"]
    assert out["role_errors"] == []
    assert dc1["roles"] == ["ad-ds", "dns"] and "AD-Domain-Services" in dc1["role_features"]
    assert (dc1["ad_domain"], dc1["ad_forest_root"]) == ("corp.test", True)
    assert dc2["ad_forest_root"] is False
    assert sql["template_name"] == "tpl-srv2022-sql2022"
    assert (sql["cores"], sql["memory_mb"], sql["disk_gb"]) == (4, 16384, 200)
    assert sql["role_features"] == []
    assert "roles" not in lnx and lnx["template_name"] == "tpl-ubuntu-2404"


def test_render_reports_unbuildable_placements():
    out = worker_render.render_topology(_tpl({"id": "a", "os": WS22, "services": ["exchange", "sccm"]}),
                                        "r-test", lambda a: a)
    assert out["role_errors"]
    with pytest.raises(worker_roles.RoleError, match="separate VMs"):
        worker_roles.require_buildable(out)


def test_an_unregistered_role_image_falls_back_to_the_bare_os():
    """Shipped ranges name exchange/sharepoint/sql; until their role images are built and
    registered (POST /golden-images), the VM still builds on the OS template."""
    registry = {WS22: "tmpl-win2022"}
    out = worker_render.render_topology(_tpl({"id": "exch", "os": WS22, "services": ["exchange"]}),
                                        "r-test", registry.get)
    (vm,) = out["vm_definitions"]
    assert vm["template_name"] == "tmpl-win2022"
    assert vm["role_image_missing"] == "srv2022-exchange2019"
    assert out["unresolved"] == []
    assert worker_roles.require_buildable(out) is out


def test_ad_forest_names_the_domain_and_its_first_dc_is_the_root():
    out = worker_render.render_topology(_tpl(
        {"id": "dc01", "os": WS22, "services": ["active_directory"], "ad_forest": "corp.range.local"},
        {"id": "dc02", "os": WS22, "services": ["active_directory"], "ad_forest": "corp.range.local"},
        {"id": "dc03", "os": WS22, "services": ["active_directory"]},
    ), "r-test", lambda a: a)
    roots = [(v["ad_domain"], v["ad_forest_root"]) for v in out["vm_definitions"]]
    assert roots == [("corp.range.local", True), ("corp.range.local", False), ("range.local", True)]
