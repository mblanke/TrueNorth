"""Backups hold the ARC² Course Studio runs when the host runs the Studio (tn_arc2_enabled).

Every Studio run (request, staged course, QA report, cmi5 package) lives only in the
runs root on the host (tn_arc2_runs_dir, tn-arc2:tn-arc2-runs, 02750), which the backup
user cannot read through. backup.sh archives it as root through a throwaway container of
the platform's postgres image, read-only and without network, numeric owners and modes
kept; restore.sh replaces it the same way, refuses while the runner is active, and maps
the archive's owner of the runs root onto the current one (a rebuilt host's tn-arc2 may
have other ids).

The real scripts run against a stand-in ``docker`` and ``systemctl`` (as in
test_backup_snapshot_failure.py) that log each call; the container's tar is the host's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts/backup"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("tar") is None, reason="needs bash, tar")

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
all="$*"
if [ "$1" = run ]; then
    host=""; prev=""
    for a in "$@"; do
        [ "$prev" = "-v" ] && host="${a%%:*}"
        prev="$a"
    done
    case "$all" in
        *":/runs:ro --entrypoint tar "*)          # backup.sh: archive the runs root
            case "${FAKE_ARC2:-ok}" in
                fail) echo "tar: /runs: Cannot open: Permission denied" >&2; exit 2 ;;
                changed) tar -C "$host" -cf - . ; echo "tar: ./x: file changed as we read it" >&2; exit 1 ;;
                *) exec tar -C "$host" -cf - . ;;
            esac ;;
        *":/runs --entrypoint sh "*)              # restore.sh: replace the runs root
            cat > "$FAKE_RESTORED_TAR"; exit 0 ;;
        *" mirror --quiet --preserve tn /backup"*) mkdir -p "$host/bucket1"; echo data > "$host/bucket1/obj" ;;
        *" ls --recursive tn"*) echo "[2026-10-09 02:17:00 UTC] 5B STANDARD bucket1/obj" ;;
        *" ls tn"*) printf '[2026-10-09 02:17:00 UTC] 0B bucket1/\n' ;;
    esac
    exit 0
fi
if [ "$1" = inspect ]; then
    case "$all" in
        *Config.Image*) echo "postgres:16@sha256:0000" ;;
        *Networks*) echo tn-backend ;;
        *com.docker.compose.project*) echo tnproj ;;
    esac
    exit 0
fi
case "$all" in
    *" ps -q "*) echo "cid-${!#}" ;;
    *" ps --services"*) printf 'postgres\nminio\napi\n' ;;
    *" stop "*|*" start "*) : ;;
    *pg_dumpall*) echo "-- roles" ;;
    *"select datname"*) echo app ;;
    *"show server_version"*) echo 16.4 ;;
    *"pg_dump "*) echo DUMP ;;
    *"pg_restore --list"*) cat >/dev/null; echo "; archive" ;;
    *"pg_restore "*) cat >/dev/null ;;
    *"exec -T postgres psql"*) cat >/dev/null ;;
    *) echo "fake docker: unhandled: $all" >&2; exit 99 ;;
esac
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$FAKE_LOG"
[ "$1" = is-active ] || exit 99
echo "${FAKE_RUNNER:-inactive}"
[ "${FAKE_RUNNER:-inactive}" = active ]
"""


@pytest.fixture
def stack(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "logger").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp_path / "compose.yml").write_text("services: {}\n")
    (tmp_path / "env").write_text("POSTGRES_USER=u\nPOSTGRES_DB=app\nMINIO_ACCESS_KEY=k\nMINIO_SECRET_KEY=s\n")
    runs = tmp_path / "arc2/runs"
    (runs / "_queue").mkdir(parents=True)
    (runs / "soc-101/07-bundle").mkdir(parents=True)
    (runs / "soc-101/07-bundle/course.yaml").write_text("title: SOC 101\n")
    (runs / "_history/job-1.json").parent.mkdir()
    (runs / "_history/job-1.json").write_text('{"status": "done"}\n')
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_RESTORED_TAR": str(tmp_path / "restored.tar"),
        "COMPOSE_FILE": str(tmp_path / "compose.yml"),
        "ENV_FILE": str(tmp_path / "env"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_MIN_FREE_GB": "0",
        "BACKUP_ESCROW": "out-of-band",
        "ARC2_RUNS_DIR": str(runs),
        "ARC2_RUNNER_UNIT": "truenorth-arc2-runner",
    }


def _run(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BACKUP / script), *args], env=env, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=120,
    )


def _the_backup(env: dict) -> Path:
    root = Path(env["BACKUP_DIR"])
    assert not list(root.glob(".partial-*")), "a partial backup was left behind"
    [backup] = [p for p in root.glob("truenorth-backup-*") if p.is_dir()]
    return backup


def _calls(env: dict) -> list[str]:
    return Path(env["FAKE_LOG"]).read_text().splitlines()


