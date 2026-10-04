"""Course Studio runner: starts and resumes ``/arc2`` runs for the web page.

The seven ARC² agents are Claude Code subagents driven by ``.claude/commands/arc2.md``;
a server process cannot launch them. This runner is the bridge: it runs on the machine
that has Claude Code (outside Docker), watches a queue directory that the API writes
into, and runs ``claude -p "/arc2 …"`` headlessly in the repository, one job at a time.

``/arc2`` stops at the outline gate and at the preview gate, so each job is short-lived:
"start" runs stage 1 and stops at the outline; "resume … accept" runs until the next
gate; feedback re-runs the stages it routes to. The page shows the run from its
``manifest.json`` and the job records below.

Queue contract (the API writes, the runner reads):

    <runs>/_queue/<created>-<id>.json   {"id", "action": "start"|"resume",
                                          "slug", "text", "created_at", "requested_by",
                                          "tenant_id"}

``tenant_id`` is the owning tenant, carried into the job record for audit. The runner
does not authorize: the API only queues jobs for runs the caller's tenant owns.

* ``start``: ``text`` is the course request; runs ``/arc2 --slug <slug> <text>``.
* ``resume``: ``text`` is ``accept`` or feedback; runs ``/arc2 --resume <slug> <text>``.

Job records (the runner writes, the API reads):

    <runs>/_jobs/<id>.json   the queued fields plus state (running|done|failed),
                             started_at, finished_at, exit_code, current_agent,
                             result, error, cost_usd, turns, log
    <runs>/_jobs/<id>.log    the raw stream-json output

Nothing here commits, provisions or imports; ``/arc2``'s own hard rules apply. Edits are
confined to ``build/arc2/``; Bash to the few commands ``/arc2`` needs; anything else is
refused. If Claude cannot be used (signed out, usage limit, overloaded), the same step is
re-run on a local Ollama through its Anthropic-compatible API (see ``Fallback``); the job
record's ``engine`` says which ran.

Usage: ``PYTHONPATH=tools .venv/bin/python -m arc2.runner [--once]``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SLUG_RE = re.compile(r"^arc2-[a-z0-9-]{1,60}$")
ACTIONS = {"start", "resume"}
MAX_TEXT = 4000
DEFAULT_TIMEOUT = 4 * 3600

# What a headless /arc2 may use. Edits are confined to the runs directory (build/arc2,
# gitignored) whichever model is driving; Bash is limited to the commands arc2.md runs.
# With --permission-mode dontAsk, anything not listed is refused, not prompted.
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "Task", "TodoWrite",
    "Write(./build/arc2/**)", "Edit(./build/arc2/**)",
    "Bash(.venv/bin/python:*)",
    "Bash(PYTHONPATH=tools .venv/bin/python:*)",
    "Bash(git status:*)", "Bash(git rev-parse:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(mkdir:*)", "Bash(ls:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(cat:*)", "Bash(wc:*)",
    "Bash(echo:*)", "Bash(printf:*)", "Bash(grep:*)", "Bash(sort:*)", "Bash(diff:*)", "Bash(stat:*)",
    "Bash(test:*)", "Bash(true)", "Bash(shasum:*)", "Bash(command -v:*)",
]

# Headless, a chained command is allowed only if every part is on the list above, and a
# variable assignment (RUN=...) is not. /arc2 writes its steps that way, so tell it how
# to phrase them; PYTHONPATH is preset so the plain form works.
RUNNER_GUIDANCE = (
    "You are running headless for the ARC2 Course Studio runner; nobody can approve a prompt. "
    "Run shell commands one at a time: no variable assignments (RUN=..., PY=...), no $VARIABLES, "
    "no command substitution. Write $RUN as the absolute run directory and $ARC as "
    "`.venv/bin/python -m arc2.check` (PYTHONPATH=tools is already set). "
    "Chaining with ; && | is fine only between allowed commands: python via .venv/bin/python, "
    "git status/rev-parse/diff/log, ls, cat, head, tail, wc, grep, sort, diff, stat, echo, printf, test, mkdir. "
    "File edits are allowed only under build/arc2/."
)

AGENT_NAMES = {
    "arc2-content-architect": "Content Architect",
    "arc2-code-generator": "Code Generator",
    "arc2-range-engineer": "Range Engineer",
    "arc2-artifact-creator": "Artifact Creator",
    "arc2-sensor-gateway": "Sensor Gateway",
    "arc2-qa-tester": "QA Tester",
    "arc2-package-builder": "Package Builder",
}


class JobError(ValueError):
    """A queued job that cannot be run as written."""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def dirs(runs: Path) -> tuple[Path, Path]:
    queue, jobs = runs / "_queue", runs / "_jobs"
    queue.mkdir(parents=True, exist_ok=True)
    jobs.mkdir(parents=True, exist_ok=True)
    return queue, jobs


def validate(job: dict) -> dict:
    """The fields a job must carry. Raises JobError; never guesses."""
    if job.get("action") not in ACTIONS:
        raise JobError(f"unknown action {job.get('action')!r}")
    slug = str(job.get("slug") or "")
    if not SLUG_RE.match(slug):
        raise JobError(f"bad slug {slug!r}")
    text = str(job.get("text") or "").strip()
    if not text:
        raise JobError("empty text")
    if len(text) > MAX_TEXT:
        raise JobError("text too long")
    if not re.match(r"^[A-Za-z0-9_-]{6,64}$", str(job.get("id") or "")):
        raise JobError("bad id")
    return {**job, "slug": slug, "text": text}


def prompt_for(job: dict) -> str:
    # Newlines would end the slash command's argument line early; the request is one line.
    text = " ".join(job["text"].split())
    if job["action"] == "start":
        return f"/arc2 --slug {job['slug']} {text}"
    return f"/arc2 --resume {job['slug']} {text}"


def command_for(job: dict, claude: str, model: str | None = None) -> list[str]:
    cmd = [
        claude, "-p", prompt_for(job),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "dontAsk",
        "--allowedTools", *ALLOWED_TOOLS,
        "--append-system-prompt", RUNNER_GUIDANCE,
    ]
    return cmd + (["--model", model] if model else [])


# ── Local fallback ──────────────────────────────────────────────────────
# When Claude cannot be used at all (signed out, usage limit, overloaded, unreachable),
# the same /arc2 step is re-run through Claude Code pointed at a local Ollama, which
# serves the Anthropic Messages API. Same agents, same contract, a local model.
# ARC2_FALLBACK=off disables it; ARC2_FALLBACK_URL / ARC2_FALLBACK_MODEL choose where.

FALLBACK_ERRORS = (
    "failed to authenticate", "oauth", "not logged in", "invalid api key", "invalid x-api-key",
    "usage limit", "rate limit", "rate_limit", "overloaded", "credit balance",
    "could not connect", "connection error", "api error: 5", "service unavailable",
)


class Fallback:
    def __init__(self, url: str, model: str):
        self.url, self.model = url.rstrip("/"), model

    @classmethod
    def from_env(cls) -> Fallback | None:
        if os.environ.get("ARC2_FALLBACK", "on").lower() in ("off", "0", "false", "no"):
            return None
        return cls(os.environ.get("ARC2_FALLBACK_URL", "http://127.0.0.1:11434"),
                   os.environ.get("ARC2_FALLBACK_MODEL", "qwen3.6:35b-a3b"))

    @property
    def label(self) -> str:
        return f"ollama:{self.model}"

    def reachable(self, timeout: float = 3.0) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=timeout) as resp:
                names = [m.get("name") for m in json.loads(resp.read()).get("models", [])]
        except (OSError, ValueError):
            return False
        return self.model in names

    def env(self, base: dict) -> dict:
        env = {k: v for k, v in base.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}
        env.update({
            "ANTHROPIC_BASE_URL": self.url,
            "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": self.model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.model,
            "CLAUDE_CODE_SUBAGENT_MODEL": self.model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        return env


def should_fall_back(record: dict) -> bool:
    """Only when Claude itself was unavailable; never for a STOP, a merge refusal or a timeout."""
    error = str(record.get("error") or "").lower()
    return record.get("state") == "failed" and any(marker in error for marker in FALLBACK_ERRORS)


def claim(queue: Path, jobs: Path) -> tuple[dict, Path] | None:
    """Move the oldest queued job into _jobs as running. Invalid jobs are recorded as failed."""
    for path in sorted(queue.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raw = {"id": path.stem[-12:] or "unknown", "action": "?", "slug": "?", "text": ""}
        target = jobs / f"{raw.get('id') or path.stem}.json"
        try:
            path.rename(target.with_suffix(".claiming"))
        except OSError:
            continue  # another runner took it
        record = {**raw, "state": "running", "started_at": now(), "log": target.with_suffix(".log").name,
                  "current_agent": None, "result": None, "error": None}
        try:
            record = {**validate(raw), **{k: v for k, v in record.items() if k not in raw}}
        except JobError as exc:
            record.update(state="failed", error=str(exc), finished_at=now())
            write_record(target, record)
            target.with_suffix(".claiming").unlink(missing_ok=True)
            continue
        write_record(target, record)
        target.with_suffix(".claiming").unlink(missing_ok=True)
        return record, target
    return None


def write_record(path: Path, record: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    tmp.replace(path)


def agent_from_event(event: dict) -> str | None:
    """The ARC² agent a stream-json event launches, if any."""
    if event.get("type") != "assistant":
        return None
    for part in (event.get("message") or {}).get("content") or []:
        if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") in ("Task", "Agent"):
            sub = (part.get("input") or {}).get("subagent_type")
            if sub in AGENT_NAMES:
                return AGENT_NAMES[sub]
    return None


def run_job(record: dict, path: Path, claude: str, timeout: int = DEFAULT_TIMEOUT,
            fallback: Fallback | None = None) -> dict:
    """Run one job on Claude; if Claude is unavailable, run it again on the local fallback."""
    env = {k: v for k, v in os.environ.items() if k not in ("AUTH_DISABLED", "DATABASE_URL")}
    env["PYTHONPATH"] = "tools"
    record["engine"] = "claude"
    record = _attempt(record, path, command_for(record, claude), env, timeout, append=False)
    if fallback and should_fall_back(record) and fallback.reachable():
        record.update(fallback_from=record["error"], engine=fallback.label, state="running", error=None,
                      result=None, exit_code=None, finished_at=None, current_agent=None)
        write_record(path, record)
        record = _attempt(record, path, command_for(record, claude, fallback.model), fallback.env(env), timeout,
                          append=True)
    return record


def _attempt(record: dict, path: Path, cmd: list[str], env: dict, timeout: int, append: bool) -> dict:
    """One headless run of /arc2, streaming progress into the job record."""
    log_path = path.with_suffix(".log")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    except OSError as exc:
        record.update(state="failed", error=f"could not start claude: {exc}", finished_at=now())
        write_record(path, record)
        return record
    last_write = 0.0
    with log_path.open("a" if append else "w") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            agent = agent_from_event(event)
            if agent:
                record["current_agent"] = agent
            if event.get("type") == "result":
                record["result"] = str(event.get("result") or "")[-4000:]
                record["cost_usd"] = event.get("total_cost_usd")
                record["turns"] = event.get("num_turns")
                if event.get("is_error"):
                    record["error"] = record["result"][:500] or "claude reported an error"
            if agent or time.monotonic() - last_write > 5:
                write_record(path, record)
                last_write = time.monotonic()
            if time.monotonic() - started > timeout:
                os.killpg(proc.pid, signal.SIGTERM)
                record["error"] = f"timed out after {timeout // 60} min"
                break
    try:
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        code = proc.wait()
    record.update(exit_code=code, finished_at=now(), current_agent=None)
    record["state"] = "done" if code == 0 and not record.get("error") else "failed"
    if record["state"] == "failed" and not record.get("error"):
        record["error"] = f"claude exited with {code}"
    write_record(path, record)
    return record


# ── Per-course history ──────────────────────────────────────────────────
# Every job ends with a commit in the course's own git repository (<runs>/<slug>/.git,
# separate from the TrueNorth repo, which /arc2 never commits to). The commit holds the
# whole run folder as that step left it, plus the step's full transcript under
# _transcripts/, so any two rounds can be compared and a bad rework rolled back.
# The engine only reads its stage folders (01-07), so neither .git nor _transcripts/
# affects its checks or digests.

def _git(run: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(run), *args], capture_output=True, text=True, timeout=60)


def snapshot(runs: Path, record: dict, log_path: Path) -> str | None:
    """Commit the run folder after a job. Returns the commit id, or None if there was nothing to record."""
    run = runs / record["slug"]
    if not run.is_dir():
        return None  # the step failed before /arc2 created the run
    stamp = (record.get("started_at") or now()).replace(":", "").replace("-", "").replace(".", "")[:15]
    transcripts = run / "_transcripts"
    transcripts.mkdir(exist_ok=True)
    if log_path.is_file():
        shutil.copyfile(log_path, transcripts / f"{stamp}-{record['id']}.jsonl")
    if not (run / ".git").is_dir():
        # Checked on the folder itself: without a .git here, git -C would find the TrueNorth repo.
        if _git(run, "init", "-q").returncode != 0:
            return None
        _git(run, "config", "user.name", "ARC2 runner")
        _git(run, "config", "user.email", "arc2-runner@localhost")
    _git(run, "add", "-A")
    if _git(run, "diff", "--cached", "--quiet").returncode == 0:
        return None
    what = "start" if record["action"] == "start" else f"resume: {' '.join(record['text'].split())[:60]}"
    message = (f"{what} · {record.get('state')} on {record.get('engine')}\n\n"
               f"Job: {record['id']}\nRequested-by: {record.get('requested_by')}\n"
               + (f"Error: {record['error'][:200]}\n" if record.get("error") else ""))
    if _git(run, "commit", "-q", "-m", message).returncode != 0:
        return None
    return _git(run, "rev-parse", "--short", "HEAD").stdout.strip() or None


def reap(jobs: Path) -> None:
    """Jobs left 'running' by a runner that stopped are failed, not silently resumed."""
    for path in jobs.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("state") == "running":
            record.update(state="failed", error="the runner stopped while this job was running", finished_at=now())
            write_record(path, record)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arc2.runner", description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=Path, default=Path(os.environ.get("ARC2_RUNS_DIR", REPO_ROOT / "build" / "arc2")))
    ap.add_argument("--claude", default=os.environ.get("ARC2_CLAUDE", "claude"))
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--once", action="store_true", help="run whatever is queued, then exit")
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args(argv)
    claude = shutil.which(args.claude) or args.claude
    fallback = Fallback.from_env()
    queue, jobs = dirs(args.runs)
    reap(jobs)
    print(f"arc2 runner: watching {queue} (claude: {claude}; fallback: "
          f"{fallback.label + ' at ' + fallback.url if fallback else 'off'})", flush=True)
    while True:
        claimed = claim(queue, jobs)
        if claimed:
            record, path = claimed
            print(f"{now()} {record['action']} {record['slug']}: running", flush=True)
            record = run_job(record, path, claude, args.timeout, fallback)
            commit = snapshot(args.runs, record, path.with_suffix(".log"))
            if commit:
                record["history_commit"] = commit
                write_record(path, record)
            print(f"{now()} {record['action']} {record['slug']}: {record['state']} on {record.get('engine')}"
                  + (f" ({record['error']})" if record.get("error") else ""), flush=True)
            continue
        if args.once:
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
