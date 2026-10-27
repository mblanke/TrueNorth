#!/usr/bin/env python3
"""Aggregate bakeoff result.json files into a markdown leaderboard, one row per model.

    python tools/taz/bakeoff/score.py /data/bakeoff/work --out leaderboard.md

Ranking: pass rate, then fewer FALSE_DONE claims, then fewer scope violations, then
median wall time. A FALSE_DONE run (the model said done, the oracle failed) is a fail and
is also counted on its own, because an engine that lies about success is unusable even when
it is often right.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def load_results(root: Path) -> list[dict]:
    results = []
    for path in sorted(Path(root).rglob("result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("schema") == 1 and data.get("model_alias") and data.get("task_id"):
            results.append(data)
    return results


def _tokens(r: dict) -> int | None:
    tok = (r.get("session") or {}).get("tokens")
    return tok.get("total") if isinstance(tok, dict) else None


def aggregate(results: list[dict]) -> list[dict]:
    by_model: dict[str, list[dict]] = {}
    for r in results:
        by_model.setdefault(r["model_alias"], []).append(r)
    rows = []
    for model, runs in by_model.items():
        passed = sum(1 for r in runs if r.get("verdict") == "PASS")
        times = [float(r["wall_time_s"]) for r in runs if isinstance(r.get("wall_time_s"), (int, float))]
        toks = [t for t in (_tokens(r) for r in runs) if t is not None]
        rows.append({
            "model": model,
            "runs": len(runs),
            "tasks": len({r["task_id"] for r in runs}),
            "passed": passed,
            "pass_rate": passed / len(runs),
            "false_done": sum(1 for r in runs if r.get("verdict") == "FALSE_DONE" or r.get("false_done")),
            "scope_violations": sum(1 for r in runs if (r.get("scope") or {}).get("out_of_scope")),
            "median_wall_s": statistics.median(times) if times else None,
            "gate_denials": sum(int((r.get("gate_denials") or {}).get("count") or 0) for r in runs),
            "timeouts": sum(1 for r in runs if (r.get("session") or {}).get("status") == "timeout"),
            "median_tokens": int(statistics.median(toks)) if toks else None,
        })
    rows.sort(key=lambda x: (-x["pass_rate"], x["false_done"], x["scope_violations"],
                             x["median_wall_s"] if x["median_wall_s"] is not None else float("inf"), x["model"]))
    return rows


def _minutes(s: float | None) -> str:
    return "—" if s is None else f"{s / 60:.1f}"


def render_markdown(results: list[dict]) -> str:
    rows = aggregate(results)
    lines = [
        "# TAZ bakeoff leaderboard",
        "",
        f"{len(results)} run(s), {len({r['model_alias'] for r in results})} model(s), "
        f"{len({r['task_id'] for r in results})} task(s). PASS needs the hidden oracle green and no "
        "out-of-scope path. FALSE_DONE (claimed done, oracle failed) is an automatic fail.",
        "",
        "| Rank | Model | Pass rate | Passed / runs | FALSE_DONE | Scope violations | Median time (min) "
        "| Gate denials | Timeouts | Median tokens |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['model']} | {r['pass_rate']:.0%} | {r['passed']} / {r['runs']} | {r['false_done']} "
            f"| {r['scope_violations']} | {_minutes(r['median_wall_s'])} | {r['gate_denials']} | {r['timeouts']} "
            f"| {r['median_tokens'] if r['median_tokens'] is not None else '—'} |"
        )
    tasks = sorted({r["task_id"] for r in results})
    models = [r["model"] for r in rows]
    if tasks:
        lines += ["", "## Verdict per task", "", "| Task | " + " | ".join(models) + " |",
                  "|---|" + "---|" * len(models)]
        for t in tasks:
            cells = []
            for m in models:
                verdicts = [r["verdict"] for r in results if r["task_id"] == t and r["model_alias"] == m]
                cells.append(", ".join(verdicts) if verdicts else "—")
            lines.append(f"| {t} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Aggregate bakeoff result.json files into a leaderboard.")
    p.add_argument("results_dir", help="directory searched recursively for result.json")
    p.add_argument("--out", help="write the markdown here instead of stdout")
    args = p.parse_args(argv)
    results = load_results(Path(args.results_dir))
    if not results:
        print(f"no result.json under {args.results_dir}", file=sys.stderr)
        return 1
    md = render_markdown(results)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
