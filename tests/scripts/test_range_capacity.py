"""scripts/range-capacity.py: per-range sums and the lab fit check."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("range_capacity", ROOT / "scripts" / "range-capacity.py")
rc = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("range_capacity", rc)  # dataclasses look their module up
_spec.loader.exec_module(rc)


def test_sums_specs_with_count_legacy_keys_and_renderer_defaults():
    tpl = {"id": "t", "nodes": [
        {"id": "a", "specs": {"cores": 4, "memory_mb": 8192, "disk_gb": 100}},
        {"id": "b", "count": 3, "specs": {"cores": 1, "memory_mb": 1024, "disk_gb": 20}},
        {"id": "c", "vcpu": 2, "ram_mb": 2048, "disk_gb": 30},  # legacy designer export
        {"id": "d"},  # no specs: worker/render.py defaults 2 / 4096 / 60
    ]}
    t = rc.range_totals(tpl)
    assert (t.vms, t.vcpu, t.disk_gb) == (6, 4 + 3 + 2 + 2, 100 + 60 + 30 + 60)
    assert t.ram_gb == (8192 + 3072 + 2048 + 4096) / 1024


def test_assets_schema_counts_vms_only():
    tpl = {"name": "s", "assets": [
        {"role": "jump", "type": "vm", "count": 1},
        {"role": "user", "type": "vm", "count": 3, "specs": {"cores": 1}},
        {"role": "bucket", "type": "s3"},
    ]}
    t = rc.range_totals(tpl)
    assert (t.vms, t.vcpu, t.ram_gb, t.disk_gb) == (4, 2 + 3, 16.0, 240)


def test_lab_defaults_match_the_lab():
    lab = rc.Lab()
    # esx02-04: 3 x 16 threads x 4 vCPU/thread = 192, minus 34 for the management VMs.
    assert lab.vcpu_available == 192 - 34
    assert lab.ram_available_gb == 3 * 512 - 128


def test_fit_is_the_tighter_of_cpu_and_ram():
    lab = rc.Lab(hosts=1, threads_per_host=4, vcpu_per_thread=4, mgmt_vcpu=0, ram_per_host_gb=64, mgmt_ram_gb=0)
    cpu_bound = rc.assess(rc.RangeTotals("c", vms=1, vcpu=8, ram_gb=4, disk_gb=10), lab)
    ram_bound = rc.assess(rc.RangeTotals("r", vms=1, vcpu=1, ram_gb=40, disk_gb=10), lab)
    too_big = rc.assess(rc.RangeTotals("x", vms=1, vcpu=17, ram_gb=1, disk_gb=10), lab)
    assert (cpu_bound.max_concurrent, ram_bound.max_concurrent, too_big.max_concurrent) == (2, 1, 0)
    assert cpu_bound.fits and not too_big.fits


# Right-sizing targets for the 4-host lab (docs/vm-build-sheet.md section 4).
TARGETS = {"soc-training": 48, "red-team": 48, "red-vs-blue": 64, "medium-enterprise": 16,
           "cloud-security": 40, "large-enterprise": 120}


def test_every_shipped_range_fits_the_lab_and_meets_its_target():
    rows = {r.range: r for r in rc.report(rc.load(), rc.Lab())}
    assert set(TARGETS) <= set(rows)
    for name, row in rows.items():
        assert row.vms > 0 and row.fits, row
        if name in TARGETS:
            assert row.vcpu <= TARGETS[name], row


@pytest.mark.parametrize("argv,code", [(["--check"], 0), (["--check", "--hosts", "1", "--mgmt-vcpu", "60"], 1)])
def test_main_check_exit_code(argv, code, capsys):
    assert rc.main(argv) == code
    out = capsys.readouterr().out
    assert "| soc-training |" in out and "Concurrent" in out


def test_main_json(capsys):
    assert rc.main(["--json", str(ROOT / "content/ranges/medium-enterprise/template.yaml")]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["lab"]["vcpu_available"] == 158
    (row,) = data["ranges"]
    assert row["range"] == "medium-enterprise" and row["vms"] == 8 and row["fits"] is True
