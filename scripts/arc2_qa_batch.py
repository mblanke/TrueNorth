#!/usr/bin/env python3
"""Run ARC²'s QA over many existing courses and write one summary table.

    python scripts/arc2_qa_batch.py 'content/courses/c1*.yaml'
    python scripts/arc2_qa_batch.py content/courses/c103-*.yaml content/courses/c104-*.yaml --out build/arc2-qa

Each argument is a file or a glob (quoted globs are expanded here, relative to the repo root
when not found from the current directory). Each course goes through
scripts/arc2_qa_course.py with its instructor pack from content/courses/instructor/, and gets
its own report under ``<out>/<course stem>/06-qa/``. The summary lands in
``<out>/summary.md`` and ``<out>/summary.json``: course, checks run, failures, open human
actions, needs-range-support items and the lab hosts verified against each range template.

Exit 1 when any course has a blocking finding (a ``fail``); 0 otherwise; 2 on bad arguments.
No network, no model call.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import arc2_qa_course as one  # noqa: E402


def expand(specs: list[str], root: Path = ROOT) -> list[Path]:
    """Files named by the arguments, globs expanded, de-duplicated, sorted by name."""
    found: dict[Path, None] = {}
    for spec in specs:
        hits = sorted(glob.glob(spec)) or sorted(glob.glob(str(root / spec)))
        for h in hits:
            p = Path(h).resolve()
            if p.is_file() and p.suffix in (".yaml", ".yml"):
                found[p] = None
    return sorted(found, key=lambda p: p.name)


def hosts_cell(verified: dict[str, list[str]]) -> str:
    return "; ".join(f"{t}: {', '.join(h)}" for t, h in verified.items()) or "none"


def summarise(reports: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for r in reports:
        n = r["counts"]
        rows.append(
            {
                "course": r["course"]["catalogue_code"] or r["course"]["code"] or r["course"]["file"],
                "file": r["course"]["file"],
                "result": r["result"],
                "checks_ran": n["checks_ran"],
                "checks_not_applicable": n["checks_not_applicable"],
                "checks_not_run": n["checks_not_run"],
                "failures": n["blocking"],
                "failing_checks": sorted({f["check"] for f in r["findings"] if f["severity"] == "fail"}),
                "open_human_actions": n["open_human_actions"],
                "needs_range_support": n["needs_range_support"],
                "lab_hosts_verified": r["lab_hosts_verified"],
                "report": f"{r['out']}/06-qa/report.md",
            }
        )
    return {
        "schema": "arc2-qa-batch/1",
        "courses": len(rows),
        "blocking_courses": sum(1 for x in rows if x["failures"]),
        "result": "fail" if any(x["failures"] for x in rows) else "pass",
        "rows": rows,
    }


def render_markdown(s: dict[str, Any]) -> str:
    lines = [
        "# ARC² QA over existing courses",
        "",
        f"{s['courses']} courses, {s['blocking_courses']} with a blocking finding. Result: **{s['result']}**.",
        "",
        "Checks: `ran` / `not applicable (TAZ course)` / `not run`. Failures are blocking findings only.",
        "",
        "| Course | File | Result | Checks ran / n/a / not run | Failures | Open human actions | Needs range support | Lab hosts verified against the range template |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for x in s["rows"]:
        fails = f"{x['failures']} ({', '.join(x['failing_checks'])})" if x["failures"] else "0"
        lines.append(
            f"| {x['course']} | {x['file']} | {x['result']} | {x['checks_ran']} / {x['checks_not_applicable']} / {x['checks_not_run']} "
            f"| {fails} | {x['open_human_actions']} | {x['needs_range_support']} | {hosts_cell(x['lab_hosts_verified'])} |"
        )
    return "\n".join(lines).replace("|  |", "| |") + "\n"


def run(courses: list[Path], out: Path, root: Path = ROOT, held: bool = True) -> dict[str, Any]:
    reports = [one.qa_course(c, None, out / c.stem, root, held) for c in courses]
    summary = summarise(reports)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run ARC²'s QA over many existing courses and write one summary.")
    ap.add_argument("courses", nargs="+", help="course files or globs (content/courses/*.yaml)")
    ap.add_argument("--out", type=Path, default=one.DEFAULT_OUT, help=f"report directory (default: {one.DEFAULT_OUT})")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root holding content/")
    ap.add_argument(
        "--no-held", dest="held", action="store_false", help="do not refuse the seven courses held for cleared authors"
    )
    args = ap.parse_args(argv)
    files = expand(args.courses, args.root)
    if not files:
        print("no course files matched", file=sys.stderr)
        return 2
    summary = run(files, args.out.resolve(), args.root.resolve(), args.held)
    print((args.out / "summary.md").read_text(encoding="utf-8"))
    return 1 if summary["result"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
