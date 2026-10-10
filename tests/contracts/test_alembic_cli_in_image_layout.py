"""`alembic heads` and `history` work the way the installer runs them in the api image.

The installer's pre-migration check runs `alembic -c alembic.ini heads` from /app with no
PYTHONPATH. Revision files are imported before env.py, so a migration that imports `app`
needs the API directory on sys.path; three new ones did not patch it themselves and the
check crashed with ModuleNotFoundError on staging (v1.2.0-rc1). CI only ever ran
migrations through env.py, which hides this.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"


def _alembic(*args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("alembic", path=str(Path(sys.executable).parent)) or shutil.which("alembic")
    if exe is None:
        pytest.skip("alembic CLI not installed")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([exe, "-c", "alembic.ini", *args], cwd=API, env=env, capture_output=True, text=True)


def test_heads_loads_every_revision_without_pythonpath() -> None:
    run = _alembic("heads")
    assert run.returncode == 0, run.stderr[-2000:]
    heads = [line for line in run.stdout.splitlines() if "(head)" in line]
    assert len(heads) == 1, run.stdout


def test_history_loads_every_revision_without_pythonpath() -> None:
    run = _alembic("history")
    assert run.returncode == 0, run.stderr[-2000:]


def test_ini_prepends_the_api_directory() -> None:
    assert "prepend_sys_path = ." in (API / "alembic.ini").read_text()
