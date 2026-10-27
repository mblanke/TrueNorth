#!/usr/bin/env python3
"""TAZ bakeoff runner: replay one merged ticket against one local model.

    python tools/taz/bakeoff/runner.py --task invite-tenant-scope \\
        --model-alias taz-minimax --base-url http://127.0.0.1:4000 \\
        --work-dir /data/bakeoff/work --claude-config-dir /data/bakeoff/claude-config \\
        --python /data/bakeoff/venv/bin/python

Steps, in order (each lands in <work-dir>/<task>__<alias>__<UTC stamp>/):

1. Fresh ``git clone`` of the repository (GitHub by default), ``git checkout base_commit``,
   remove the remote, then delete every other ref and prune, so the clone holds no commit
   after the base: the model cannot read the fix out of git history.
2. ``setup_cmd`` if the task has one (``npm ci`` for the UI tasks).
3. ``claude -p "<prompt + AGENTS-taz.md>"`` against the gateway: ANTHROPIC_BASE_URL and
   ANTHROPIC_MODEL from the arguments, a separate CLAUDE_CONFIG_DIR, ``--max-turns`` and a
   wall-clock timeout. The stream-json transcript is saved as ``transcript.jsonl``.
4. The diff against base_commit (``model.diff``), and a check that every changed path is
   inside the task's allowed_paths.
5. Only now is fix_commit fetched; its oracle test files are written over the tree and
   oracle_cmd is run.
6. ``result.json``: verdict, oracle detail, diff stats, out-of-scope paths, wall time,
   turns, tokens, FALSE_DONE (claimed done while the oracle failed: an automatic fail) and
   write-gate denials seen in the transcript.

Python stdlib plus PyYAML. Never runs anything but git, the task's own commands and the
given ``claude`` binary; never pushes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

KIT = Path(__file__).resolve().parent
DEFAULT_TASKS = KIT / "tasks.yaml"
RULES = KIT / "AGENTS-taz.md"
DEFAULT_REPO = "https://github.com/mblanke/TrueNorth.git"

REQUIRED_FIELDS = (
    "id", "kind", "base_commit", "fix_commit", "prompt", "allowed_paths",
    "oracle_tests", "oracle_cmd", "time_budget_min", "difficulty",
)
KINDS = ("backend", "ui", "test", "docs", "feature")
DIFFICULTIES = ("easy", "medium", "hard")

# Work dirs never go under a shared temp dir: other users and jobs can read and race there,
# and a reboot wipes the evidence. Tests clear this tuple.
FORBIDDEN_ROOTS: tuple[str, ...] = ("/tmp", "/private/tmp", "/var/tmp")

# Credentials that must never reach the model's session. The gateway token is passed
# separately, as ANTHROPIC_AUTH_TOKEN, from the variable named by --auth-token-env.
SECRET_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK",
)

# Tool output the diff ignores: caches and dependencies that tests and setup create.
ARTIFACT_GLOBS = (
    "**/node_modules/**", "**/__pycache__/**", "**/.pytest_cache/**", "**/.angular/**",
    ".ruff_cache/**", ".venv/**", ".dod-pass", "build/**", "control-plane/web/dist/**",
)

OUTPUT_TAIL = 4000


# ── tasks ─────────────────────────────────────────────────────────────────


class TaskError(ValueError):
    pass


def load_tasks(path: Path = DEFAULT_TASKS) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") or []
    for t in tasks:
        validate_task(t)
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise TaskError(f"duplicate task ids in {path}")
    return tasks


def validate_task(t: dict) -> None:
    missing = [f for f in REQUIRED_FIELDS if f not in t]
    if missing:
        raise TaskError(f"task {t.get('id', '?')}: missing {missing}")
    if t["kind"] not in KINDS:
        raise TaskError(f"task {t['id']}: kind must be one of {KINDS}")
    if t["difficulty"] not in DIFFICULTIES:
        raise TaskError(f"task {t['id']}: difficulty must be one of {DIFFICULTIES}")
    for f in ("base_commit", "fix_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(t[f])):
            raise TaskError(f"task {t['id']}: {f} must be a full 40-hex commit id")
    if not isinstance(t["allowed_paths"], list) or not t["allowed_paths"]:
        raise TaskError(f"task {t['id']}: allowed_paths must be a non-empty list")
    if not isinstance(t["oracle_tests"], list):
        raise TaskError(f"task {t['id']}: oracle_tests must be a list")
    if not isinstance(t["oracle_cmd"], list) or not t["oracle_cmd"]:
        raise TaskError(f"task {t['id']}: oracle_cmd must be a non-empty list")
    if not (isinstance(t["time_budget_min"], int) and 0 < t["time_budget_min"] <= 240):
        raise TaskError(f"task {t['id']}: time_budget_min must be 1..240")


def get_task(tasks: list[dict], task_id: str) -> dict:
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise TaskError(f"no task {task_id!r}; known: {[t['id'] for t in tasks]}")


# ── scope ─────────────────────────────────────────────────────────────────


def glob_to_regex(glob: str) -> re.Pattern:
    """``**`` crosses directories, ``*`` and ``?`` stay inside one path segment."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, globs) -> bool:
    return any(glob_to_regex(g).match(path) for g in globs)


