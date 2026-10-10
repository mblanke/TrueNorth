"""A shipped range template's declared ``vm_count`` equals the VMs it builds.

ADR 0004 decisions log (2026-10-05): four templates declared a vm_count that differed from
what they build (soc-training 25 vs 23, cloud-security 20 vs 16, red-team 30 vs 28,
large-enterprise 54 vs 50). Nothing reads the field to size a booking (demand comes from
the nodes, app/range_topology.template_demand), but people read it, so it must not lie.
This renders every shipped template with the worker's real code and compares, including
red-vs-blue's per-hypervisor-node ``deployment.node_assignment.*.vm_count``.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app.range_topology import template_demand  # noqa: E402
from worker.render import render_topology  # noqa: E402

TEMPLATES = sorted((ROOT / "content/ranges").glob("*/template.yaml"))
RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"


def _built(path: Path) -> tuple[dict, list[dict]]:
    template = yaml.safe_load(path.read_text())
    return template, render_topology(template, RANGE_ID, lambda alias: alias)["vm_definitions"]


def test_there_are_templates_to_check():
    assert len(TEMPLATES) >= 8


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.parent.name)
def test_declared_vm_count_is_what_the_template_builds(path):
    template, vms = _built(path)
    if "vm_count" in template:
        assert template["vm_count"] == len(vms), f"{path.parent.name}: declares {template['vm_count']}, builds {len(vms)}"
    assert template_demand(path.read_text()).vm_count == len(vms)  # what the scheduler books


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.parent.name)
def test_per_node_vm_counts_match_the_nodes_placed_there(path):
    template, vms = _built(path)
    assignment = (template.get("deployment") or {}).get("node_assignment") or {}
    if not assignment:
        pytest.skip("no deployment.node_assignment")
    nodes = {n.get("id"): n for n in template.get("nodes") or []}
    placed = collections.Counter(str(nodes.get(v.get("node_id"), {}).get("node") or "") for v in vms)
    assert {k: v.get("vm_count") for k, v in assignment.items()} == {k: placed[k] for k in assignment}
    assert sum(v.get("vm_count", 0) for v in assignment.values()) == len(vms)
