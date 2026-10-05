"""Every worker module imports first, in a fresh interpreter.

celery_app imports the task modules at its end. A task module that imported tasks.py at
load time (power_tasks did, in #30) found it half-initialised whenever tasks.py was the
first module imported: ``import worker.tasks`` raised ImportError, though ``celery -A
worker.celery_app`` happened to work.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[2] / "control-plane" / "worker"


@pytest.mark.parametrize(
    "module", ["worker.tasks", "worker.power_tasks", "worker.celery_app", "worker.range_rows", "worker.render"]
)
def test_module_imports_first(module):
    env = {**os.environ, "PYTHONPATH": str(WORKER)}
    done = subprocess.run([sys.executable, "-c", f"import {module}"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
