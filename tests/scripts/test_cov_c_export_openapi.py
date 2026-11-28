# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/export_openapi.py: render() builds the live app's OpenAPI document, --check
passes against the committed docs/interfaces/openapi.json and fails on drift. Only
--check mode runs, so the committed contract is never rewritten."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("cov_c_export_openapi", ROOT / "scripts" / "export_openapi.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_render_is_the_live_app_contract():
    spec = mod.render()
    doc = json.loads(spec)
    assert doc["openapi"].startswith("3")
    assert doc["paths"], "the app must serve routes"
    assert spec.endswith("\n")


def test_check_passes_against_the_committed_contract(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])
    assert mod.main() == 0


def test_check_fails_when_the_committed_file_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "OUT", tmp_path / "openapi.json")
    monkeypatch.setattr(sys, "argv", ["export_openapi.py", "--check"])
    assert mod.main() == 1
    assert "OPENAPI DRIFT" in capsys.readouterr().err
