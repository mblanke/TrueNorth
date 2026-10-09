"""Backups hold the installer's Moodle when it runs one (tn_moodle_enabled).

Moodle's database is `moodle` in the platform's PostgreSQL, so backup.sh's dump of every
database already holds it. Its files (moodledata) are archived when backup.env names the
node (MOODLE_COMPOSE_FILE / MOODLE_ENV_FILE), through a throwaway container of its own
image; restore.sh stops the node with the platform, restores the database with the others,
replaces moodledata with the archive and starts the node after the platform.

The real scripts run against a stand-in ``docker`` (as in test_backup_snapshot_failure.py)
that answers the platform's and the Moodle node's compose calls and logs each one. The
same tar and restore commands were run against the real image (compose.moodle-prod.yml);
the commit message records that.
"""

from __future__ import annotations

import hashlib
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
moodle=0
case "$all" in *"$FAKE_MOODLE_COMPOSE"*) moodle=1 ;; esac
if [ "$moodle" = 1 ]; then
    case "$all" in
        *" run --rm --no-deps -T --entrypoint tar moodle "*)
            [ "${FAKE_MOODLE:-ok}" = fail ] && { echo "Error: no such image" >&2; exit 1; }
            tar -C "$FAKE_MOODLEDATA" -cf - . ;;
        *" run --rm --no-deps -T --entrypoint sh moodle "*) cat > "$FAKE_RESTORED_TAR" ;;
        *" ps --services --status running"*) printf 'moodle\nmoodle-edge\n' ;;
        *" stop "*|*" start "*) : ;;
        *) echo "fake docker (moodle): unhandled: $all" >&2; exit 99 ;;
    esac
    exit 0
fi
if [ "$1" = run ]; then   # mc_run
    host=""; prev=""
    for a in "$@"; do [ "$prev" = "-v" ] && host="${a%%:/backup}"; prev="$a"; done
    case "$all" in
        *" mirror --quiet --preserve tn /backup"*) mkdir -p "$host/bucket1"; echo data > "$host/bucket1/obj" ;;
        *" ls --recursive tn"*) echo "[2026-10-09 02:17:00 UTC] 5B STANDARD bucket1/obj" ;;
        *" ls tn"*) printf '[2026-10-09 02:17:00 UTC] 0B bucket1/\n' ;;
    esac
    exit 0
fi
if [ "$1" = inspect ]; then
    case "$all" in
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
    *"select datname"*) printf 'app\nmoodle\n' ;;
    *"show server_version"*) echo 16.4 ;;
    *"pg_dump "*) echo DUMP ;;
    *"pg_restore --list"*) cat >/dev/null; echo "; archive" ;;
    *"pg_restore "*) cat >/dev/null ;;
    *"exec -T postgres psql"*) cat >/dev/null ;;
    *) echo "fake docker: unhandled: $all" >&2; exit 99 ;;
