# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for the lab-profile validator (app/course_releases/lab_profile.py).

``findings`` is pure: the same document the arc2 tool and the lab-session service validate.
These tests walk its rule branches (lab.modules / lab.references / lab.size / lab.catalogue /
lab.egress) and the catalogue CSV reader.
"""

import copy

import pytest
from _release_kit import LAB_PROFILE
from app.course_releases.lab_profile import catalogue_ids, findings

# _release_kit's copy ships module_ids: [] (write_run fills it in); a standalone profile
# must name at least one module to pass the schema.
BASE = {**copy.deepcopy(LAB_PROFILE), "module_ids": ["mod_001"]}

ENABLED = {"ubuntu-lts", "win2022"}


def check(profile, *, catalogue=(ENABLED, set())):
    return findings(profile, range_modules={"mod_001"}, catalogue=catalogue)


def of(check_name, profile, **kw):
    return [m for c, m in check(profile, **kw) if c == check_name]


def with_node(profile, **over):
    """A second node cloned from the profile's first one, so size rules can trip."""
    p = copy.deepcopy(profile)
    extra = copy.deepcopy(p["nodes"][0])
    extra.update(over)
    p["nodes"].append(extra)
    return p


def test_a_valid_profile_has_no_findings():
    assert check(BASE) == []


def test_schema_violations_are_reported_with_their_path():
    p = copy.deepcopy(BASE)
    p["version"] = "one"  # schema wants an integer
    found = check(p)
    assert found and all(c == "lab.schema" for c, _ in found)
    assert any("version" in m for _, m in found)


def test_schema_errors_short_circuit_the_other_rules():
    """A document the schema refuses is not read as a profile at all."""
    p = copy.deepcopy(BASE)
    p["nodes"] = []
    found = check(p)
    assert found and {c for c, _ in found} == {"lab.schema"}


def test_a_range_module_without_a_lab_is_reported():
    found = findings(BASE, range_modules={"mod_001", "mod_002"}, catalogue=(ENABLED, set()))
    assert found == [("lab.modules", "range module mod_002 has no lab in this profile")]


def test_a_named_module_that_is_not_a_range_module_is_reported():
    p = copy.deepcopy(BASE)
    p["module_ids"] = ["mod_002"]
    found = findings(p, range_modules=set(), catalogue=(ENABLED, set()))
    assert found == [("lab.modules", "mod_002 is not a range module; theory and practical modules get no VM")]


def test_duplicate_node_names_are_reported():
    p = copy.deepcopy(BASE)
    p["nodes"].append(copy.deepcopy(p["nodes"][0]))
    assert of("lab.references", p) == ["node names are not unique"]


def test_an_undeclared_network_is_reported():
    p = copy.deepcopy(BASE)
    p["nodes"][0]["networks"].append("mgmt")
    assert of("lab.references", p) == ["node gateway joins undeclared network mgmt"]


@pytest.mark.parametrize("what", ["health_checks", "access", "evidence_checks"])
def test_references_to_unknown_nodes_are_reported(what):
    p = copy.deepcopy(BASE)
    p[what][0]["node"] = "ghost"
    found = of("lab.references", p)
    assert f"{what} names unknown node ghost" in found
    if what == "health_checks":  # gateway lost its only check, so it is unready too
        assert "nodes with no health check cannot be called ready: gateway" in found


@pytest.mark.parametrize("kind", ["tcp", "http", "ssh"])
def test_ported_health_checks_need_a_port(kind):
    p = copy.deepcopy(BASE)
    p["health_checks"][0] = {"node": "gateway", "kind": kind, "timeout_s": 300}
    assert of("lab.references", p) == [f"{kind} health check on gateway needs a port"]


def test_a_health_check_without_a_port_kind_is_fine():
    """``tools`` checks probe the agent, not a socket, so they need no port."""
    p = copy.deepcopy(BASE)
    p["health_checks"][0] = {"node": "gateway", "kind": "tools", "timeout_s": 60}
    assert of("lab.references", p) == []


def test_an_unchecked_node_cannot_be_called_ready():
    p = with_node(BASE, name="sensor")
    p["health_checks"] = list(p["health_checks"])  # the new node has none
    assert of("lab.references", p) == ["nodes with no health check cannot be called ready: sensor"]


