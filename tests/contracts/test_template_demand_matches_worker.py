"""The scheduler's booking size must equal what the worker builds.

`range_topology.template_demand` (API) re-implements worker/render.py's node expansion
because the API may not import the worker. If they drift, bookings reserve the wrong
amount of capacity. This renders every shipped template with the real worker code and
compares.
"""

from __future__ import annotations

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

LEGACY_DESIGNER = """
nodes:
  - id: fw
    type: firewall
    os_template: pfsense
    vcpu: 2
    ram_mb: 2048
  - id: sw
    type: switch
  - id: kali
    os: kali
    count: 2
    specs: { cores: 4 }
"""


@pytest.mark.parametrize(
    "source", [*TEMPLATES, LEGACY_DESIGNER], ids=lambda s: getattr(s, "parent", Path("legacy")).name
)
def test_demand_equals_rendered_vms(source):
    text = source.read_text() if isinstance(source, Path) else source
    vms = render_topology(yaml.safe_load(text), "r1", lambda alias: alias)["vm_definitions"]
    d = template_demand(text)
    assert d is not None
    assert (d.vm_count, d.vcpu, d.ram_mb, d.disk_gb) == (
        len(vms),
        sum(v["cores"] for v in vms),
        sum(v["memory_mb"] for v in vms),
        sum(v["disk_gb"] for v in vms),
    )


def test_there_are_templates_to_check():
    assert TEMPLATES
