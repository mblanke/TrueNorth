#!/usr/bin/env python3
"""Export the control-plane API contract to docs/interfaces/openapi.json.

The committed file is the published key interface (MOSA pillar 3). scripts/dod.sh runs
this with --check and fails when the code's contract differs from the committed one, so
every API shape change is a reviewed diff. See docs/adr/0002-interface-versioning.md.

    python scripts/export_openapi.py          # regenerate
    python scripts/export_openapi.py --check  # fail on drift
"""

from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs/interfaces/openapi.json"

# Same environment the test suite builds the app in (tests/conftest.py).
os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
sys.path.insert(0, str(ROOT / "control-plane/api"))


def render() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    spec = render()
    if "--check" not in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(spec)
        print(f"wrote {OUT.relative_to(ROOT)}")
        return 0

    committed = OUT.read_text() if OUT.exists() else ""
    if committed == spec:
        print(f"  {OUT.relative_to(ROOT)} matches the code")
        return 0
    diff = difflib.unified_diff(
        committed.splitlines(), spec.splitlines(), "committed", "code", lineterm="", n=2
    )
    for i, line in enumerate(diff):
        if i >= 80:
            print("  ... (diff truncated)")
            break
        print(line)
    print(
        "OPENAPI DRIFT: the API contract changed. If intended, run "
        "`python scripts/export_openapi.py` and commit docs/interfaces/openapi.json.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