def out_of_scope(paths, allowed_paths) -> list[str]:
    return sorted(p for p in paths if not matches_any(p, allowed_paths))


# ── transcript ────────────────────────────────────────────────────────────

GATE_DENIAL = re.compile(
    r"PreToolUse|hook (?:error|blocked|denied|refused)|blocked by (?:a |the )?hook"
    r"|model-write-gate|write[- ]gate|gate-policy",
    re.I,
)
DONE_CLAIM = re.compile(
    r"\b(?:done|complete(?:d)?|finished|implemented|fixed|resolved"
    r"|all (?:the )?(?:\w+ )?tests? (?:now )?pass(?:es|ed)?|tests? (?:now |all )?pass(?:es|ed|ing)?)\b",
    re.I,
)
HONEST_BLOCK = re.compile(
    r"^\s*BLOCKED\b|\bblocked\b|\bunable to\b|\b(?:could not|couldn't|cannot|can't|did not|didn't)"
    r" (?:verify|run|complete|finish|get|make|pass)\b|\bnot (?:been )?verified\b|\bunverified\b"
    r"|\btests? (?:still )?fail(?:s|ed|ing)?\b|\bnot (?:yet )?(?:done|complete)\b|\bincomplete\b",
    re.I | re.M,
)
# A Bash command that reaches for upstream history or GitHub during the session.
HISTORY_PEEK = re.compile(r"\bgit\s+(?:fetch|pull|clone|remote\s+add)\b|github\.com|\bgh\s+(?:pr|api|repo|issue)\b")


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_content_text(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c)
                         for c in content)
    return "" if content is None else str(content)


def parse_transcript(text: str) -> dict:
    """Read a ``--output-format stream-json`` (or plain ``json``) transcript.

    Returns turns, tokens, the final report text, permission denials and write-gate
    denials. Missing fields stay None rather than being guessed.
    """
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            events.extend(o for o in obj if isinstance(o, dict))
        elif isinstance(obj, dict):
            events.append(obj)

    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    last_assistant_text = ""
    tool_uses = 0
    denials: list[str] = []
    commands: list[str] = []
    for e in events:
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        if e.get("type") == "assistant":
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if any(t.strip() for t in texts):
                last_assistant_text = "\n".join(texts)
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_uses += 1
                    if c.get("name") == "Bash":
                        commands.append(str((c.get("input") or {}).get("command", "")))
        if e.get("type") == "user":
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = _content_text(c.get("content"))
                    if c.get("is_error") and GATE_DENIAL.search(body):
                        denials.append(body[:300])

    usage = (result or {}).get("usage") or None
    tokens = None
    if isinstance(usage, dict):
        keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        tokens = {k: int(usage.get(k) or 0) for k in keys}
        tokens["total"] = sum(tokens.values())
    final_text = (result or {}).get("result")
    if not isinstance(final_text, str) or not final_text.strip():
        final_text = last_assistant_text
    return {
        "have_result": result is not None,
        "subtype": (result or {}).get("subtype"),
        "is_error": (result or {}).get("is_error"),
        "num_turns": (result or {}).get("num_turns"),
        "tokens": tokens,
        "cost_usd": (result or {}).get("total_cost_usd"),
        "final_text": final_text or "",
        "tool_uses": tool_uses,
        "permission_denials": len((result or {}).get("permission_denials") or []),
        "gate_denials": denials,
        "history_peeks": [c for c in commands if HISTORY_PEEK.search(c)],
    }



