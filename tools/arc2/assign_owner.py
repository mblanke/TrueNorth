"""Give unowned Course Studio runs a tenant.

The Studio API shows a run only to the tenant recorded as ``tenant_id`` in
``<runs>/_studio/<slug>.json``. Runs started from Claude Code, and runs created before
the Studio recorded ownership, have none and are visible to nobody. This records one.

* Adds ``tenant_id`` (and ``name`` from the manifest when there is no metadata) to the
  Studio metadata; keeps every other field. Writes are atomic (temp file + rename).
* Never reassigns a run that already belongs to another tenant, and never touches
  ``manifest.json`` or anything the engine wrote.
* Idempotent and resumable: re-running assigns only what is still unowned. Prints the
  unowned count before and after so the result can be checked.

Usage::

    PYTHONPATH=tools .venv/bin/python -m arc2.assign_owner --tenant <uuid> --all-unowned [--dry-run]
    PYTHONPATH=tools .venv/bin/python -m arc2.assign_owner --tenant <uuid> --slug arc2-x [--slug ...]
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from arc2.runner import REPO_ROOT, SLUG_RE


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tenant(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a tenant UUID: {value!r}") from None


def run_slugs(runs: Path) -> set[str]:
    """Every run the Studio could list: run directories and Studio metadata."""
    slugs = {p.name for p in runs.iterdir() if p.is_dir() and SLUG_RE.match(p.name)} if runs.is_dir() else set()
    studio = runs / "_studio"
    if studio.is_dir():
        slugs |= {p.stem for p in studio.glob("*.json") if SLUG_RE.match(p.stem)}
    return slugs


def owner_of(runs: Path, slug: str) -> str | None:
    return ((_read_json(runs / "_studio" / f"{slug}.json") or {}).get("tenant_id")) or None


def _assign(runs: Path, slug: str, tenant: str) -> None:
    studio = runs / "_studio"
    studio.mkdir(parents=True, exist_ok=True)
    path = studio / f"{slug}.json"
    meta = _read_json(path) or {}
    if "name" not in meta:
        course = ((_read_json(runs / slug / "manifest.json") or {}).get("course")) or {}
        meta["name"] = course.get("title") or slug
    meta["tenant_id"] = tenant
    tmp = studio / f".{slug}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta))
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arc2.assign_owner", description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=Path, default=Path(os.environ.get("ARC2_RUNS_DIR", REPO_ROOT / "build" / "arc2")))
    ap.add_argument("--tenant", type=_tenant, required=True, help="the owning tenant's UUID")
    pick = ap.add_mutually_exclusive_group(required=True)
    pick.add_argument("--slug", action="append", help="a run to assign (repeatable)")
    pick.add_argument("--all-unowned", action="store_true", help="every run without an owner")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args(argv)

    runs: Path = args.runs
    known = run_slugs(runs)
    unowned_before = sorted(s for s in known if not owner_of(runs, s))
    targets = unowned_before if args.all_unowned else args.slug
    problems = 0
    assigned = 0
    for slug in targets:
        if not SLUG_RE.match(slug) or slug not in known:
            print(f"{slug}: no such run")
            problems += 1
            continue
        owner = owner_of(runs, slug)
        if owner and owner.lower() != args.tenant:
            print(f"{slug}: owned by another tenant; not changed")
            problems += 1
            continue
        if owner:
            continue
        if not args.dry_run:
            _assign(runs, slug, args.tenant)
        print(f"{slug}: {'would assign' if args.dry_run else 'assigned'} to {args.tenant}")
        assigned += 1

    unowned_after = sum(1 for s in run_slugs(runs) if not owner_of(runs, s))
    print(f"unowned before: {len(unowned_before)}")
    print(f"{'would assign' if args.dry_run else 'assigned'}: {assigned}")
    print(f"unowned after: {unowned_after}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
