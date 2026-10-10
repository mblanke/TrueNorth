"""Range Designer: the Internet/Cloud stencil is the template's ``greyspace:`` block (ADR 0007,
plan slice 7). Export emits it, import draws it back, and the round trip keeps it."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from app import range_topology as rt

ROOT = Path(__file__).resolve().parents[2]
BLOCK = {"version": 1, "corpus_tier": "t0", "site_packs": ["news", "webmail"], "npc_profile": "office-day",
         "threat_infra": False, "trust_ca": True, "network": "internet"}


def _template() -> dict:
    return {
        "name": "gs", "version": "1.0",
        "network": {"vlans": [{"id": 110, "name": "corp", "cidr": "10.40.110.0/24"},
                              {"id": 190, "name": "internet", "cidr": "100.64.190.0/24"}]},
        "nodes": [
            {"id": "edge", "name": "Edge", "role": "router", "os": "vyos", "vlan": "corp", "ip": "10.40.110.1"},
            {"id": "ws", "name": "WS", "role": "workstation", "os": "windows-11", "vlan": "corp", "ip": "10.40.110.10"},
        ],
        "greyspace": BLOCK,
    }


def test_import_draws_the_block_as_a_cloud_and_export_gives_it_back():
    diagram = rt.template_to_diagram(_template())
    clouds = [c for c in diagram["cells"] if c["nodeType"] == "cloud"]
    assert len(clouds) == 1 and clouds[0]["id"] == "greyspace"
    assert clouds[0]["nodeData"]["greyspace"] == BLOCK
    out = rt.diagram_to_template(diagram, "gs")
    assert out["template"]["greyspace"] == BLOCK
    assert [n["id"] for n in out["template"]["nodes"]] == ["edge", "ws"]  # the cloud is not a VM


def test_a_dropped_stencil_emits_the_defaults_and_validates_against_the_template_schema():
    diagram = rt.template_to_diagram({**_template(), "greyspace": {}})
    out = rt.diagram_to_template(diagram, "gs")
    assert out["template"]["greyspace"] == rt.GREYSPACE_DEFAULTS
    schema = json.loads((ROOT / "scenario-engine/schemas/template.schema.json").read_text())
    jsonschema.validate(out["template"]["greyspace"], schema["properties"]["greyspace"])


def test_a_cloud_without_greyspace_or_switched_off_emits_nothing():
    diagram = rt.template_to_diagram({k: v for k, v in _template().items() if k != "greyspace"})
    plain = rt._cell_node("c1", "Internet", "cloud", "", "", 900, 40)
    off = rt.greyspace_cell({**BLOCK, "enabled": False}, 900, 200)
    off["nodeData"]["greyspace"]["enabled"] = False
    diagram["cells"] += [plain, off]
    assert "greyspace" not in rt.diagram_to_template(diagram, "gs")["template"]


def test_two_greyspace_clouds_use_the_first_and_say_so():
    diagram = rt.template_to_diagram(_template())
    second = rt.greyspace_cell({**BLOCK, "corpus_tier": "t2"}, 900, 300)
    second["id"] = "greyspace-2"
    diagram["cells"].append(second)
    out = rt.diagram_to_template(diagram, "gs")
    assert out["template"]["greyspace"]["corpus_tier"] == "t0"
    assert any("2 Internet/Cloud stencils carry Greyspace" in w for w in out["warnings"])


def test_unknown_settings_are_dropped_with_a_warning():
    diagram = rt.template_to_diagram(_template())
    cloud = next(c for c in diagram["cells"] if c["nodeType"] == "cloud")
    cloud["nodeData"]["greyspace"]["colour"] = "grey"
    out = rt.diagram_to_template(diagram, "gs")
    assert "colour" not in out["template"]["greyspace"]
    assert any("colour" in w for w in out["warnings"])


def test_the_web_mirror_uses_the_same_defaults():
    ts = (ROOT / "control-plane/web/src/app/features/range-designer/greyspace-stencil.ts").read_text()
    for key, value in rt.GREYSPACE_DEFAULTS.items():
        literal = json.dumps(value).replace('"', "'")  # true / false / null / 1 / 't0'
        assert f"{key}: {literal}" in ts, key