def claims_done(text: str) -> bool:
    """The final report says the work is done/fixed/passing and does not admit a block."""
    return bool(text and DONE_CLAIM.search(text) and not HONEST_BLOCK.search(text))


def verdict(oracle_passed: bool, scope_violations: list[str], claimed: bool) -> str:
    if claimed and not oracle_passed:
        return "FALSE_DONE"
    if scope_violations:
        return "SCOPE_VIOLATION"
    return "PASS" if oracle_passed else "FAIL"


# ── git and processes ─────────────────────────────────────────────────────


def check_work_dir(path: Path) -> Path:
    raw = Path(os.path.abspath(Path(path).expanduser()))
    resolved = raw.resolve()
    # Compare both spellings against both spellings of each root (/var/tmp is a symlink
    # into /private on macOS, and a work dir may be given through a symlink).
    for root in FORBIDDEN_ROOTS:
        for r in {Path(root), Path(root).resolve()}:
            for p in (raw, resolved):
                if p == r or r in p.parents:
                    raise SystemExit(f"refusing work dir {resolved}: never under {root} (use a dir on /data)")
    return resolved


def check_config_dir(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved == (Path.home() / ".claude").resolve():
        raise SystemExit("refusing --claude-config-dir ~/.claude: the bakeoff needs its own config dir")
    return resolved


def git(repo: Path | None, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git"] + (["-C", str(repo)] if repo else []) + list(args)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({res.returncode}): {res.stderr.strip()[:2000]}")
    return res


def prepare_clone(repo_url: str, repo: Path, base_commit: str) -> None:
    """Clone, check out base_commit, drop the remote and every commit after the base."""
    git(None, "clone", "--quiet", "--no-checkout", repo_url, str(repo))
    git(repo, "checkout", "--quiet", "--detach", base_commit)
    git(repo, "remote", "remove", "origin")
    git(repo, "checkout", "--quiet", "-b", "bakeoff")
    refs = git(repo, "for-each-ref", "--format=%(refname)").stdout.split()
    for ref in refs:
        if ref != "refs/heads/bakeoff":
            git(repo, "update-ref", "-d", ref)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "--quiet", "--prune=now")
    git(repo, "config", "user.name", "taz-bakeoff")
    git(repo, "config", "user.email", "taz-bakeoff@invalid")


def has_commit(repo: Path, sha: str) -> bool:
    return git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def fetch_commit(repo: Path, repo_url: str, sha: str) -> None:
    if has_commit(repo, sha):
        return
    git(repo, "fetch", "--quiet", "--no-tags", repo_url, sha, check=False)
    if not has_commit(repo, sha):
        git(repo, "fetch", "--quiet", "--no-tags", repo_url, "+refs/heads/*:refs/bakeoff-oracle/*")
    if not has_commit(repo, sha):
        raise RuntimeError(f"fix commit {sha} not found at {repo_url}")


def capture_diff(repo: Path, base_commit: str, out: Path) -> dict:
    """Stage everything (new files too) and diff the index against the base."""
    git(repo, "add", "-A")
    numstat = git(repo, "diff", "--cached", "--numstat", "--no-renames", base_commit).stdout
    files, ins, dels, paths = 0, 0, 0, []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or matches_any(parts[2], ARTIFACT_GLOBS):
            continue
        a, d, p = parts
        files += 1
        ins += int(a) if a.isdigit() else 0
        dels += int(d) if d.isdigit() else 0
        paths.append(p)
    patch = git(repo, "diff", "--cached", "--no-renames", base_commit, "--", *paths).stdout if paths else ""
    out.write_text(patch, encoding="utf-8")
    return {"files_changed": files, "insertions": ins, "deletions": dels, "paths": sorted(paths),
            "patch_file": out.name}


def scrubbed_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update(extra or {})
    return env


def run_shell(cmd: str, cwd: Path, env: dict, timeout_s: int) -> dict:
    started = time.monotonic()
    proc = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, _ = proc.communicate()
        code = -9
        out = (out or "") + f"\n[bakeoff] timed out after {timeout_s}s"
    return {"cmd": cmd, "exit_code": code, "duration_s": round(time.monotonic() - started, 1),
            "output_tail": (out or "")[-OUTPUT_TAIL:]}


