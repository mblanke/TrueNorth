#!/usr/bin/env python3
"""Sum vCPU / RAM / disk per range template and check them against the vSphere lab.

Reads every ``content/ranges/*/template.yaml`` (or the paths given) and prints one row per
range with what a single instance asks for, how many instances fit on the lab at once, and
whether at least one does.

Counting follows the worker renderer (``control-plane/worker/worker/render.py``) so the
numbers are what provisioning would actually request:

* ``nodes:`` templates: ``specs.cores`` / ``specs.memory_mb`` / ``specs.disk_gb``, with the
  legacy designer keys ``vcpu`` / ``ram_mb`` / ``disk_gb`` as fallback; ``count`` multiplies.
* ``assets:`` templates (small-enterprise): one VM per ``count`` with the asset's ``specs``
  if it has any, else the renderer defaults.
* Missing specs fall back to the renderer defaults: 2 vCPU, 4096 MB, 60 GB.

Lab model (defaults; all overridable): esx01 holds vCenter, so ranges get esx02-04, each a
Xeon D-2146NT with 16 threads and 512 GB. The provisioner admits VMs while powered-on vCPUs
<= threads x ``VSPHERE_MAX_VCPU_PER_THREAD`` (default 4). The management VMs (TN-MGMT01,
TN-DC01, TN-DEPOT01, TN-BUILD01, TN-KMS01, ...) take about 34 vCPU and 128 GB of that.
Disk is reported against the range hosts' local datastores; ranges are thin-provisioned,
so provisioned disk over the datastore total is a warning, not a failure.

Usage:
    .venv/bin/python scripts/range-capacity.py [--json] [--check] [template.yaml ...]
        [--hosts 3] [--threads-per-host 16] [--vcpu-per-thread 4]
        [--ram-per-host-gb 512] [--mgmt-vcpu 34] [--mgmt-ram-gb 128] [--datastore-gb 4980]

``--check`` exits 1 if any range does not fit at least once.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RANGES_DIR = ROOT / "content" / "ranges"

# Same defaults as worker/render.py, so a node without specs counts as it would deploy.
DEFAULT_CORES = 2
DEFAULT_MEMORY_MB = 4096
DEFAULT_DISK_GB = 60


@dataclass(frozen=True)
class Lab:
    hosts: int = 3
    threads_per_host: int = 16
    vcpu_per_thread: float = 4
    ram_per_host_gb: int = 512
    mgmt_vcpu: int = 34
    mgmt_ram_gb: int = 128
    datastore_gb: int = 4980  # 3 x 1.66 TB local VMFS on esx02-04

    @property
    def vcpu_available(self) -> int:
        return int(self.hosts * self.threads_per_host * self.vcpu_per_thread) - self.mgmt_vcpu

    @property
    def ram_available_gb(self) -> int:
        return self.hosts * self.ram_per_host_gb - self.mgmt_ram_gb


@dataclass
class RangeTotals:
    range: str
    vms: int
    vcpu: int
    ram_gb: float
    disk_gb: int
    max_concurrent: int = 0
    fits: bool = False
    disk_over_datastore: bool = False


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _specs(item: dict) -> tuple[int, int, int]:
    specs = item.get("specs") if isinstance(item.get("specs"), dict) else {}
    cores = specs.get("cores", item.get("vcpu"))
    mem = specs.get("memory_mb", item.get("ram_mb"))
    disk = specs.get("disk_gb", item.get("disk_gb"))
    return (
        _int(cores, DEFAULT_CORES) if cores not in (None, "") else DEFAULT_CORES,
        _int(mem, DEFAULT_MEMORY_MB) if mem not in (None, "") else DEFAULT_MEMORY_MB,
        _int(disk, DEFAULT_DISK_GB) if disk not in (None, "") else DEFAULT_DISK_GB,
    )


def vm_items(template: dict) -> list[dict]:
    """The VM-bearing entries of a template: ``nodes`` if present, else ``assets`` VMs."""
    if template.get("nodes"):
        return [n for n in template["nodes"] if isinstance(n, dict)]
    return [a for a in template.get("assets") or []
            if isinstance(a, dict) and str(a.get("type", "vm")) == "vm"]


def range_totals(template: dict, name: str | None = None) -> RangeTotals:
    vms = vcpu = mem_mb = disk = 0
    for item in vm_items(template):
        count = max(_int(item.get("count", 1) or 1, 1), 0)
        cores, mem, gb = _specs(item)
        vms += count
        vcpu += cores * count
        mem_mb += mem * count
        disk += gb * count
    return RangeTotals(range=name or str(template.get("id") or template.get("name") or "?"),
                       vms=vms, vcpu=vcpu, ram_gb=round(mem_mb / 1024, 1), disk_gb=disk)


def assess(totals: RangeTotals, lab: Lab) -> RangeTotals:
    if totals.vms == 0:
        totals.max_concurrent, totals.fits = 0, False
        return totals
    by_cpu = lab.vcpu_available // totals.vcpu if totals.vcpu else 10**6
    by_ram = int(lab.ram_available_gb // totals.ram_gb) if totals.ram_gb else 10**6
    totals.max_concurrent = max(min(by_cpu, by_ram), 0)
    totals.fits = totals.max_concurrent >= 1
    totals.disk_over_datastore = totals.disk_gb > lab.datastore_gb
    return totals


def load(paths: list[Path] | None = None) -> list[tuple[str, dict]]:
    paths = paths or sorted(RANGES_DIR.glob("*/template.yaml"))
    out = []
    for p in paths:
        tpl = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
        name = p.parent.name if p.name == "template.yaml" else p.stem
        out.append((name, tpl))
    return out


def report(templates: list[tuple[str, dict]], lab: Lab) -> list[RangeTotals]:
    return [assess(range_totals(tpl, name), lab) for name, tpl in templates]


def render_table(rows: list[RangeTotals], lab: Lab) -> str:
    head = (f"Lab: {lab.hosts} hosts x {lab.threads_per_host} threads x {lab.vcpu_per_thread:g} vCPU/thread"
            f" - {lab.mgmt_vcpu} mgmt = {lab.vcpu_available} vCPU; "
            f"{lab.hosts} x {lab.ram_per_host_gb} GB - {lab.mgmt_ram_gb} mgmt = {lab.ram_available_gb} GB RAM; "
            f"{lab.datastore_gb} GB datastore (thin)")
    cols = ("Range", "VMs", "vCPU", "RAM GB", "Disk GB", "Concurrent", "Fits")
    lines = [head, "", "| " + " | ".join(cols) + " |", "|---|" + "---:|" * 5 + "---|"]
    for r in rows:
        fits = "yes" if r.fits else "NO"
        if r.disk_over_datastore:
            fits += " (disk > datastore; thin only)"
        lines.append(f"| {r.range} | {r.vms} | {r.vcpu} | {r.ram_gb:g} | {r.disk_gb} | {r.max_concurrent} | {fits} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("templates", nargs="*", type=Path, help="template.yaml files (default: content/ranges/*)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--check", action="store_true", help="exit 1 if any range does not fit once")
    defaults = Lab()
    for field, typ in (("hosts", int), ("threads_per_host", int), ("vcpu_per_thread", float),
                       ("ram_per_host_gb", int), ("mgmt_vcpu", int), ("mgmt_ram_gb", int),
                       ("datastore_gb", int)):
        ap.add_argument("--" + field.replace("_", "-"), type=typ, default=getattr(defaults, field))
    args = ap.parse_args(argv)
    lab = Lab(hosts=args.hosts, threads_per_host=args.threads_per_host, vcpu_per_thread=args.vcpu_per_thread,
              ram_per_host_gb=args.ram_per_host_gb, mgmt_vcpu=args.mgmt_vcpu, mgmt_ram_gb=args.mgmt_ram_gb,
              datastore_gb=args.datastore_gb)
    rows = report(load(args.templates or None), lab)
    if args.json:
        print(json.dumps({"lab": {**asdict(lab), "vcpu_available": lab.vcpu_available,
                                  "ram_available_gb": lab.ram_available_gb},
                          "ranges": [asdict(r) for r in rows]}, indent=2))
    else:
        print(render_table(rows, lab))
    return 1 if args.check and not all(r.fits for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
