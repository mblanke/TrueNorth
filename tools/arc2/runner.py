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

Nothing here commits, provisions or imports; ``/arc2``'s own hard rules apply. Each job
runs in an OS sandbox that confines it to its own run (``arc2/confine.py``, chosen by
``ARC2_CONFINE``). Without one the runner does not start. Tool rules additionally limit
edits to the job's run and Bash to the few commands ``/arc2`` needs. If Claude cannot be used (signed out, usage limit, overloaded), the same step is
re-run on a local Ollama through its Anthropic-compatible API (see ``Fallback``); the job
record's ``engine`` says which ran.

Usage: ``PYTHONPATH=tools .venv/bin/python -m arc2.runner [--once]``
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from arc2.confine import Confinement, ConfinementError, Jail, Unconfined, select
from arc2.egress import EgressProxy, LocalForward

REPO_ROOT = Path(__file__).resolve().parents[2]
SLUG_RE = re.compile(r"^arc2-[a-z0-9-]{1,60}$")
# The job's text follows its own slug on /arc2's argument line; --slug or --resume in it
# would point the engine at another run (the API refuses these too).
RUN_FLAG_RE = re.compile(r"(?i)(?:^|\s)--(?:slug|resume)\b")
ACTIONS = {"start", "resume"}
MAX_TEXT = 4000
DEFAULT_TIMEOUT = 4 * 3600
# After the deadline (or once the engine has exited) its process group gets SIGTERM, then
# SIGKILL after this many seconds. A job therefore ends at most SHUTDOWN_GRACE seconds
# (plus scheduling) after its deadline, whatever the engine does.
SHUTDOWN_GRACE = 10
PROGRESS_TICK = 5.0  # seconds between job-record writes while output is flowing or not

# What a headless /arc2 may use. Bash is limited to the commands arc2.md runs. With
# --permission-mode dontAsk, anything not listed is refused, not prompted. These rules
# match command text and are not the security boundary (.venv/bin/python runs anything):
# the job's OS sandbox is (arc2/confine.py). File edits are added per job, for the job's
# own run only (job_tools).
ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Task",
    "TodoWrite",
    "Bash(.venv/bin/python:*)",
    "Bash(PYTHONPATH=tools .venv/bin/python:*)",
    "Bash(git status:*)",
    "Bash(git rev-parse:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(mkdir:*)",
    "Bash(ls:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(cat:*)",
    "Bash(wc:*)",
    "Bash(echo:*)",
    "Bash(printf:*)",
    "Bash(grep:*)",
    "Bash(sort:*)",
    "Bash(diff:*)",
    "Bash(stat:*)",
    "Bash(test:*)",
    "Bash(true)",
    "Bash(shasum:*)",
    "Bash(command -v:*)",
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
    "File edits are allowed only in this run's directory, build/arc2/<slug>/, and its "
    "request file build/arc2/<slug>.request.txt; other runs and build/arc2/_* are out of reach."
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
    if RUN_FLAG_RE.search(text):
        raise JobError("text may not contain --slug or --resume")
    if not re.match(r"^[A-Za-z0-9_-]{6,64}$", str(job.get("id") or "")):
        raise JobError("bad id")
    return {**job, "slug": slug, "text": text}


def prompt_for(job: dict) -> str:
    # Newlines would end the slash command's argument line early; the request is one line.
    text = " ".join(job["text"].split())
    if job["action"] == "start":
        return f"/arc2 --slug {job['slug']} {text}"
    return f"/arc2 --resume {job['slug']} {text}"


def job_tools(slug: str) -> list[str]:
    """ALLOWED_TOOLS plus file edits for this job's own run and request file."""
    own = [f"./build/arc2/{slug}/**", f"./build/arc2/{slug}.request.txt"]
    return [*ALLOWED_TOOLS, *(f"{tool}({path})" for tool in ("Write", "Edit") for path in own)]


