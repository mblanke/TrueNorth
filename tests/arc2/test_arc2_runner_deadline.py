"""The runner's deadline holds however the engine behaves.

The deadline used to be checked only when the engine printed a line, so a silent engine
(a stuck subagent, a network stall) held the single runner forever and every queued job
behind it. These tests run a fake ``claude`` that misbehaves in each way and check that
the job fails within the deadline plus the shutdown grace, that the engine's whole process
group is gone afterwards, and that the next queued job still runs.

Each call is made from a thread with a join timeout, so if the deadline does not hold the
test fails instead of hanging the suite; the finaliser kills whatever was left behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import threading
import time
from pathlib import Path

import pytest
from arc2 import runner

TIMEOUT = 1  # seconds
GRACE = 1  # seconds between SIGTERM and SIGKILL
SLACK = 4  # scheduling slack on a loaded CI box

FAKE = r"""#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
pids = os.environ["FAKE_PIDS"]
open(pids, "a").write(f"{os.getpid()}\n")
prompt = sys.argv[2]
def emit(e): print(json.dumps(e), flush=True)
emit({"type": "system", "subtype": "init"})
if "silent" in prompt:
    time.sleep(60)
elif "deaf" in prompt:                      # ignores SIGTERM: only SIGKILL stops it
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
elif "grandchild" in prompt:                # a descendant that would outlive a plain kill of the engine
    gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)
    open(pids, "a").write(f"{gc.pid}\n")
    time.sleep(60)
elif "orphan" in prompt:                    # exits at once; a descendant keeps stdout open
    gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(pids, "a").write(f"{gc.pid}\n")
    emit({"type": "result", "is_error": False, "result": "done early", "num_turns": 1})
    sys.exit(0)
elif "partial" in prompt:                   # half a line, no newline, then nothing
    sys.stdout.write('{"type": "assist'); sys.stdout.flush()
    time.sleep(60)
elif "crash" in prompt:
    sys.stdout.write("not json\n"); sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)
emit({"type": "result", "is_error": False, "result": "Outline ready for review.", "num_turns": 2})
"""


@pytest.fixture
def engine(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    pids = tmp_path / "pids"
    pids.touch()
    monkeypatch.setenv("FAKE_PIDS", str(pids))
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("ARC2_FALLBACK", "off")
    monkeypatch.setattr(runner, "SHUTDOWN_GRACE", GRACE, raising=False)
    yield exe, pids
    for pid in _pids(pids):  # never leave a sleeper behind, even when the test failed
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _pids(path: Path) -> list[int]:
    return [int(x) for x in path.read_text().split()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def queue_job(runs: Path, job_id: str, text: str) -> None:
    queue, _ = runner.dirs(runs)
    job = {
        "id": job_id,
        "action": "start",
        "slug": "arc2-deadline",
        "text": text,
        "created_at": f"2026-10-04T12:00:0{job_id[-1]}Z",
        "requested_by": "dev-admin",
    }
    (queue / f"20261004T12000{job_id[-1]}-{job_id}.json").write_text(json.dumps(job))


def run_queue(runs: Path, exe: Path, budget: float) -> float:
    """Run the queue once in a thread; fail (not hang) if it overruns the budget."""
    done = threading.Event()

    def go():
        runner.main(["--runs", str(runs), "--claude", str(exe), "--timeout", str(TIMEOUT), "--once"])
        done.set()

    started = time.monotonic()
    threading.Thread(target=go, daemon=True).start()
    assert done.wait(budget), f"the runner was still busy after {budget}s: the deadline did not hold"
    return time.monotonic() - started


def records(runs: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((runs / "_jobs").glob("*.json"))]


def gone(pids: list[int], within: float = 3.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("text", ["silent engine", "partial line", "deaf engine"])
def test_an_engine_that_goes_quiet_is_stopped_at_the_deadline(tmp_path, engine, text):
    exe, pids = engine
    runs = tmp_path / "runs"
    queue_job(runs, "job000001", text)
    took = run_queue(runs, exe, budget=TIMEOUT + GRACE + SLACK)
    [rec] = records(runs)
    assert rec["state"] == "failed"
    assert "timed out" in rec["error"]
    assert took < TIMEOUT + GRACE + SLACK
    assert gone(_pids(pids))


def test_the_engines_descendants_are_stopped_with_it(tmp_path, engine):
    exe, pids = engine
    runs = tmp_path / "runs"
    queue_job(runs, "job000001", "grandchild")
    run_queue(runs, exe, budget=TIMEOUT + GRACE + SLACK)
    assert len(_pids(pids)) == 2
    assert gone(_pids(pids)), "the engine's process group outlived the job"


def test_an_engine_that_exits_but_leaves_its_output_open_does_not_hold_the_runner(tmp_path, engine):
    exe, pids = engine
    runs = tmp_path / "runs"
    queue_job(runs, "job000001", "orphan")
    run_queue(runs, exe, budget=TIMEOUT + GRACE + SLACK)
    [rec] = records(runs)
    assert rec["result"] == "done early"
    assert gone(_pids(pids)), "a descendant holding the engine's output outlived the job"


def test_the_next_job_runs_after_a_hung_one(tmp_path, engine):
    exe, _ = engine
    runs = tmp_path / "runs"
    queue_job(runs, "job000001", "silent engine")
    queue_job(runs, "job000002", "a normal course request")
    run_queue(runs, exe, budget=TIMEOUT + GRACE + SLACK + 3)
    first, second = records(runs)
    assert (first["id"], first["state"]) == ("job000001", "failed")
    assert (second["id"], second["state"], second["result"]) == ("job000002", "done", "Outline ready for review.")


def test_a_crashed_engine_is_a_failed_job_with_its_exit_status(tmp_path, engine):
    exe, _ = engine
    runs = tmp_path / "runs"
    queue_job(runs, "job000001", "crash")
    run_queue(runs, exe, budget=TIMEOUT + GRACE + SLACK)
    [rec] = records(runs)
    assert rec["state"] == "failed"
    assert rec["exit_code"] == -signal.SIGKILL
    assert "timed out" not in (rec["error"] or "")
    assert (runs / "_jobs" / rec["log"]).read_text().startswith('{"type": "system"')
