# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/unclassified_data_inventory.py: the inventory lists the current learner
identity/result persistence findings, --check passes against the accepted baseline and
fails when the schema grows a finding the baseline does not accept. Only --check and the
listing mode run; the drift path swaps in a throwaway empty baseline."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "cov_c_unclassified_data_inventory", ROOT / "scripts" / "unclassified_data_inventory.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_listing_prints_the_current_findings_as_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["unclassified_data_inventory.py"])
    assert mod.main() == 0
    findings = json.loads(capsys.readouterr().out)
    assert isinstance(findings, list) and findings
    assert all(isinstance(f, str) for f in findings)
    assert all(f.startswith(("identity:", "result:")) for f in findings)


def test_check_passes_against_the_accepted_baseline(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["unclassified_data_inventory.py", "--check"])
    assert mod.main() == 0


def test_check_fails_on_persistence_outside_the_baseline(tmp_path, monkeypatch, capsys):
    (tmp_path / "baseline.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(sys, "argv", ["unclassified_data_inventory.py", "--check"])
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "requires explicit review" in err