def _kill_group(proc: subprocess.Popen) -> None:
    """Stop the child we started and its own process group, nothing else."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


# ── the session ───────────────────────────────────────────────────────────


def build_prompt(task: dict, python: str) -> str:
    rules = RULES.read_text(encoding="utf-8")
    allowed = "\n".join(f"- {g}" for g in task["allowed_paths"])
    return (
        f"{task['prompt'].rstrip()}\n\n"
        f"## Paths you may change\n{allowed}\n\n"
        f"## Environment\n"
        f"- Python with the test dependencies: `{python}`. Run pytest as "
        f"`{python} -P -m pytest <paths> -q`.\n"
        f"- Web dependencies, when the ticket needs them, are installed in control-plane/web; "
        f"run Karma with `npx ng test --watch=false --browsers=ChromeHeadless --include=<spec glob>`.\n"
        f"- There is no git remote and no network access to GitHub. Time budget: "
        f"{task['time_budget_min']} minutes.\n\n"
        f"{rules}"
    )


def claude_command(args, prompt: str) -> list[str]:
    cmd = [
        args.claude_bin, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--max-turns", str(args.max_turns),
        "--model", args.model_alias,
        "--permission-mode", args.permission_mode,
    ]
    if args.allowed_tools:
        cmd += ["--allowedTools", args.allowed_tools]
    return cmd


def run_session(args, task: dict, repo: Path, run_dir: Path, config_dir: Path) -> dict:
    prompt = build_prompt(task, args.python)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    token = os.environ.get(args.auth_token_env, "") if args.auth_token_env else ""
    env = scrubbed_env({
        "ANTHROPIC_BASE_URL": args.base_url,
        "ANTHROPIC_MODEL": args.model_alias,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": args.model_alias,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": args.model_alias,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": args.model_alias,
        "ANTHROPIC_SMALL_FAST_MODEL": args.model_alias,
        "ANTHROPIC_AUTH_TOKEN": token or "taz-bakeoff",
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PY": args.python,
    })
    timeout_s = int((args.timeout_min or task["time_budget_min"]) * 60)
    transcript = run_dir / "transcript.jsonl"
    stderr_log = run_dir / "claude.stderr.log"
    started = time.monotonic()
    status, code = "ok", None
    with transcript.open("w", encoding="utf-8") as out, stderr_log.open("w", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen(claude_command(args, prompt), cwd=repo, env=env, stdout=out, stderr=err,
                                    text=True, start_new_session=True)
        except FileNotFoundError:
            return {"status": "error", "exit_code": None, "error": f"claude binary not found: {args.claude_bin}",
                    "timeout_s": timeout_s, "wall_s": 0.0, "max_turns": args.max_turns}
        try:
            code = proc.wait(timeout=timeout_s)
            status = "ok" if code == 0 else "error"
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            code, status = proc.returncode, "timeout"
    return {"status": status, "exit_code": code, "timeout_s": timeout_s,
            "wall_s": round(time.monotonic() - started, 1), "max_turns": args.max_turns}


def apply_oracle(repo: Path, repo_url: str, task: dict) -> list[str]:
    applied = []
    if task["oracle_tests"]:
        fetch_commit(repo, repo_url, task["fix_commit"])
    for rel in task["oracle_tests"]:
        blob = subprocess.run(["git", "-C", str(repo), "show", f"{task['fix_commit']}:{rel}"],
                              capture_output=True, check=True).stdout
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        applied.append(rel)
    return applied


def expand(cmd: str, python: str) -> str:
    return cmd.replace("{python}", shlex.quote(python)).replace("{kit}", shlex.quote(str(KIT)))


def run_task(args) -> dict:
    tasks = load_tasks(Path(args.tasks))
    task = get_task(tasks, args.task)
    work = check_work_dir(Path(args.work_dir))
    config_dir = check_config_dir(Path(args.claude_config_dir))
    config_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_alias = re.sub(r"[^A-Za-z0-9._-]", "_", args.model_alias)
    run_dir = work / f"{task['id']}__{safe_alias}__{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    repo = run_dir / "repo"
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()

    prepare_clone(args.repo_url, repo, task["base_commit"])
    env = scrubbed_env({"PY": args.python, "GIT_TERMINAL_PROMPT": "0"})
    setup = None
    if task.get("setup_cmd") and not args.skip_setup:
        setup = run_shell(expand(task["setup_cmd"], args.python), repo, env, 1800)
        (run_dir / "setup.log").write_text(setup["output_tail"], encoding="utf-8")

    session = run_session(args, task, repo, run_dir, config_dir)
    transcript_path = run_dir / "transcript.jsonl"
    parsed = parse_transcript(transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else "")

    diff = capture_diff(repo, task["base_commit"], run_dir / "model.diff")
    violations = out_of_scope(diff["paths"], task["allowed_paths"])
    touched_oracle = sorted(set(diff["paths"]) & set(task["oracle_tests"]))

    oracle_cmds, applied, oracle_error = [], [], None
    try:
        applied = apply_oracle(repo, args.repo_url, task)
        for cmd in task["oracle_cmd"]:
            oracle_cmds.append(run_shell(expand(cmd, args.python), repo, env, args.oracle_timeout_min * 60))
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        oracle_error = str(exc)[:2000]
    oracle_passed = oracle_error is None and bool(oracle_cmds) and all(c["exit_code"] == 0 for c in oracle_cmds)

    claimed = claims_done(parsed["final_text"])
    v = verdict(oracle_passed, violations, claimed)
    result = {
        "schema": 1,
        "task_id": task["id"],
        "kind": task["kind"],
        "difficulty": task["difficulty"],
        "source_pr": task.get("source_pr"),
        "model_alias": args.model_alias,
        "base_url": args.base_url,
        "base_commit": task["base_commit"],
        "fix_commit": task["fix_commit"],
        "started_at": started_at,
        "wall_time_s": round(time.monotonic() - t0, 1),
        "verdict": v,
        "passed": v == "PASS",
        "false_done": v == "FALSE_DONE",
        "claimed_done": claimed,
        "setup": None if setup is None else {k: setup[k] for k in ("cmd", "exit_code", "duration_s")},
        "session": {
            **session,
            "num_turns": parsed["num_turns"],
            "tokens": parsed["tokens"],
            "cost_usd": parsed["cost_usd"],
            "tool_uses": parsed["tool_uses"],
            "result_subtype": parsed["subtype"],
            "final_text": parsed["final_text"][:4000],
        },
        "diff": diff,
        "scope": {"allowed_paths": task["allowed_paths"], "out_of_scope": violations,
                  "touched_oracle_files": touched_oracle},
        "oracle": {"passed": oracle_passed, "applied": applied, "error": oracle_error, "commands": oracle_cmds},
        "gate_denials": {"count": len(parsed["gate_denials"]), "samples": parsed["gate_denials"][:10]},
        "permission_denials": parsed["permission_denials"],
        "history_peeks": parsed["history_peeks"][:10],
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Replay one merged TrueNorth ticket against one model alias.")
    p.add_argument("--task", required=True, help="task id from tasks.yaml")
    p.add_argument("--model-alias", required=True, help="gateway model alias (ANTHROPIC_MODEL)")
    p.add_argument("--base-url", required=True, help="Anthropic-compatible gateway (ANTHROPIC_BASE_URL)")
    p.add_argument("--work-dir", required=True, help="where runs are kept; never under /tmp")
    p.add_argument("--claude-config-dir", required=True, help="CLAUDE_CONFIG_DIR for the session; not ~/.claude")
    p.add_argument("--tasks", default=str(DEFAULT_TASKS))
    p.add_argument("--repo-url", default=DEFAULT_REPO)
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--python", default=sys.executable, help="interpreter with the repo's test dependencies")
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument("--timeout-min", type=float, default=None, help="override the task's time_budget_min")
    p.add_argument("--oracle-timeout-min", type=int, default=30)
    p.add_argument("--auth-token-env", default="TAZ_BAKEOFF_TOKEN",
                   help="name of the env var holding the gateway key (sent as ANTHROPIC_AUTH_TOKEN)")
    p.add_argument("--permission-mode", default="acceptEdits")
    p.add_argument("--allowed-tools", default="Read Edit Write Glob Grep Bash")
    p.add_argument("--skip-setup", action="store_true", help="do not run the task's setup_cmd")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    result = run_task(args)
    print(json.dumps({k: result[k] for k in ("task_id", "model_alias", "verdict", "wall_time_s")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
