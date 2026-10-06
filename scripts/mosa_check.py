#!/usr/bin/env python3
"""MOSA ratchet: modularity debt may shrink but never grow.

Same contract as the ruff ratchet in scripts/dod.sh (.dod-ruff-baseline): each metric
below counts one way a module reaches past its interface. A count above the recorded
baseline fails the gate; a count below it lowers the baseline, so cleanup sticks.

Why each metric exists is in docs/adr/0003-mosa-ratchet.md. To lower a baseline, remove
the debt; never raise one by hand without an ADR saying why.

    python scripts/mosa_check.py            # check and ratchet down
    python scripts/mosa_check.py --report   # print counts and offending locations
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".dod-mosa-baseline"

API = ROOT / "control-plane/api/app"
WORKER = ROOT / "control-plane/worker/worker"
FEATURES = ROOT / "control-plane/web/src/app/features"

# Vendor SDKs belong behind an adapter. These directories ARE the adapters.
VENDOR_SDK = re.compile(r"^\s*(?:from|import)\s+(proxmoxer|pyVmomi|pyvmomi|opensearchpy)\b", re.M)
ADAPTER_DIRS = (
    "control-plane/worker/worker/provisioners/",
    "control-plane/api/app/search_backends/",
    "control-plane/api/app/hypervisor_backends/",
    "control-plane/api/app/vector_backends/",
    "control-plane/api/app/console_backends/",  # browser consoles (vSphere WebMKS)
    "control-plane/api/app/moodle_backends/",
    "telemetry-pipeline/",
    "scenario-engine/scenario_engine/event_stores/",
)
SDK_SCAN_DIRS = ("control-plane", "scenario-engine", "ai-orchestrator", "telemetry-pipeline", "tools")


def _py_files(base: Path):
    return (p for p in base.rglob("*.py") if ".venv" not in p.parts and "node_modules" not in p.parts)


def _hits(files, pattern: re.Pattern[str]) -> list[str]:
    out = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in pattern.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            out.append(f"{f.relative_to(ROOT)}:{line}")
    return out


def _lines(p: Path) -> int:
    return sum(1 for _ in p.open(encoding="utf-8")) if p.exists() else 0


def collect() -> dict[str, list[str] | int]:
    """Metric name -> list of offending locations (count = len) or a plain int."""
    worker_sql = _hits(_py_files(WORKER), re.compile(r"\btext\("))

    sdk_files = [
        f for d in SDK_SCAN_DIRS for f in _py_files(ROOT / d) if not str(f.relative_to(ROOT)).startswith(ADAPTER_DIRS)
    ]
    vendor_sdk = _hits(sdk_files, VENDOR_SDK)
    # Same rule for vendor endpoints reached over plain HTTP: reading the store's URL
    # outside an adapter means building requests against it there.
    vendor_url = _hits(sdk_files, re.compile(r"getenv\(\s*['\"]OPENSEARCH_URL|environ\[\s*['\"]OPENSEARCH_URL"))

    branches = _hits(
        _py_files(API / "routers"),
        re.compile(r"\b(?:hypervisor_type|platform_type)\s*==\s*['\"]"),
    )

    # The API and worker are separate images. An import across that line passes under
    # pytest (both on sys.path) and fails in production. Talk via celery_client.dispatch.
    cross_service = _hits(_py_files(API), re.compile(r"^\s*(?:from|import)\s+worker\b", re.M))

    ts = [p for p in FEATURES.rglob("*.ts") if not p.name.endswith(".spec.ts")] if FEATURES.exists() else []
    http = _hits(ts, re.compile(r"\bHttpClient\b(?=[^\n]*from\s+'@angular/common/http')"))

    return {
        "worker_raw_sql": worker_sql,
        "vendor_sdk_outside_adapters": vendor_sdk,
        "vendor_endpoint_outside_adapters": vendor_url,
        "hardcoded_backend_branches": branches,
        "api_imports_worker": cross_service,
        "web_features_direct_httpclient": http,
        "lines_api_models_py": _lines(API / "models.py"),
        "lines_api_schemas_py": _lines(API / "schemas.py"),
        "lines_worker_tasks_py": _lines(WORKER / "tasks.py"),
    }


def main() -> int:
    report = "--report" in sys.argv
    metrics = collect()
    counts = {k: (len(v) if isinstance(v, list) else v) for k, v in metrics.items()}
    try:
        base = json.loads(BASELINE_FILE.read_text())
    except (OSError, ValueError):
        base = dict(counts)

    worse = []
    for name, cur in counts.items():
        b = base.get(name, cur)
        mark = "  " if cur == b else ("v " if cur < b else "! ")
        print(f"  {mark}{name}: {cur} (baseline {b})")
        if cur > b:
            worse.append(name)
        if report or cur > b:
            v = metrics[name]
            if isinstance(v, list):
                for loc in v:
                    print(f"        {loc}")

    if worse:
        print(
            f"MOSA FAIL: {', '.join(worse)} rose above baseline. Route the new code through "
            "the adapter/contract instead (docs/adr/0001-adapter-registry.md).",
            file=sys.stderr,
        )
        return 1

    new_base = {k: min(counts[k], base.get(k, counts[k])) for k in counts}
    BASELINE_FILE.write_text(json.dumps(new_base, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
