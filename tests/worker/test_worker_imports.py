"""Every worker task module imports on its own, in a fresh interpreter.

``import worker.tasks`` used to fail with a circular import (tasks -> celery_app ->
lab_tasks -> tasks) unless ``worker.celery_app`` happened to be imported first. The
running worker never noticed, but ``pytest.importorskip("worker.tasks")`` turned it
into a silent skip of whole test modules. In-process imports cannot catch this
(another test may already have imported celery_app), hence the subprocesses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("celery")

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "control-plane" / "worker"


def _python(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(WORKER), os.environ.get("PYTHONPATH", "")])}
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("module", ["worker.tasks", "worker.lab_tasks", "worker.celery_app"])
def test_module_imports_first_in_a_fresh_interpreter(module):
    proc = _python("-c", f"import {module}")
    assert proc.returncode == 0, proc.stderr


def test_snapshot_task_tests_are_collected_not_skipped():
    proc = _python(
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        "tests/worker/test_snapshot_lifecycle.py",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipped" not in proc.stdout, proc.stdout
    assert "TestSnapshotRangeTask::test_records_the_name_on_the_ranges_own_backend" in proc.stdout, proc.stdout
