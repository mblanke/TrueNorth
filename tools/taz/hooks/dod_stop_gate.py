#!/usr/bin/env python3
"""Stop hook: refuse to end a turn that changed code without a green DoD on that state.

Vendored from TAZ's dod-stop-gate.py. The 2026-08-19 incident did not end with a crash.
It ended with the model writing a document asserting the work was complete, having never
once run pytest, ruff or dod.sh. Every other control was aimed at the writing; this one
is aimed at the claim.

Scope is deliberately narrow so it does not become the thing people switch off:
  - only fires in repos that actually have scripts/dod.sh;
  - only when code files (CODE_PATHSPECS) differ from HEAD, or new untracked ones exist,
    and scripts/dod.sh has not passed on exactly this state;
  - reminds once per session and state, then stands down so the session can finish.

"This state" is the first line of `.dod-pass`, which scripts/dod.sh writes on success:

    <git rev-parse HEAD, or "nohead">:<first 16 hex of sha256(`git diff HEAD`)>
    <UTC timestamp>

fingerprint() must stay byte-identical to that recipe; tests/tools/test_taz_hooks.py
runs both and compares. Known limit of the recipe: `git diff HEAD` does not see untracked
files, so editing a NEW file after a green run does not change the fingerprint.

Exit 2 on Stop prevents Claude from stopping and feeds stderr back as the reason.
Configuration: $TAZ_STOP_STATE overrides the reminder state file.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

MARKER = ".dod-pass"
DEFAULT_STATE = pathlib.Path.home() / ".claude" / "hooks" / ".dod-stop-state"
# What counts as code for this check: the Python the original guarded, plus the Angular
# sources TAZ's UI cards touch (DOD_WEB=1 covers them).
CODE_PATHSPECS = ("*.py", "*.ts", "*.html", "*.scss", "*.css", "*.js", "*.mjs", "*.json")


def git(args: list[str], cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def fingerprint(root: str) -> str | None:
    """`HEAD:sha256(git diff HEAD)[:16]`, exactly as scripts/dod.sh writes `.dod-pass`.

    dod.sh: `git rev-parse HEAD 2>/dev/null || echo nohead` and
    `git diff HEAD 2>/dev/null | sha256sum | cut -c1-16`. A failing `git diff` (no HEAD)
    hashes empty output there, so it does here too.
    """
    head = git(["rev-parse", "HEAD"], root) or "nohead"
    try:
        diff = subprocess.run(["git", "diff", "HEAD"], cwd=root, capture_output=True, timeout=30).stdout
    except Exception:
        return None
    return f"{head}:{hashlib.sha256(diff).hexdigest()[:16]}"


def passed_state(root: str) -> str | None:
    try:
        lines = (pathlib.Path(root) / MARKER).read_text().splitlines()
    except Exception:
        return None
    return lines[0].strip() if lines else None


def changed_code(root: str) -> list[str]:
    tracked = git(["diff", "--name-only", "HEAD", "--", *CODE_PATHSPECS], root) or ""
    untracked = git(["ls-files", "--others", "--exclude-standard", "--", *CODE_PATHSPECS], root) or ""
    seen: dict[str, None] = {}
    for f in (tracked + "\n" + untracked).splitlines():
        if f.strip():
            seen[f.strip()] = None
    return list(seen)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # Never wedge a session on a malformed Stop payload.
    if not isinstance(payload, dict):
        return 0

    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    root = git(["rev-parse", "--show-toplevel"], cwd)
    if not root or not os.path.isfile(os.path.join(root, "scripts", "dod.sh")):
        return 0

    changed = changed_code(root)
    if not changed:
        return 0

    current = fingerprint(root)
    passed = passed_state(root)
    if passed and current and passed == current:
        return 0

    # Remind once per session and state, then stand down so the turn can end.
    state = pathlib.Path(os.environ.get("TAZ_STOP_STATE") or DEFAULT_STATE)
    key = f"{payload.get('session_id')}:{current}"
    try:
        seen = state.read_text().splitlines()
    except Exception:
        seen = []
    if key in seen:
        return 0
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("\n".join((seen + [key])[-200:]) + "\n")
    except Exception:
        pass

    listing = "\n".join(f"  - {f}" for f in changed[:12])
    more = f"\n  ...and {len(changed) - 12} more" if len(changed) > 12 else ""
    sys.stderr.write(
        f"{len(changed)} code file(s) changed, but ./scripts/dod.sh has not passed on this "
        f"exact state:\n{listing}{more}\n\n"
        "Run `bash scripts/dod.sh` (with DOD_WEB=1 for Angular changes) and fix what it "
        "reports before finishing. If you genuinely cannot run it, say so explicitly and say "
        "why; do not describe the work as done. (This check stands down after one reminder "
        "per change.)\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
