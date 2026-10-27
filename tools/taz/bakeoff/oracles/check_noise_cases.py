"""noise-tamper-flake oracle: the forge test still checks every tamper case.

Passing the pinned-clock run is not enough on its own: deleting the flaky ``kind`` case,
or skipping it, also "passes". This collects the test and requires at least five cases,
one per field the original test tampered with, and no skip or xfail marker on it.

Usage: python check_noise_cases.py <python-with-test-deps>   (run from the repo root)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

TEST = "tests/api/test_noise.py"
NAME = "test_a_stolen_token_cannot_forge_ground_truth"
FIELDS = ("target", "persona", "kind", "at", "sig")


def main(python: str) -> int:
    out = subprocess.run(
        [python, "-P", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", f"{TEST}", "-k", NAME],
        capture_output=True, text=True,
    ).stdout
    cases = [ln for ln in out.splitlines() if NAME in ln]
    print(f"{len(cases)} collected case(s) of {NAME}")
    problems = []
    if len(cases) < len(FIELDS):
        problems.append(f"expected at least {len(FIELDS)} tamper cases, collected {len(cases)}")
    source = Path(TEST).read_text(encoding="utf-8")
    start = source.find(f"def {NAME}")
    head = source[max(0, start - 1500):start] if start >= 0 else ""
    # Decorators directly above the test (the parametrize block sits in this window).
    if re.search(r"@pytest\.mark\.(skip|skipif|xfail)\b", head.split("\n\n")[-1] if head else ""):
        problems.append("the forge test carries a skip/xfail marker")
    body = source[start:start + 3000] if start >= 0 else ""
    if re.search(r"pytest\.(skip|xfail)\(", body):
        problems.append("the forge test skips or xfails itself")
    missing = [f for f in FIELDS if f'"{f}"' not in head + body and f"'{f}'" not in head + body]
    if missing:
        problems.append(f"no tamper case names these fields any more: {missing}")
    for p in problems:
        print("FAIL:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else sys.executable))
