"""The nightly wrapper reports success for a successful backup on a new host.

On a host's first nights only daily/ exists; counting weekly/ and monthly/ for the summary
failed under pipefail + errexit, so cron-backup.sh exited 1 (and alerted) after a backup
that had succeeded (staging, 2026-10-09). The real wrapper and lib.sh run here against a
stand-in backup.sh that writes one backup directory.
"""

from __future__ import annotations

import os
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


def test_a_successful_backup_on_a_fresh_host_exits_zero(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(BACKUP / "cron-backup.sh", scripts / "cron-backup.sh")
    shutil.copy(BACKUP / "lib.sh", scripts / "lib.sh")
    (scripts / "backup.sh").write_text(FAKE_BACKUP)
    backups = tmp_path / "backups"  # nothing yet: no daily/, weekly/ or monthly/

    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "BACKUP_DIR": str(backups)}
    run = subprocess.run(["bash", str(scripts / "cron-backup.sh")], env=env, capture_output=True, text=True)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "Cron backup complete" in run.stdout + run.stderr
    assert len([p for p in backups.rglob("truenorth-backup-*") if p.is_dir()]) == 1