def command_for(job: dict, claude: str, model: str | None = None) -> list[str]:
    """The engine's command line. The prompt (a tenant's request or feedback) is not on
    it: it goes on stdin (``prompt_for``), because any process of the same account can
    read another's arguments, and a sandbox cannot stop that on macOS."""
    cmd = [
        claude,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        *job_tools(job["slug"]),
        # No user-level settings, hooks or MCP servers: only this repository's project settings.
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--append-system-prompt",
        RUNNER_GUIDANCE,
    ]
    return cmd + (["--model", model] if model else [])


# ── Local fallback ──────────────────────────────────────────────────────
# When Claude cannot be used at all (signed out, usage limit, overloaded, unreachable),
# the same /arc2 step is re-run through Claude Code pointed at a local Ollama, which
# serves the Anthropic Messages API. Same agents, same contract, a local model.
# ARC2_FALLBACK=off disables it; ARC2_FALLBACK_URL / ARC2_FALLBACK_MODEL choose where.

FALLBACK_ERRORS = (
    "failed to authenticate",
    "oauth",
    "not logged in",
    "invalid api key",
    "invalid x-api-key",
    "usage limit",
    "rate limit",
    "rate_limit",
    "overloaded",
    "credit balance",
    "could not connect",
    "connection error",
    "api error: 5",
    "service unavailable",
)


