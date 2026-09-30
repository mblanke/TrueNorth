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
                                          "slug", "text", "created_at", "requested_by"}

* ``start``: ``text`` is the course request; runs ``/arc2 --slug <slug> <text>``.
* ``resume``: ``text`` is ``accept`` or feedback; runs ``/arc2 --resume <slug> <text>``.

Job records (the runner writes, the API reads):

    <runs>/_jobs/<id>.json   the queued fields plus state (running|done|failed),
                             started_at, finished_at, exit_code, current_agent,
                             result, error, cost_usd, turns, log
    <runs>/_jobs/<id>.log    the raw stream-json output

Nothing here commits, provisions or imports; ``/arc2``'s own hard rules apply. Tools are
limited to reading and writing files and the few commands ``/arc2`` needs.

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

# What a headless /arc2 may use. File tools cover the run directory; Bash is limited to
# the commands arc2.md runs. Anything else is refused non-interactively.
ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep", "Task", "TodoWrite",
    "Bash(.venv/bin/python:*)",
    "Bash(PYTHONPATH=tools .venv/bin/python:*)",
    "Bash(git status:*)", "Bash(git rev-parse:*)",
    "Bash(mkdir:*)", "Bash(ls:*)", "Bash(head:*)", "Bash(cat:*)", "Bash(wc:*)",
]

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
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def command_for(job: dict, claude: str) -> list[str]:
    return [
        claude, "-p", prompt_for(job),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", *ALLOWED_TOOLS,
    ]


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


def run_job(record: dict, path: Path, claude: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run one job to completion, streaming progress into its record."""
    log_path = path.with_suffix(".log")
    cmd = command_for(record, claude)
    env = {k: v for k, v in os.environ.items() if k not in ("AUTH_DISABLED", "DATABASE_URL")}
    started = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    except OSError as exc:
        record.update(state="failed", error=f"could not start claude: {exc}", finished_at=now())
        write_record(path, record)
        return record
    last_write = 0.0
    with log_path.open("w") as log:
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
    queue, jobs = dirs(args.runs)
    reap(jobs)
    print(f"arc2 runner: watching {queue} (claude: {claude})", flush=True)
    while True:
        claimed = claim(queue, jobs)
        if claimed:
            record, path = claimed
            print(f"{now()} {record['action']} {record['slug']}: running", flush=True)
            record = run_job(record, path, claude, args.timeout)
            print(f"{now()} {record['action']} {record['slug']}: {record['state']}"
                  + (f" ({record['error']})" if record.get("error") else ""), flush=True)
            continue
        if args.once:
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