def test_the_runs_are_archived_as_root_read_only_without_network(stack):
    run = _run("backup.sh", {**stack, "FAKE_RUNNER": "active"})
    assert run.returncode == 0, run.stdout + run.stderr
    backup = _the_backup(stack)
    with tarfile.open(backup / "arc2/runs.tar") as tar:
        names = set(tar.getnames())
    assert {"./soc-101/07-bundle/course.yaml", "./_history/job-1.json", "./_queue"} <= names
    assert "arc2/runs.tar" in (backup / "SHA256SUMS").read_text()
    entry = json.loads((backup / "manifest.json").read_text())["arc2"]
    assert entry["status"] == "backed-up" and entry["runs"] == "arc2/runs.tar"
    assert entry["source"] == stack["ARC2_RUNS_DIR"] and entry["runner"] == "active"
    assert entry["changed_while_reading"] is False
    [call] = [c for c in _calls(stack) if ":/runs:ro" in c]
    assert call.startswith("run --rm --network none --user 0:0 -v ")
    assert "--entrypoint tar postgres:16@sha256:0000 -C /runs --numeric-owner -cf - ." in call
    # The runner is only asked for its state, never stopped.
    assert [c for c in _calls(stack) if c.startswith("systemctl")] == ["systemctl is-active truenorth-arc2-runner"]


def test_a_run_written_during_the_backup_is_recorded_not_fatal(stack):
    run = _run("backup.sh", {**stack, "FAKE_ARC2": "changed"})
    assert run.returncode == 0, run.stdout + run.stderr
    entry = json.loads((_the_backup(stack) / "manifest.json").read_text())["arc2"]
    assert entry["status"] == "backed-up" and entry["changed_while_reading"] is True
    assert "a job in progress may be incomplete" in run.stdout + run.stderr


def test_a_failed_archive_fails_the_backup(stack):
    run = _run("backup.sh", {**stack, "FAKE_ARC2": "fail"})
    assert run.returncode == 1
    assert "backup FAILED at step 'arc2'" in run.stderr
    assert not list(Path(stack["BACKUP_DIR"]).glob("truenorth-backup-*"))


def test_runs_dir_not_there_yet_or_not_configured(stack, tmp_path):
    run = _run("backup.sh", {**stack, "ARC2_RUNS_DIR": str(tmp_path / "nope")})
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads((_the_backup(stack) / "manifest.json").read_text())["arc2"]["status"] == "not-installed"

    shutil.rmtree(stack["BACKUP_DIR"])
    env = {k: v for k, v in stack.items() if k != "ARC2_RUNS_DIR"}
    assert _run("backup.sh", env).returncode == 0
    backup = _the_backup(env)
    assert json.loads((backup / "manifest.json").read_text())["arc2"] == {"status": "not-configured"}
    assert not (backup / "arc2").exists()


def test_restore_refuses_while_the_runner_is_active(stack):
    assert _run("backup.sh", stack).returncode == 0
    backup = _the_backup(stack)
    Path(stack["FAKE_LOG"]).write_text("")
    run = _run("restore.sh", {**stack, "FAKE_RUNNER": "active"}, str(backup), "--force", "--no-restart")
    assert run.returncode == 1
    assert "sudo systemctl stop truenorth-arc2-runner" in run.stderr
    assert not any(" stop " in c or "pg_restore" in c for c in _calls(stack)), "nothing may be touched"
    # --skip-arc2 restores the rest with the runner up.
    run = _run("restore.sh", {**stack, "FAKE_RUNNER": "active"}, str(backup), "--force", "--no-restart", "--skip-arc2")
    assert run.returncode == 0, run.stdout + run.stderr
    assert not Path(stack["FAKE_RESTORED_TAR"]).exists()


def test_restore_replaces_the_runs_as_root_keeping_owners(stack):
    assert _run("backup.sh", stack).returncode == 0
    backup = _the_backup(stack)
    Path(stack["FAKE_LOG"]).write_text("")
    run = _run("restore.sh", stack, str(backup), "--force", "--no-restart")
    assert run.returncode == 0, run.stdout + run.stderr
    assert Path(stack["FAKE_RESTORED_TAR"]).read_bytes() == (backup / "arc2/runs.tar").read_bytes()
    [call] = [c for c in _calls(stack) if ":/runs " in c]
    assert call.startswith(f"run --rm -i --network none --user 0:0 -v {stack['ARC2_RUNS_DIR']}:/runs --entrypoint sh ")
    script = Path(stack["FAKE_LOG"]).read_text()  # the sh -c script spans lines
    assert "tar -C /runs --numeric-owner -xpf -" in script and 'chown -R --from="$au" "$u" /runs' in script
    assert "start the runner again" in run.stdout + run.stderr


def test_restore_on_a_host_without_the_studio_skips_the_runs(stack, tmp_path):
    assert _run("backup.sh", stack).returncode == 0
    backup = _the_backup(stack)
    env = {k: v for k, v in stack.items() if k != "ARC2_RUNS_DIR"}
    run = _run("restore.sh", env, str(backup), "--force", "--no-restart")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "they are NOT restored" in run.stdout + run.stderr
    assert not Path(stack["FAKE_RESTORED_TAR"]).exists()


def test_the_installer_names_the_runs_dir_only_with_the_studio():
    tasks = (ROOT / "install/roles/tn_config/tasks/main.yml").read_text()
    block = tasks.split("{% if tn_arc2_enabled | default(false) | bool %}", 1)[1].split("{% endif %}", 1)[0]
    assert "ARC2_RUNS_DIR={{ tn_arc2_runs_dir }}" in block
    assert "ARC2_RUNNER_UNIT=truenorth-arc2-runner" in block
