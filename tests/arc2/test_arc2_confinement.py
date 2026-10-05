"""A Course Studio job can touch only its own run.

The engine runs untrusted request and feedback text, and its tools include arbitrary
Python, so Claude Code's tool permissions are not a boundary. The runner puts the whole
engine process tree in an OS sandbox (tools/arc2/confine.py). While it runs, a job may
read and write its own run directory and its request file. It may not write to other
runs, to the Studio's metadata (ownership lives there), to the queue, to the job
records or to the repository, and it may not read any of those. Without a sandbox the
runner does not start, unless an operator opts out explicitly.

The end-to-end cases run a fake ``claude`` that tries each escape. They need macOS
(Seatbelt) and are skipped elsewhere; the profile and fail-closed cases run everywhere.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

import pytest
from arc2 import confine, runner

SLUG = "arc2-mine"

PROBE = r"""#!/usr/bin/env python3
import json, os, sys
runs, repo, probe = os.environ["PROBE_RUNS"], os.environ["PROBE_REPO"], os.environ["PROBE_NAME"]
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
attempt("write_own_run", write(f"{runs}/arc2-mine/own.txt"))
attempt("write_request_file", write(f"{runs}/arc2-mine.request.txt"))
attempt("write_other_run", write(f"{runs}/arc2-other/stolen.txt"))
attempt("write_studio_owner", write(f"{runs}/_studio/arc2-other.json"))
attempt("write_queue", write(f"{runs}/_queue/forged.json"))
attempt("write_repo", write(f"{repo}/tools/arc2/{probe}"))
attempt("read_other_run", read(f"{runs}/arc2-other/secret.md"))
attempt("read_studio", read(f"{runs}/_studio/arc2-other.json"))
attempt("read_job_records", read(f"{runs}/_jobs/job000001.json"))
attempt("read_via_symlink", read(f"{runs}/arc2-mine/link"))
attempt("read_repo", read(f"{repo}/tools/arc2/check.py"))
open(f"{runs}/arc2-mine/probe.json", "w").write(json.dumps(results))
print(json.dumps({"type": "result", "is_error": False, "result": "probed"}), flush=True)
"""

needs_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="Seatbelt (sandbox-exec) is macOS only"
)


@pytest.fixture
def runs(tmp_path) -> Path:
    root = (tmp_path / "runs").resolve()
    (root / SLUG).mkdir(parents=True)
    (root / "arc2-other").mkdir()
    (root / "arc2-other" / "secret.md").write_text("another tenant's answer key")
    (root / "_studio").mkdir()
    (root / "_studio" / "arc2-other.json").write_text(json.dumps({"tenant_id": "other"}))
    (root / SLUG / "link").symlink_to(root / "arc2-other" / "secret.md")
    return root


@pytest.fixture
def probe_engine(tmp_path, monkeypatch, runs):
    exe = tmp_path / "claude"
    exe.write_text(PROBE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    name = f"_probe_{tmp_path.name}"
    monkeypatch.setenv("PROBE_RUNS", str(runs))
    monkeypatch.setenv("PROBE_REPO", str(runner.REPO_ROOT))
    monkeypatch.setenv("PROBE_NAME", name)
    monkeypatch.setenv("ARC2_FALLBACK", "off")
    yield exe
    (runner.REPO_ROOT / "tools" / "arc2" / name).unlink(missing_ok=True)


def queue_job(runs: Path) -> None:
    queue, _ = runner.dirs(runs)
    job = {"id": "job000001", "action": "start", "slug": SLUG, "text": "a course", "created_at": "2026-10-04T12:00:00Z"}
    (queue / "20261004T120000-job000001.json").write_text(json.dumps(job))


@needs_seatbelt
def test_a_confined_job_reaches_only_its_own_run(runs, probe_engine, monkeypatch):
    monkeypatch.setenv("ARC2_CONFINE", "seatbelt")
    queue_job(runs)
    assert runner.main(["--runs", str(runs), "--claude", str(probe_engine), "--once"]) == 0
    record = json.loads((runs / "_jobs" / "job000001.json").read_text())
    assert record["state"] == "done", record
    assert record["confinement"] == "seatbelt"
    assert json.loads((runs / SLUG / "probe.json").read_text()) == {
        "write_own_run": "allowed",
        "write_request_file": "allowed",
        "write_other_run": "denied",
        "write_studio_owner": "denied",
        "write_queue": "denied",
        "write_repo": "denied",
        "read_other_run": "denied",
        "read_studio": "denied",
        "read_job_records": "denied",
        "read_via_symlink": "denied",
        "read_repo": "allowed",
    }
    assert json.loads((runs / "_studio" / "arc2-other.json").read_text()) == {"tenant_id": "other"}
    assert not (runs / "arc2-other" / "stolen.txt").exists()


def test_without_a_sandbox_the_runner_does_not_start(runs, probe_engine, monkeypatch):
    monkeypatch.setenv("ARC2_CONFINE", "auto")
    monkeypatch.setattr(confine.Seatbelt, "available", lambda self: False)
    queue_job(runs)
    assert runner.main(["--runs", str(runs), "--claude", str(probe_engine), "--once"]) == 2
    assert [p.name for p in (runs / "_queue").glob("*.json")] == ["20261004T120000-job000001.json"]
    assert not (runs / SLUG / "probe.json").exists()


@pytest.mark.parametrize("choice", ["seatbelt", "bubblewrap", "yes"])
def test_an_unavailable_or_unknown_sandbox_is_an_error_not_a_downgrade(monkeypatch, choice):
    monkeypatch.setattr(confine.Seatbelt, "available", lambda self: False)
    with pytest.raises(confine.ConfinementError):
        confine.select(choice)


def test_running_unconfined_takes_an_explicit_choice(monkeypatch):
    assert confine.select("none").name == "none"


def test_the_profile_allows_only_the_jobs_own_run(tmp_path):
    runs = tmp_path / 'we"ird\\runs'
    profile = confine.Seatbelt().profile(repo=tmp_path / "repo", runs=runs, slug=SLUG)
    r = str(runs.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    assert f'(deny file-write* (subpath "{r}"))' in profile
    assert f'(deny file-read-data (subpath "{r}"))' in profile
    assert f'(allow file-write* file-read-data (subpath "{r}/{SLUG}"))' in profile
    assert f'(allow file-write* file-read-data (literal "{r}/{SLUG}.request.txt"))' in profile
    # the allow rules come last: in Seatbelt the last matching rule wins
    assert profile.rindex("(deny") < profile.index("(allow file-write*")


def test_the_engine_may_edit_only_its_own_run():
    job = {"id": "job000001", "action": "start", "slug": SLUG, "text": "a course"}
    tools = runner.command_for(job, "claude")
    assert f"Write(./build/arc2/{SLUG}/**)" in tools and f"Edit(./build/arc2/{SLUG}/**)" in tools
    assert "Write(./build/arc2/**)" not in tools and "Edit(./build/arc2/**)" not in tools
