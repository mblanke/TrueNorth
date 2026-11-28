# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/export_task_contracts.py: render() mirrors the worker's contracts.py into the
API copy plus a JSON schema; --check passes against the committed pair and fails when a
copy goes stale. The stale path runs on a throwaway tree."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "cov_c_export_task_contracts", ROOT / "scripts" / "export_task_contracts.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_TINY_SOURCE = """\
from dataclasses import dataclass


@dataclass
class Task:
    name: str


def json_schema():
    return {"type": "object", "properties": {"name": {"type": "string"}}}
"""


def test_check_passes_against_the_committed_copies(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["export_task_contracts.py", "--check"])
    assert mod.main() == 0


def test_render_mirrors_the_source_and_publishes_its_schema(tmp_path, monkeypatch):
    source = tmp_path / "contracts.py"
    source.write_text(_TINY_SOURCE, encoding="utf-8")
    mirror = tmp_path / "mirror" / "task_contracts.py"
    schema = tmp_path / "schema" / "worker-tasks.schema.json"
    monkeypatch.setattr(mod, "SOURCE", source)
    monkeypatch.setattr(mod, "MIRROR", mirror)
    monkeypatch.setattr(mod, "SCHEMA", schema)

    outputs = mod.render()
    assert set(outputs) == {mirror, schema}
    assert outputs[mirror] == mod.HEADER + _TINY_SOURCE
    assert json.loads(outputs[schema])["properties"]["name"] == {"type": "string"}


def test_check_fails_when_a_copy_is_stale(tmp_path, monkeypatch, capsys):
    source = tmp_path / "contracts.py"
    source.write_text(_TINY_SOURCE, encoding="utf-8")
    mirror = tmp_path / "task_contracts.py"
    schema = tmp_path / "worker-tasks.schema.json"
    monkeypatch.setattr(mod, "SOURCE", source)
    monkeypatch.setattr(mod, "MIRROR", mirror)
    monkeypatch.setattr(mod, "SCHEMA", schema)
    monkeypatch.setattr(mod, "ROOT", tmp_path)  # the stale report prints paths relative to it

    outputs = mod.render()
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    source.write_text(_TINY_SOURCE + "\n# a new task was added\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["export_task_contracts.py", "--check"])
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "TASK CONTRACT DRIFT" in err
