"""The API decides which machines are Windows exactly as the worker does (ADR 0012).

A typed-in password for a Windows machine must meet Windows complexity
(app/range_credentials/accounts.py), because Sysprep sets it; the worker decides what is
Windows from the rendered template (worker/render.py, provisioners/vsphere_infra.os_family).
The API may not import the worker, so it mirrors that decision: if they drift, a weak
password reaches Sysprep (and fails the build) or a Linux password is refused for nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app.range_credentials import accounts  # noqa: E402
from worker.provisioners.vsphere_infra import os_family  # noqa: E402
from worker.render import render_topology  # noqa: E402

RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"

CASES = {
    "os": {"nodes": [{"id": "a", "os": "windows-server-2022"}, {"id": "b", "os": "ubuntu-2404"}]},
    "os_template": {"nodes": [{"id": "a", "os_template": "win-2019"}, {"id": "b", "os_template": "kali-2024"}]},
    "type": {"nodes": [{"id": "a", "type": "windows-11"}, {"id": "b", "type": "workstation"}]},
    "default": {"nodes": [{"id": "a"}]},
    "assets": {"assets": [{"role": "dc", "os": "windows-server-2022", "type": "vm", "count": 1},
                          {"role": "web", "type": "vm", "count": 1}]},
    "nodes_win_over_assets": {"nodes": [{"id": "a", "os": "windows-11"}],
                              "assets": [{"role": "kali", "os": "kali-2024", "type": "vm", "count": 1}]},
}
CASES.update({
    f"content:{p.parent.name}": yaml.safe_load(p.read_text(encoding="utf-8-sig"))
    for p in sorted(ROOT.glob("content/ranges/*/template.yaml"))
})


@pytest.mark.parametrize("name", sorted(CASES))
def test_windows_machines_match(name):
    doc = CASES[name]
    rendered = render_topology(doc, RANGE_ID, lambda alias: None)["vm_definitions"]
    worker = {str(vm["node_id"]): os_family(vm) == "windows" for vm in rendered}
    api = {m: accounts.is_windows(os) for m, os in accounts.machine_os(doc).items()}
    assert api == worker, name
