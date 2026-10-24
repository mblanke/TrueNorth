#!/usr/bin/env python3
"""List or check learner identity/result columns against the accepted remediation baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "control-plane" / "api"))
os.environ.setdefault("TN_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.db import Base  # noqa: E402
from app.main import app as _app  # noqa: E402,F401  # registers every API model
from app.result_transfer.dlp import schema_findings  # noqa: E402

BASELINE = ROOT / "docs" / "security" / "unclassified-data-baseline.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if a finding is not in the accepted baseline")
    args = parser.parse_args()
    current = [finding.key for finding in schema_findings(Base.metadata)]
    if not args.check:
        print(json.dumps(current, indent=2))
        return 0
    accepted = set(json.loads(BASELINE.read_text())["findings"])
    unexpected = sorted(set(current) - accepted)
    if unexpected:
        print("New Unclassified identity/result persistence requires explicit review:", file=sys.stderr)
        print("\n".join(f"  - {item}" for item in unexpected), file=sys.stderr)
        return 1
    print(f"Unclassified data inventory: {len(current)} known finding(s), no new persistence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
