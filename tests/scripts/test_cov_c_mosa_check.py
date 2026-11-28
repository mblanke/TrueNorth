# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/mosa_check.py: each MOSA metric is found on a synthetic tree, and the ratchet
fails when debt grows, lowers the baseline when it shrinks, and prints locations with
--report. The checker's real scan runs against a throwaway ROOT, so the repo's own
baseline file is never touched."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("cov_c_mosa_check", ROOT / "scripts" / "mosa_check.py")
mosa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mosa)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A repo-shaped tree with exactly one hit for every list metric."""
    api = tmp_path / "control-plane" / "api"
    worker = tmp_path / "control-plane" / "worker" / "worker"
    features = tmp_path / "control-plane" / "web" / "src" / "app" / "features"
    for d in (api / "routers", worker / "provisioners", features):
        d.mkdir(parents=True)

    (worker / "raw_sql.py").write_text('stmt = text("select 1")\n', encoding="utf-8")
    # An adapter dir is exempt from the vendor-SDK scan...
    (worker / "provisioners" / "adapter.py").write_text("import pyVmomi\n", encoding="utf-8")
    # ...but not from the raw-SQL one.
    (worker / "provisioners" / "sql.py").write_text('rows = conn.execute(text("select 2"))\n', encoding="utf-8")
    (api / "sdk_use.py").write_text("from proxmoxer import ProxmoxAPI\n", encoding="utf-8")
    (api / "opensearch_url.py").write_text('url = os.environ["OPENSEARCH_URL"]\n', encoding="utf-8")
    (api / "routers" / "branches.py").write_text('if hypervisor_type == "vsphere":\n    pass\n', encoding="utf-8")
    (api / "cross.py").write_text("from worker import tasks\n", encoding="utf-8")
    (features / "widget.ts").write_text("import { HttpClient } from '@angular/common/http';\n", encoding="utf-8")
    (api / "models.py").write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr(mosa, "ROOT", tmp_path)
    monkeypatch.setattr(mosa, "API", api)
    monkeypatch.setattr(mosa, "WORKER", worker)
    monkeypatch.setattr(mosa, "FEATURES", features)
    monkeypatch.setattr(mosa, "BASELINE_FILE", tmp_path / ".dod-mosa-baseline")
    return tmp_path


def test_collect_finds_each_metric_once_on_the_synthetic_tree(lab):
    metrics = mosa.collect()
    assert metrics["worker_raw_sql"] == [
        "control-plane/worker/worker/raw_sql.py:1",
        "control-plane/worker/worker/provisioners/sql.py:1",
    ]
    assert metrics["vendor_sdk_outside_adapters"] == ["control-plane/api/sdk_use.py:1"]
    assert metrics["vendor_endpoint_outside_adapters"] == ["control-plane/api/opensearch_url.py:1"]
    assert metrics["hardcoded_backend_branches"] == ["control-plane/api/routers/branches.py:1"]
    assert metrics["api_imports_worker"] == ["control-plane/api/cross.py:1"]
    assert metrics["web_features_direct_httpclient"] == ["control-plane/web/src/app/features/widget.ts:1"]
    # The pyVmomi import sits in an adapter dir, so only the line counts remain plain ints.
    assert metrics["lines_api_models_py"] == 1
    assert metrics["lines_api_schemas_py"] == 0


def test_main_records_a_baseline_and_passes_at_it(lab, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mosa_check.py"])
    assert mosa.main() == 0
    base = json.loads((lab / ".dod-mosa-baseline").read_text())
    assert base["worker_raw_sql"] == 2
    assert base["hardcoded_backend_branches"] == 1


def test_main_fails_when_debt_grows_and_names_the_metric(lab, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mosa_check.py"])
    assert mosa.main() == 0
    (lab / "control-plane" / "api" / "cross2.py").write_text("import worker.tasks\n", encoding="utf-8")
    assert mosa.main() == 1
    err = capsys.readouterr().err
    assert "api_imports_worker" in err and "MOSA FAIL" in err


def test_main_lowers_the_baseline_when_debt_shrinks(lab, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mosa_check.py"])
    assert mosa.main() == 0
    (lab / "control-plane" / "worker" / "worker" / "raw_sql.py").unlink()
    assert mosa.main() == 0
    base = json.loads((lab / ".dod-mosa-baseline").read_text())
    assert base["worker_raw_sql"] == 1


def test_report_prints_the_offending_locations(lab, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mosa_check.py", "--report"])
    assert mosa.main() == 0
    out = capsys.readouterr().out
    assert "control-plane/api/routers/branches.py:1" in out