esac
"""


@pytest.fixture
def stack(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "logger").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp_path / "compose.yml").write_text("services: {}\n")
    (tmp_path / "compose.moodle-prod.yml").write_text("services: {}\n")
    (tmp_path / "env").write_text("POSTGRES_USER=u\nPOSTGRES_DB=app\nMINIO_ACCESS_KEY=k\nMINIO_SECRET_KEY=s\n")
    (tmp_path / "moodle-default.env").write_text("MOODLE_NODE=default\n")
    data = tmp_path / "moodledata"
    (data / "filedir/ab/cd").mkdir(parents=True)
    (data / "filedir/ab/cd/abcd1234").write_bytes(b"a course file")
    (data / "truenorth-registration.json").write_text("{}")
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_MOODLE_COMPOSE": str(tmp_path / "compose.moodle-prod.yml"),
        "FAKE_MOODLEDATA": str(data),
        "FAKE_RESTORED_TAR": str(tmp_path / "restored.tar"),
        "COMPOSE_FILE": str(tmp_path / "compose.yml"),
        "ENV_FILE": str(tmp_path / "env"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_MIN_FREE_GB": "0",
        "BACKUP_ESCROW": "out-of-band",
        "MOODLE_COMPOSE_FILE": str(tmp_path / "compose.moodle-prod.yml"),
        "MOODLE_ENV_FILE": str(tmp_path / "moodle-default.env"),
    }


def _run(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BACKUP / script), *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )


def _the_backup(env: dict) -> Path:
    root = Path(env["BACKUP_DIR"])
    assert not list(root.glob(".partial-*")), "a partial backup was left behind"
    [backup] = [p for p in root.glob("truenorth-backup-*") if p.is_dir()]
    return backup


def _calls(env: dict) -> list[str]:
    return Path(env["FAKE_LOG"]).read_text().splitlines()


def test_backup_archives_moodledata_and_dumps_its_database(stack):
    run = _run("backup.sh", stack)
    assert run.returncode == 0, run.stdout + run.stderr
    backup = _the_backup(stack)
    manifest = json.loads((backup / "manifest.json").read_text())
    assert [d["name"] for d in manifest["postgres"]["databases"]] == ["app", "moodle"]
    entry = manifest["moodle"]
    assert entry["status"] == "backed-up" and entry["moodledata"] == "moodle/moodledata.tar"
    archive = backup / "moodle/moodledata.tar"
    assert entry["bytes"] == archive.stat().st_size
    with tarfile.open(archive) as tf:
        assert "./filedir/ab/cd/abcd1234" in tf.getnames()
    sums = dict(reversed(line.split(maxsplit=1)) for line in (backup / "SHA256SUMS").read_text().splitlines())
    assert sums["./moodle/moodledata.tar"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    [tar_call] = [c for c in _calls(stack) if "--entrypoint tar moodle" in c]
    assert f"-f {stack['MOODLE_COMPOSE_FILE']} --env-file {stack['MOODLE_ENV_FILE']} run --rm --no-deps -T" in tar_call
    for skipped in ("cache", "localcache", "sessions", "temp", "trashdir"):
        assert f"--exclude=./{skipped}" in tar_call


def test_a_failed_moodle_archive_fails_the_backup(stack):
    run = _run("backup.sh", {**stack, "FAKE_MOODLE": "fail"})
    assert run.returncode == 1, run.stdout + run.stderr
    assert "backup FAILED at step 'moodle'" in run.stderr
    assert not list(Path(stack["BACKUP_DIR"]).glob("truenorth-backup-*"))


def test_configured_but_not_installed_is_recorded_not_fatal(stack):
    """30-config renders backup.env before 85-moodle installs the node; a pre-upgrade
    backup in between must not fail."""
    Path(stack["MOODLE_ENV_FILE"]).unlink()
    run = _run("backup.sh", stack)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads((_the_backup(stack) / "manifest.json").read_text())["moodle"]["status"] == "not-installed"
    assert not any("moodle-prod" in c for c in _calls(stack))


def test_without_moodle_nothing_moodle_runs(stack):
    env = {k: v for k, v in stack.items() if not k.startswith("MOODLE_")}
    run = _run("backup.sh", env)
    assert run.returncode == 0, run.stdout + run.stderr
    backup = _the_backup(env)
    assert json.loads((backup / "manifest.json").read_text())["moodle"] == {"status": "not-configured"}
    assert not (backup / "moodle").exists()
    assert not any("moodle-prod" in c for c in _calls(env))


def test_restore_stops_moodle_restores_its_files_and_starts_it_last(stack):
    assert _run("backup.sh", stack).returncode == 0
    backup = _the_backup(stack)
    Path(stack["FAKE_LOG"]).write_text("")
    run = _run("restore.sh", stack, str(backup), "--force")
    assert run.returncode == 0, run.stdout + run.stderr
    calls = _calls(stack)

    def at(fragment: str) -> int:
        return next(i for i, c in enumerate(calls) if fragment in c)

    moodle_stop = at(f"{stack['MOODLE_COMPOSE_FILE']} --env-file {stack['MOODLE_ENV_FILE']} stop moodle moodle-edge")
    files = at("--entrypoint sh moodle")
    platform_start = at("start api")
    moodle_start = at(f"{stack['MOODLE_ENV_FILE']} start moodle moodle-edge")
    first_restore = at("pg_restore -U u -d template1 --create")
    assert moodle_stop < first_restore < files < platform_start < moodle_start
    assert Path(stack["FAKE_RESTORED_TAR"]).read_bytes() == (backup / "moodle/moodledata.tar").read_bytes()
    assert "find /var/www/moodledata -mindepth 1 -maxdepth 1 -exec rm -rf {} +" in calls[files]


def test_restore_without_the_node_here_keeps_its_hands_off(stack):
    assert _run("backup.sh", stack).returncode == 0
    backup = _the_backup(stack)
    Path(stack["FAKE_LOG"]).write_text("")
    env = {k: v for k, v in stack.items() if not k.startswith("MOODLE_")}
    run = _run("restore.sh", env, str(backup), "--force", "--no-restart")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "its files are NOT restored" in run.stdout + run.stderr
    assert not any("moodle-prod" in c for c in _calls(env))


def test_skip_moodle_restores_no_files(stack):
    assert _run("backup.sh", stack).returncode == 0
    backup = _the_backup(stack)
    Path(stack["FAKE_LOG"]).write_text("")
    run = _run("restore.sh", stack, str(backup), "--force", "--skip-moodle")
    assert run.returncode == 0, run.stdout + run.stderr
    calls = _calls(stack)
    assert not any("--entrypoint sh moodle" in c for c in calls)
    # Its database is still replaced, so the node is still stopped around that.
    assert any(c.endswith("stop moodle moodle-edge") for c in calls)
