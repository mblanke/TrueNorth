# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/export_content_staleness.py: render() prepends the GENERATED header to the
API's staleness rules; main() writes the worker copy, --check passes when it matches and
fails on drift. The worker copy is always a throwaway file under tmp_path."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "cov_c_export_content_staleness", ROOT / "scripts" / "export_content_staleness.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_RULES = "RULES = {'content_age_days': 90}\n"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    source = tmp_path / "staleness.py"
    source.write_text(_RULES, encoding="utf-8")
    out = tmp_path / "worker" / "staleness.py"
    out.parent.mkdir(parents=True, exist_ok=True)  # --check reads OUT even before main writes it
    monkeypatch.setattr(mod, "SOURCE", source)
    monkeypatch.setattr(mod, "OUT", out)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return out


def test_render_prepends_the_generated_header(tree):
    assert mod.render() == mod.HEADER + _RULES
    assert "Do not edit" in mod.HEADER


def test_main_writes_the_worker_copy_and_check_then_passes(tree):
    assert mod.main([]) == 0
    assert tree.read_text() == mod.render()
    assert mod.main(["--check"]) == 0


def test_check_fails_when_the_copy_is_missing_or_stale(tree):
    assert mod.main(["--check"]) == 1  # never written
    tree.write_text("# someone edited the generated copy\n")
    assert mod.main(["--check"]) == 1
