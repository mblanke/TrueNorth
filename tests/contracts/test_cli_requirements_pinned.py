"""tools/cli/requirements.txt pins exact versions, the same ones the services pin.

It was the one unpinned requirements file (``typer[all]>=0.9.0`` ...): what an operator got
depended on the day they installed it, and nothing kept httpx/pyyaml/jsonschema in step
with control-plane/api, the worker and scenario-engine, so the CLI could not share their
environment. Also: every third-party module forge.py imports is listed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "tools/cli"
SERVICES = [
    ROOT / "control-plane/api/requirements.txt",
    ROOT / "control-plane/worker/requirements.txt",
    ROOT / "scenario-engine/requirements.txt",
]
# Import name -> distribution name, where they differ.
DIST = {"yaml": "pyyaml"}
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][^\s;#]*)\s*(?:#.*)?$")


def _requirements(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")  # tolerate a byte-order mark
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _pins(path: Path) -> dict[str, str]:
    out = {}
    for line in _requirements(path):
        m = PIN.match(line.split(";")[0].strip())
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def test_every_cli_requirement_is_an_exact_pin():
    lines = _requirements(CLI / "requirements.txt")
    assert lines
    loose = [ln for ln in lines if not PIN.match(ln)]
    assert not loose, f"not name==version: {loose}"


def test_shared_packages_match_the_service_pins():
    cli = _pins(CLI / "requirements.txt")
    for path in SERVICES:
        for name, version in _pins(path).items():
            if name in cli:
                assert cli[name] == version, f"{name}: CLI {cli[name]}, {path.relative_to(ROOT)} {version}"


def test_every_third_party_import_of_the_cli_is_listed():
    listed = set(_pins(CLI / "requirements.txt"))
    imported: set[str] = set()
    for path in CLI.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    third_party = {m for m in imported if m not in sys.stdlib_module_names and m != "__future__"}
    missing = {DIST.get(m, m) for m in third_party} - listed
    assert not missing, f"forge.py imports packages its requirements do not pin: {sorted(missing)}"
