#!/usr/bin/env python3
"""Print the test files one CI shard runs, one path per line.

The `test-python` suite is one serial pytest run that takes over half an hour, so
.github/workflows/ci.yml splits it across `test-python-shard` jobs and a final job named
`test-python` (the name branch protection requires) that checks every shard passed.

    python scripts/ci_shard.py 2 5     # shard 2 of 5 (1-based)

Files are dealt out round-robin from the sorted list, so neighbouring files (which tend
to share a slow fixture) land on different shards. A shard runs whole files, never part
of one, so module-scoped fixtures are not repeated. tests/contracts/test_ci_shard.py
proves the shards are disjoint and together cover every file
`pytest tests/ --ignore=tests/integration` would collect. scripts/dod.sh still runs the
whole suite in one process.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_files(root: Path = ROOT) -> list[str]:
    """Every file pytest collects under tests/ (pytest's default python_files), minus integration."""
    tests = root / "tests"
    found = {
        p.relative_to(root).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for p in tests.rglob(pattern)
        if "integration" not in p.relative_to(tests).parts
    }
    return sorted(found)


def shard(files: list[str], index: int, total: int) -> list[str]:
    if not 1 <= index <= total:
        raise ValueError(f"shard {index} is not in 1..{total}")
    return files[index - 1 :: total]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    index, total = int(argv[1]), int(argv[2])
    chosen = shard(test_files(), index, total)
    if not chosen:
        print(f"shard {index}/{total} has no files", file=sys.stderr)
        return 1
    print("\n".join(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
