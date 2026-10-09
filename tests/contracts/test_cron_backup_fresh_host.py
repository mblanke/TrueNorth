"""The nightly wrapper reports success for a successful backup on a new host.

On a host's first nights only daily/ exists; counting weekly/ and monthly/ for the summary
failed under pipefail + errexit, so cron-backup.sh exited 1 (and alerted) after a backup
that had succeeded (staging, 2026-10-09). The real wrapper and lib.sh run here against a
stand-in backup.sh that writes one backup directory.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts/backup"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_BACKUP = """#!/usr/bin/env bash
set -euo pipefail
mkdir -p "${BACKUP_DIR}/truenorth-backup-$(date -u +%Y%m%dT%H%M%SZ)"
"""


def _wrapper(tmp_path: Path, backup_script: str) -> tuple[subprocess.CompletedProcess, Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(BACKUP / "cron-backup.sh", scripts / "cron-backup.sh")
    shutil.copy(BACKUP / "lib.sh", scripts / "lib.sh")
    (scripts / "backup.sh").write_text(backup_script)
    backups = tmp_path / "backups"  # nothing yet: no daily/, weekly/ or monthly/

    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "BACKUP_DIR": str(backups)}
    run = subprocess.run(["bash", str(scripts / "cron-backup.sh")], env=env, capture_output=True, text=True)
    return run, backups


def test_a_successful_backup_on_a_fresh_host_exits_zero(tmp_path: Path) -> None:
    run, backups = _wrapper(tmp_path, FAKE_BACKUP)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Cron backup complete" in run.stdout + run.stderr
    assert len([p for p in backups.rglob("truenorth-backup-*") if p.is_dir()]) == 1


def test_a_failed_telemetry_snapshot_is_kept_rotated_and_passed_through(tmp_path: Path) -> None:
    """backup.sh exit 5: the data was backed up (it alerted about the snapshot). The wrapper
    treats it like 3: the backup stays, rotation and the summary run, and 5 comes back out."""
    run, backups = _wrapper(tmp_path, FAKE_BACKUP + "exit 5\n")
    out = run.stdout + run.stderr
    assert run.returncode == 5, out
    # The summary ran and counted it (daily, or weekly/monthly on a Sunday or the 1st).
    assert "Cron backup complete" in out and re.search(r"(Daily|Weekly|Monthly):\s*1/", out), out
    assert "telemetry snapshot FAILED" in out and "Failed (exit 5)" not in out
    assert len([p for p in backups.rglob("truenorth-backup-*") if p.is_dir()]) == 1


def test_a_real_failure_still_stops_the_wrapper(tmp_path: Path) -> None:
    run, _ = _wrapper(tmp_path, "#!/usr/bin/env bash\nexit 1\n")
    assert run.returncode == 1
    assert "[ALERT]" in run.stderr and "Cron backup complete" not in run.stdout + run.stderr