def test_size_rules_trip_on_node_count_and_totals():
    p = with_node(BASE, name="sensor")
    totals = (4, 4096, 40)
    found = check(p)
    assert ("lab.size", f"2 nodes exceed max_vms {BASE['limits']['max_vms']}") in found
    for key, total in zip(("vcpu_total", "ram_mb_total", "disk_gb_total"), totals, strict=True):
        assert ("lab.size", f"nodes need {key} {total}, above the limit {BASE['limits'][key]}") in found


def test_size_within_the_limits_has_no_findings():
    p = with_node(BASE, name="sensor", vcpu=1, ram_mb=1024, disk_gb=10)
    p["limits"] = {"max_vms": 2, "vcpu_total": 3, "ram_mb_total": 3072, "disk_gb_total": 30}
    assert of("lab.size", p) == []


def test_a_disabled_catalogue_image_is_reported():
    p = copy.deepcopy(BASE)
    p["nodes"][0]["catalogue_id"] = "win2022"
    assert of("lab.catalogue", p, catalogue=(set(), {"win2022"})) == [
        "node gateway uses disabled catalogue image win2022"
    ]


def test_an_unknown_catalogue_image_is_reported():
    p = copy.deepcopy(BASE)
    p["nodes"][0]["catalogue_id"] = "haiku-os"
    assert of("lab.catalogue", p) == ["node gateway uses haiku-os, which is not in the image catalogue"]


def test_catalogue_none_skips_the_image_rule():
    p = copy.deepcopy(BASE)
    p["nodes"][0]["catalogue_id"] = "haiku-os"
    assert of("lab.catalogue", p, catalogue=None) == []


def test_an_allowlist_without_destinations_is_reported():
    p = copy.deepcopy(BASE)
    p["egress"] = {"policy": "allowlist"}
    assert of("lab.egress", p) == ["an allowlist egress policy names no destination"]


def test_an_allowlist_with_a_destination_passes():
    p = copy.deepcopy(BASE)
    p["egress"] = {"policy": "allowlist", "allow": ["10.20.0.5:443"]}
    assert of("lab.egress", p) == []


# -- catalogue_ids -----------------------------------------------------------


def _write_catalogue(tmp_path, rows, name="content/catalogue/vm_iso_catalogue.csv"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["template_id,enabled"]
    lines += [",".join(r) for r in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_catalogue_ids_splits_enabled_from_disabled(tmp_path):
    _write_catalogue(tmp_path, [("ubuntu-lts", "yes"), ("win2022", "false"), ("old-iso", "")])
    assert catalogue_ids(tmp_path) == ({"ubuntu-lts"}, {"win2022", "old-iso"})


def test_catalogue_ids_reads_a_utf8_sig_bom(tmp_path):
    path = tmp_path / "content/catalogue/vm_iso_catalogue.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbftemplate_id,enabled\r\nubuntu-lts,1\r\n")
    assert catalogue_ids(tmp_path) == ({"ubuntu-lts"}, set())


def test_catalogue_ids_skips_blank_template_ids(tmp_path):
    _write_catalogue(tmp_path, [("", "yes"), ("ubuntu-lts", "y")])
    assert catalogue_ids(tmp_path) == ({"ubuntu-lts"}, set())


def test_catalogue_ids_is_none_without_a_catalogue(tmp_path):
    assert catalogue_ids(tmp_path) is None


def test_catalogue_ids_uses_the_first_catalogue_present(tmp_path):
    """The content-pack copy is only read when the platform catalogue is absent."""
    _write_catalogue(tmp_path, [("from-platform", "yes")])
    pack = tmp_path / "truenorth-content-pack/truenorth-content/vm_catalogue.csv"
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text("template_id,enabled\nfrom-pack,yes\n", encoding="utf-8")
    assert catalogue_ids(tmp_path) == ({"from-platform"}, set())


def test_load_reads_yaml(tmp_path):
    from app.course_releases.lab_profile import load, load_schema

    path = tmp_path / "p.yaml"
    path.write_text("id: iot-gateway\n", encoding="utf-8")
    assert load(path) == {"id": "iot-gateway"}
    schema = load_schema()
    assert schema["properties"]["schema_version"] == {"const": "arc2/lab-profile/0.1"}
