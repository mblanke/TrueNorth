"""One runner owns the queue at a time, and a restart recovers its own leftovers only.

The runner is a single host process. Its startup recovery fails jobs left ``running``,
which is right after a crash and wrong if another runner is still working on them. So a
runner holds an exclusive OS lock on the runs root for its whole life: a second one does
not start (and recovers nothing), and the lock is released by the kernel when the holder
dies, however it dies. A job claimed but not yet recorded when the runner died goes back
on the queue; it never ran. Each job record names the runner that ran it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from arc2 import runner


def _queue(runs: Path, job_id: str, slug: str = "arc2-recover") -> Path:
    queue, _ = runner.dirs(runs)
    job = {"id": job_id, "action": "start", "slug": slug, "text": "a course", "created_at": "2026-10-04T12:00:00Z"}
    path = queue / f"20261004T120000-{job_id}.json"
    path.write_text(json.dumps(job))
    return path


def _hold_lock(runs: Path) -> subprocess.Popen:
    """Another live runner: a separate process holding the runs-root lock."""
    code = (
        "import fcntl, sys, time; f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('held', flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(runs / "_runner.lock")], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_second_runner_does_not_start_or_touch_the_first_runners_jobs(tmp_path):
    runs = tmp_path / "runs"
    _, jobs = runner.dirs(runs)
    (jobs / "live.json").write_text(json.dumps({"id": "live", "state": "running"}))
    queued = _queue(runs, "job000002")
    other = _hold_lock(runs)
    try:
        assert runner.main(["--runs", str(runs), "--claude", str(tmp_path / "never"), "--once"]) == 3
    finally:
        other.kill()
        other.wait()
    assert json.loads((jobs / "live.json").read_text())["state"] == "running", "a live runner's job was failed"
    assert queued.exists(), "the second runner claimed a job"


def test_the_lock_is_released_when_the_holder_dies(tmp_path):
    runs = tmp_path / "runs"
    runner.dirs(runs)
    other = _hold_lock(runs)
    other.kill()
    other.wait()
    assert runner.main(["--runs", str(runs), "--claude", str(tmp_path / "never"), "--once"]) == 0


def test_a_job_claimed_but_not_recorded_when_the_runner_died_goes_back_on_the_queue(tmp_path):
    runs = tmp_path / "runs"
    queue, jobs = runner.dirs(runs)
    path = _queue(runs, "job000003")
    path.rename(jobs / "job000003.claiming")  # the crash window inside claim()
    runner.recover(queue, jobs)
    back = list(queue.glob("*.json"))
    assert [json.loads(p.read_text())["id"] for p in back] == ["job000003"]
    assert not list(jobs.glob("*.claiming"))


def test_a_job_left_running_by_a_dead_runner_is_failed_on_restart(tmp_path):
    runs = tmp_path / "runs"
    queue, jobs = runner.dirs(runs)
    (jobs / "old.json").write_text(json.dumps({"id": "old", "state": "running"}))
    runner.recover(queue, jobs)
    record = json.loads((jobs / "old.json").read_text())
    assert record["state"] == "failed" and "runner stopped" in record["error"]


def test_each_job_record_names_the_runner_that_ran_it(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    exe = tmp_path / "claude"
    exe.write_text('#!/bin/sh\necho \'{"type": "result", "is_error": false, "result": "ok"}\'\n')
    exe.chmod(0o755)
    monkeypatch.setenv("ARC2_FALLBACK", "off")
    _queue(runs, "job000004")
    assert runner.main(["--runs", str(runs), "--claude", str(exe), "--once"]) == 0
    record = json.loads((runs / "_jobs" / "job000004.json").read_text())
    assert record["runner"] == {
        "pid": os.getpid(),
        "host": os.uname().nodename,
        "started_at": record["runner"]["started_at"],
    }
    assert record["state"] == "done"
