#!/usr/bin/env python3
"""Print the test files one CI shard runs, one path per line.

The `test-python` suite is one serial pytest run that takes over half an hour, so
.github/workflows/ci.yml splits it across `test-python-shard` jobs and a final job named
`test-python` (the name branch protection requires) that checks every shard passed.

    python scripts/ci_shard.py 2 5                    # shard 2 of 5 (1-based)
    python scripts/ci_shard.py --refresh JUNIT_DIR    # rewrite scripts/ci_shard_timings.json

Files are dealt out longest first, each to the shard with the least time so far, using the
seconds each file took in a recent run (scripts/ci_shard_timings.json). A file with no
recorded time counts as the median, so a new file still lands somewhere sensible. A shard
runs whole files, never part of one, so module-scoped fixtures are not repeated.

Refresh the timings when the shards drift apart: download the `test-python-junit-*`
artifacts from a green CI run and pass the directory to --refresh.
tests/contracts/test_ci_shard.py proves the shards are disjoint, cover every file
`pytest tests/ --ignore=tests/integration` would collect, and stay balanced on the recorded
times. scripts/dod.sh still runs the whole suite in one process.
"""
from __future__ import annotations

import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMINGS = ROOT / "scripts" / "ci_shard_timings.json"


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


def load_timings(path: Path = TIMINGS) -> dict[str, float]:
    return json.loads(path.read_text()) if path.exists() else {}


def assign(files: list[str], total: int, timings: dict[str, float]) -> list[list[str]]:
    """Longest file first, onto the shard with the least time so far (ties: lowest shard)."""
    known = [timings[f] for f in files if f in timings]
    fallback = statistics.median(known) if known else 1.0
    weight = {f: timings.get(f, fallback) for f in files}
    shards: list[list[str]] = [[] for _ in range(total)]
    load = [0.0] * total
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        i = min(range(total), key=lambda k: (load[k], k))
        shards[i].append(f)
        load[i] += weight[f]
    return [sorted(s) for s in shards]


def shard(files: list[str], index: int, total: int, timings: dict[str, float] | None = None) -> list[str]:
    if not 1 <= index <= total:
        raise ValueError(f"shard {index} is not in 1..{total}")
    return assign(files, total, load_timings() if timings is None else timings)[index - 1]


def refresh(junit_dir: Path) -> int:
    """Sum each testcase's seconds into the file it belongs to (classname is the dotted path)."""
    files = set(test_files())
    seconds: dict[str, float] = {}
    reports = sorted(junit_dir.glob("**/junit.xml"))
    if not reports:
        print(f"no junit.xml under {junit_dir}", file=sys.stderr)
        return 1
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            parts = (case.get("classname") or "").split(".")
            for k in range(len(parts), 0, -1):
                candidate = "/".join(parts[:k]) + ".py"
                if candidate in files:
                    seconds[candidate] = seconds.get(candidate, 0.0) + float(case.get("time") or 0)
                    break
    TIMINGS.write_text(json.dumps({f: round(s, 1) for f, s in sorted(seconds.items())}, indent=0) + "\n")
    print(f"{len(seconds)} of {len(files)} files timed; {sum(seconds.values()):.0f} s of test time")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--refresh":
        return refresh(Path(argv[2]))
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
