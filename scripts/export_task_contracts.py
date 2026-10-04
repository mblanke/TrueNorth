#!/usr/bin/env python3
"""Publish the API -> worker task contract.

Source: control-plane/worker/worker/contracts.py. Writes
  control-plane/api/app/task_contracts.py        (the API image's copy)
  docs/interfaces/worker-tasks.schema.json       (the published interface)

    python scripts/export_task_contracts.py          # regenerate
    python scripts/export_task_contracts.py --check  # fail on drift (dod.sh, CI)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "control-plane/worker/worker/contracts.py"
MIRROR = ROOT / "control-plane/api/app/task_contracts.py"
SCHEMA = ROOT / "docs/interfaces/worker-tasks.schema.json"

HEADER = (
    "# GENERATED from control-plane/worker/worker/contracts.py by scripts/export_task_contracts.py.\n"
    "# Do not edit; edit the source and rerun the script.\n"
)


def _load_source():
    spec = importlib.util.spec_from_file_location("_task_contracts_src", SOURCE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


def render() -> dict[Path, str]:
    schema = json.dumps(_load_source().json_schema(), indent=2, sort_keys=True) + "\n"
    return {MIRROR: HEADER + SOURCE.read_text(), SCHEMA: schema}


def main() -> int:
    outputs = render()
    if "--check" not in sys.argv:
        for path, text in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"wrote {path.relative_to(ROOT)}")
        return 0

    stale = [p for p, text in outputs.items() if not p.exists() or p.read_text() != text]
    for p in stale:
        print(f"  stale: {p.relative_to(ROOT)}")
    if stale:
        print(
            "TASK CONTRACT DRIFT: run `python scripts/export_task_contracts.py` and commit the result.",
            file=sys.stderr,
        )
        return 1
    print("  task contract copies match control-plane/worker/worker/contracts.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
