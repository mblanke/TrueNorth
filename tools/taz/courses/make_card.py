#!/usr/bin/env python3
"""Print the taz-card JSON for one course: TAZ may write that course file and its
instructor pack, and nothing else.

    python tools/taz/courses/make_card.py content/courses/c103-intro-to-networking.yaml \
        --repo-root /data/taz/courses/work/c103/repo > /data/taz/courses/cards/c103.json

The card format is the one tools/taz/README.md documents ("The task card"); the write
gate's ring 4 confines every write to `allowed_paths` under `repo_root`. The oracle is
scripts/check_course.py and its tests, which policy.json also protects. Held courses
(scripts/check_course.py HELD_FILES) get no card.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DIFF_BUDGET = 900  # lab_range for six modules + a new instructor pack + quiz and topic fixes


def _held_files() -> frozenset[str]:
    spec = importlib.util.spec_from_file_location("check_course", ROOT / "scripts" / "check_course.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HELD_FILES


def card_for(course: str, *, repo_root: str | None = None, base_ref: str = "main") -> dict:
    rel = Path(course)
    if (
        rel.is_absolute()
        or rel.parent != Path("content/courses")
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.yaml", rel.name)
    ):
        raise ValueError(f"{course}: give the path as content/courses/<file>.yaml, relative to the repo root")
    if rel.name in _held_files():
        raise ValueError(f"{rel.name} is held for cleared authors; TAZ gets no card for it")
    card = {
        "allowed_paths": [rel.as_posix(), f"content/courses/instructor/{rel.name}"],
        "oracle_tests": ["scripts/check_course.py", "tests/tools/test_check_course.py"],
        "diff_budget": DIFF_BUDGET,
        "base_ref": base_ref,
    }
    if repo_root:
        card["repo_root"] = str(Path(repo_root).resolve())
    return card


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Emit the taz-card JSON for one course file.")
    ap.add_argument("course", help="content/courses/<file>.yaml")
    ap.add_argument("--repo-root", default=None, help="absolute path of the clone (the runner's local card needs it)")
    ap.add_argument("--base-ref", default="main")
    args = ap.parse_args(argv)
    try:
        card = card_for(args.course, repo_root=args.repo_root, base_ref=args.base_ref)
    except ValueError as exc:
        print(f"make_card: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(card, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