class Fallback:
    def __init__(self, url: str, model: str):
        self.url, self.model = url.rstrip("/"), model

    @classmethod
    def from_env(cls) -> Fallback | None:
        if os.environ.get("ARC2_FALLBACK", "on").lower() in ("off", "0", "false", "no"):
            return None
        return cls(
            os.environ.get("ARC2_FALLBACK_URL", "http://127.0.0.1:11434"),
            os.environ.get("ARC2_FALLBACK_MODEL", "qwen3.6:35b-a3b"),
        )

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
        env.update(
            {
                "ANTHROPIC_BASE_URL": self.url,
                "ANTHROPIC_AUTH_TOKEN": "ollama",
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_MODEL": self.model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.model,
                "CLAUDE_CODE_SUBAGENT_MODEL": self.model,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        return env


def should_fall_back(record: dict) -> bool:
    """Only when Claude itself was unavailable; never for a STOP, a merge refusal or a timeout."""
    error = str(record.get("error") or "").lower()
    return record.get("state") == "failed" and any(marker in error for marker in FALLBACK_ERRORS)


def claim(queue: Path, jobs: Path, owner: dict | None = None) -> tuple[dict, Path] | None:
    """Move the oldest queued job into _jobs as running, recording ``owner`` (the runner).
    Invalid jobs are recorded as failed."""
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
        record = {
            **raw,
            "state": "running",
            "started_at": now(),
            "log": target.with_suffix(".log").name,
            "current_agent": None,
            "result": None,
            "error": None,
            "runner": owner,
        }
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


# Environment a confined job gets: these names from the runner's environment (plus the
# names in ARC2_JOB_ENV), never the rest, which may hold the runner's own secrets.
JOB_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "SHELL", "TZ")
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def job_homes() -> Path:
    return Path(os.environ.get("ARC2_JOB_HOMES") or Path.home() / ".arc2" / "jobs")


def token_file() -> Path:
    return Path(os.environ.get("ARC2_OAUTH_TOKEN_FILE") or Path.home() / ".arc2" / "oauth-token")


def job_env(home: Path) -> dict:
    """The confined job's environment: a fresh HOME and Claude config, and an allow-list."""
    names = (*JOB_ENV, *AUTH_ENV, *filter(None, os.environ.get("ARC2_JOB_ENV", "").split(",")))
    env = {k: os.environ[k] for k in names if k in os.environ}
    if not any(env.get(k) for k in AUTH_ENV):
        with contextlib.suppress(OSError):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token_file().read_text().strip()
    env.update(
        HOME=str(home),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        TMPDIR=str(home / "tmp"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        PYTHONPATH="tools",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def _claude_install(claude: str) -> tuple[Path, ...]:
    found = shutil.which(claude) or claude
    paths = {Path(found).parent, Path(os.path.realpath(found)).parent}
    return tuple(sorted(paths))


def _local_ports(fallback: Fallback | None) -> tuple[int, ...]:
    if not fallback:
        return ()
    from urllib.parse import urlparse

    url = urlparse(fallback.url)
    if url.hostname in ("localhost", "127.0.0.1", "::1"):
        return (url.port or (443 if url.scheme == "https" else 80),)
    return ()


def run_job(
    record: dict,
    path: Path,
    claude: str,
    timeout: int = DEFAULT_TIMEOUT,
    fallback: Fallback | None = None,
    confinement: Confinement | None = None,
    runs: Path | None = None,
    egress: EgressProxy | None = None,
) -> dict:
    """Run one job on Claude; if Claude is unavailable, run it again on the local fallback.

    Both attempts run inside ``confinement`` (arc2/confine.py). Confined, a job gets a
    fresh home of its own (Claude config, sessions, temp), deleted afterwards, and an
    allow-listed environment. ``none`` keeps the runner's own environment and config.
    Callers other than ``main`` (tests) may omit ``confinement`` to run unconfined.
    """
    confinement = confinement or Unconfined()
    runs = runs or path.parent.parent
    record["engine"] = "claude"
    record["confinement"] = confinement.name
    if isinstance(confinement, Unconfined):
        env = {k: v for k, v in os.environ.items() if k not in ("AUTH_DISABLED", "DATABASE_URL")}
        env.update(PYTHONPATH="tools", PYTHONDONTWRITEBYTECODE="1")
        home = None
    else:
        home = job_homes() / record["id"]
        shutil.rmtree(home, ignore_errors=True)
        for sub in (".claude", "tmp", ".config", ".cache"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        env = job_env(home)
        if egress:
            env.update(egress.env(), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
            record["egress"] = list(egress.allow)
    jail = Jail(
        repo=REPO_ROOT,
        runs=runs,
        home=Path.home(),
        writable=tuple(p for p in (runs / record["slug"], home) if p is not None),
        writable_files=(runs / f"{record['slug']}.request.txt",),
        readable=_claude_install(claude),
        local_ports=_local_ports(fallback),
        egress_port=egress.port if egress and not isinstance(confinement, Unconfined) else None,
        bridges=tuple(getattr(egress, "bridges", ())) if egress else (),
    )
    try:
        record = _attempt(
            record,
            path,
            confinement.wrap(command_for(record, claude), jail),
            env,
            timeout,
            append=False,
            stdin_text=prompt_for(record),
        )
        if fallback and should_fall_back(record) and fallback.reachable():
            record.update(
                fallback_from=record["error"],
                engine=fallback.label,
                state="running",
                error=None,
                result=None,
                exit_code=None,
                finished_at=None,
                current_agent=None,
            )
            write_record(path, record)
            record = _attempt(
                record,
                path,
                confinement.wrap(command_for(record, claude, fallback.model), jail),
                fallback.env(env),
                timeout,
                append=True,
                stdin_text=prompt_for(record),
            )
    finally:
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)
    return record


def _attempt(
    record: dict, path: Path, cmd: list[str], env: dict, timeout: int, append: bool, stdin_text: str | None = None
) -> dict:
    """One headless run of /arc2, streaming progress into the job record.

    The deadline is enforced by the clock, not by the engine's output: a reader thread
    moves output lines onto a queue and this loop wakes at least every PROGRESS_TICK
    seconds, so a silent engine, half a line with no newline, or an engine that exits
    while a descendant keeps its output open cannot hold the runner. The engine runs in
    its own session; when the job ends, its whole process group is stopped (see
    ``_stop_group``). A descendant that calls setsid() itself leaves the group and is
    out of reach of this; /arc2's tools do not.
    """
    log_path = path.with_suffix(".log")
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        record.update(state="failed", error=f"could not start claude: {exc}", finished_at=now())
        write_record(path, record)
        return record
    if stdin_text is not None and proc.stdin is not None:
        with contextlib.suppress(OSError):  # the prompt is small (MAX_TEXT); an engine that exits early may not read it
            proc.stdin.write(stdin_text)
        with contextlib.suppress(OSError):
            proc.stdin.close()
    assert proc.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(target=_read_lines, args=(proc.stdout, lines), daemon=True)
    reader.start()
    timed_out = False
    last_write = 0.0
    with log_path.open("a" if append else "w") as log:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = lines.get(timeout=min(remaining, PROGRESS_TICK))
            except queue.Empty:
                if proc.poll() is not None:
                    break  # the engine is gone; a descendant may still hold its output open
                continue
            if line is None:
                break  # end of output
            if _consume(record, line, log) or time.monotonic() - last_write > PROGRESS_TICK:
                write_record(path, record)
                last_write = time.monotonic()
        if not timed_out:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True  # output closed, but the engine kept running past the deadline
        code = _stop_group(proc, SHUTDOWN_GRACE)
        reader.join(timeout=2)
        while True:  # whatever was printed before the group stopped
            try:
                line = lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                _consume(record, line, log)
    if timed_out:
        record["error"] = f"timed out after {_duration(timeout)}"
    record.update(exit_code=code, finished_at=now(), current_agent=None)
    record["state"] = "done" if code == 0 and not record.get("error") else "failed"
    if record["state"] == "failed" and not record.get("error"):
        record["error"] = f"claude exited with {code}"
    write_record(path, record)
    return record


def _read_lines(stream, lines: queue.Queue) -> None:
    """Reader thread: every output line onto ``lines``, then None at end of output."""
    try:
        for line in stream:
            lines.put(line)
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def _consume(record: dict, line: str, log) -> bool:
    """Log one output line and fold it into the record. True when the record should be written now."""
    log.write(line)
    log.flush()
    try:
        event = json.loads(line)
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    agent = agent_from_event(event)
    if agent:
        record["current_agent"] = agent
    if event.get("type") == "result":
        record["result"] = str(event.get("result") or "")[-4000:]
        record["cost_usd"] = event.get("total_cost_usd")
        record["turns"] = event.get("num_turns")
        if event.get("is_error"):
            record["error"] = record["result"][:500] or "claude reported an error"
    return bool(agent)


def _stop_group(proc: subprocess.Popen, grace: float) -> int:
    """Stop the engine's process group and reap the engine. Returns its exit status.

    SIGTERM to the group, up to ``grace`` seconds for the engine to exit, then SIGKILL to
    the group regardless, so descendants that outlive the engine (or ignore SIGTERM) go
    too. Bounded: returns within ``grace`` seconds plus the time SIGKILL takes. The group
    id is the engine's pid; once the engine is reaped the group lives only while a member
    does, so a signal can reach only this job's leftovers.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    _signal_group(proc.pid, signal.SIGKILL)
    return proc.wait()


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # nothing left in the group
        os.killpg(pgid, sig)


def _duration(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 120 else f"{seconds} s"


# ── Per-course history ──────────────────────────────────────────────────
# Every job ends with a commit in the course's own history, separate from the TrueNorth
# repo, which /arc2 never commits to. The git directory is runner-owned,
# <runs>/_history/<slug>.git, with the run folder as its work tree, so a job cannot plant
# hooks or config in it. Each step's transcript is kept beside it in
# <runs>/_history/<slug>.transcripts/. Git runs with hooks, fsmonitor and global/system
# config disabled, and inside the job's confinement (reading the run, writing only the
# history), so a link or file the job left in its run cannot make it touch anything else.
# A run folder that is itself a link is not recorded. Any two rounds can be compared, and
# a bad rework rolled back.

GIT_SAFE = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.symlinks=true",
    "-c",
    "user.name=ARC2 runner",
    "-c",
    "user.email=arc2-runner@localhost",
]


def _git(
    git_dir: Path, work: Path, *args: str, confinement: Confinement | None = None, jail: Jail | None = None
) -> subprocess.CompletedProcess:
    cmd = ["git", f"--git-dir={git_dir}", f"--work-tree={work}", *GIT_SAFE, *args]
    if confinement and jail:
        cmd = confinement.wrap(cmd, jail)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", HOME=str(git_dir))
    return subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=60)


def snapshot(runs: Path, record: dict, log_path: Path, confinement: Confinement | None = None) -> str | None:
    """Commit the run folder after a job. Returns the commit id, or None if there was nothing to record."""
    slug = record["slug"]
    run = runs / slug
    if run.is_symlink() or not run.is_dir() or run.resolve() != runs.resolve() / slug:
        return None  # never created, or replaced by a link: nothing trustworthy to record
    history = runs / "_history"
    git_dir, transcripts = history / f"{slug}.git", history / f"{slug}.transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    stamp = (record.get("started_at") or now()).replace(":", "").replace("-", "").replace(".", "")[:15]
    if log_path.is_file():
        shutil.copyfile(log_path, transcripts / f"{stamp}-{record['id']}.jsonl")
    jail = Jail(repo=REPO_ROOT, runs=runs, home=Path.home(), writable=(git_dir,), readable_inner=(run,))
    confinement = confinement or Unconfined()
    if (
        not git_dir.is_dir()
        and subprocess.run(
            ["git", "init", "-q", "--bare", str(git_dir)],
            capture_output=True,
            timeout=60,
            env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        ).returncode
        != 0
    ):
        return None

    def git(*args: str) -> subprocess.CompletedProcess:
        return _git(git_dir, run, *args, confinement=confinement, jail=jail)

    git("add", "-A", "--", ".", ":(exclude).git", ":(exclude)_transcripts")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return None
    what = "start" if record["action"] == "start" else f"resume: {' '.join(record['text'].split())[:60]}"
    message = (
        f"{what} · {record.get('state')} on {record.get('engine')}\n\n"
        f"Job: {record['id']}\nRequested-by: {record.get('requested_by')}\n"
        + (f"Error: {record['error'][:200]}\n" if record.get("error") else "")
    )
    if git("commit", "-q", "-m", message).returncode != 0:
        return None
    return git("rev-parse", "--short", "HEAD").stdout.strip() or None


# ── One runner per runs root, and crash recovery ───────────────────────
# The runner is a single host process. Recovery after a restart fails jobs left
# "running", which is only safe if no other runner is working on them, so a runner holds
# an exclusive flock on <runs>/_runner.lock for its whole life. A second runner exits
# instead of recovering anything; the kernel releases the lock however the holder dies.
# Jobs are not run twice: a job is only re-queued if it was never recorded as started.


def acquire_lock(runs: Path):
    """The open, exclusively locked runs-root lock file, or None if another runner holds it."""
    handle = (runs / "_runner.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def recover(queue: Path, jobs: Path) -> None:
    """After a crash, with the lock held: put interrupted claims back on the queue (the job
    never started) and fail jobs left running (they did start; resuming blind is unsafe)."""
    for path in jobs.glob("*.claiming"):
        job = None
        with contextlib.suppress(OSError, ValueError):
            job = json.loads(path.read_text())
        if not isinstance(job, dict) or not job.get("id"):
            path.unlink(missing_ok=True)
            continue
        stamp = str(job.get("created_at") or now()).replace(":", "").replace("-", "").replace(".", "")[:15]
        path.rename(queue / f"{stamp}-{job['id']}.json")
    reap(jobs)


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
    try:
        confinement = select()
    except ConfinementError as exc:  # fail closed: no job runs without its sandbox
        print(f"arc2 runner: not starting: {exc}", file=sys.stderr, flush=True)
        return 2
    fallback = Fallback.from_env()
    if confinement.name != "none" and not any(os.environ.get(k) for k in AUTH_ENV) and not token_file().is_file():
        print(
            f"arc2 runner: warning: no CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY or {token_file()}; "
            "confined jobs start with an empty Claude config and cannot sign in",
            file=sys.stderr,
            flush=True,
        )
    queue, jobs = dirs(args.runs)
    lock = acquire_lock(args.runs)
    if lock is None:
        print(
            f"arc2 runner: not starting: another runner holds {args.runs / '_runner.lock'}", file=sys.stderr, flush=True
        )
        return 3
    owner = {"pid": os.getpid(), "host": os.uname().nodename, "started_at": now()}
    # Confined jobs reach the internet only through this allow-listing proxy (arc2/egress.py).
    # ARC2_EGRESS=open leaves egress unrestricted (the sandbox still applies).
    egress = None
    forwards: list[LocalForward] = []
    if confinement.name != "none" and os.environ.get("ARC2_EGRESS", "proxy").lower() != "open":
        if confinement.name == "bubblewrap":
            # Linux jobs get a network namespace of their own; the proxy and the local
            # fallback reach them as Unix sockets (arc2/netbridge.py) in a private directory.
            import tempfile

            sockets = Path(tempfile.mkdtemp(prefix="arc2-net-"))
            egress = EgressProxy(unix_path=str(sockets / "egress.sock")).start()
            egress.bridges = [(egress.port, sockets / "egress.sock")]
            for port in _local_ports(fallback):
                forwards.append(LocalForward(str(sockets / f"local-{port}.sock"), port))
                egress.bridges.append((port, sockets / f"local-{port}.sock"))
        else:
            egress = EgressProxy().start()
    recover(queue, jobs)
    print(
        f"arc2 runner: watching {queue} (claude: {claude}; fallback: "
        f"{fallback.label + ' at ' + fallback.url if fallback else 'off'}; confinement: {confinement.name})",
        flush=True,
    )
    try:
        while True:
            claimed = claim(queue, jobs, owner)
            if claimed:
                record, path = claimed
                print(f"{now()} {record['action']} {record['slug']}: running", flush=True)
                record = run_job(record, path, claude, args.timeout, fallback, confinement, args.runs, egress)
                commit = snapshot(args.runs, record, path.with_suffix(".log"), confinement)
                if commit:
                    record["history_commit"] = commit
                    write_record(path, record)
                print(
                    f"{now()} {record['action']} {record['slug']}: {record['state']} on {record.get('engine')}"
                    + (f" ({record['error']})" if record.get("error") else ""),
                    flush=True,
                )
                continue
            if args.once:
                return 0
            time.sleep(args.poll)
    finally:
        lock.close()  # releases the flock
        if egress:
            egress.close()
        for forward in forwards:
            forward.close()


# The runner's own environment, as the kernel recorded it at exec. A confined job cannot
# read the runner's memory, but on macOS it can read any same-account process's original
# arguments and environment (sysctl KERN_PROCARGS2; Seatbelt has no rule for it). So the
# runner re-executes itself once with only what it needs; anything else it was started
# with (DATABASE_URL, cloud keys, ...) is gone before the first job runs.
RUNNER_ENV = (*JOB_ENV, "HOME", "TMPDIR", "PYTHONPATH", "VIRTUAL_ENV", *AUTH_ENV)


def clean_runner_env(environ: dict) -> dict:
    """What the runner keeps: its own settings (ARC2_*), what jobs need (RUNNER_ENV), and
    the names the operator forwards to jobs (ARC2_JOB_ENV). Nothing else."""
    forwarded = set(filter(None, environ.get("ARC2_JOB_ENV", "").split(",")))
    env = {k: v for k, v in environ.items() if k in RUNNER_ENV or k in forwarded or k.startswith("ARC2_")}
    env["ARC2_RUNNER_CLEAN"] = "1"
    return env


def _reexec_clean() -> None:
    if os.environ.get("ARC2_RUNNER_CLEAN") == "1" or os.environ.get("ARC2_CONFINE", "auto").lower() == "none":
        return
    os.execve(sys.executable, [sys.executable, "-m", "arc2.runner", *sys.argv[1:]], clean_runner_env(dict(os.environ)))


if __name__ == "__main__":
    _reexec_clean()
    sys.exit(main())
