"""A failed OpenSearch snapshot never costs the backup of the data.

The snapshot is on by default, so an OpenSearch outage at 02:17 must not throw away that
night's database dumps, buckets and escrow. backup.sh keeps them, records the failure in
the manifest, alerts, and exits 5 ("data backed up, telemetry snapshot failed"), the way 3
means "data backed up, secrets not escrowed". restore.sh restores such a backup without
--skip-opensearch, and cron-backup.sh treats 5 like 3.

The real scripts run here against a stand-in ``docker`` that answers the compose, exec and
run calls they make (and logs each one); FAKE_OS=fail makes curl inside the opensearch
container fail as it does when the node is down. scripts/backup/drill.sh runs the same
failure against a real OpenSearch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts/backup"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
all="$*"
if [ "$1" = run ]; then   # mc_run: docker run ... -v <host>:/backup ... mc <args>
    host=""; prev=""
    for a in "$@"; do [ "$prev" = "-v" ] && host="${a%%:/backup}"; prev="$a"; done
    case "$all" in
        *" mirror --quiet --preserve tn /backup"*) mkdir -p "$host/bucket1"; echo data > "$host/bucket1/obj" ;;
        *" ls --recursive tn"*) echo "[2026-10-09 02:17:00 UTC] 5B STANDARD bucket1/obj" ;;
        *" ls tn"*) printf '[2026-10-09 02:17:00 UTC] 0B bucket1/\n[2026-10-09 02:17:00 UTC] 0B empty/\n' ;;
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
    *" ps --services"*) printf 'postgres\nminio\nopensearch\napi\n' ;;
    *" stop "*|*" start "*) : ;;
    *pg_dumpall*) echo "-- roles" ;;
    *"select datname"*) echo app ;;
    *"show server_version"*) echo 16.4 ;;
    *"pg_dump "*) echo DUMP ;;
    *"pg_restore --list"*) cat >/dev/null; echo "; archive" ;;
    *"pg_restore "*) cat >/dev/null ;;
    *"exec -T postgres psql"*) cat >/dev/null ;;
    *"exec -T opensearch curl"*)
        cat >/dev/null
        if [ "${FAKE_OS:-ok}" = fail ]; then
            echo "curl: (7) Failed to connect to localhost port 9200" >&2; exit 7
        fi
        case "$all" in
            *_cat/snapshots*) : ;;
            *) echo '{"snapshot":{"snapshot":"tn-x","state":"SUCCESS"}}' ;;
        esac ;;
    *) echo "fake docker: unhandled: $all" >&2; exit 99 ;;
esac
"""


@pytest.fixture
def stack(tmp_path: Path) -> dict:
    """PATH with the stand-in docker (and a silent logger), an env file and a compose file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "logger").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp_path / "compose.yml").write_text("services: {}\n")
    (tmp_path / "env").write_text("POSTGRES_USER=u\nPOSTGRES_DB=app\nMINIO_ACCESS_KEY=k\nMINIO_SECRET_KEY=s\n")
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "COMPOSE_FILE": str(tmp_path / "compose.yml"),
        "ENV_FILE": str(tmp_path / "env"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_MIN_FREE_GB": "0",
        "BACKUP_ESCROW": "out-of-band",
        "OPENSEARCH_SNAPSHOT_REPO": "tn_snapshots",
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


def _assert_complete(backup: Path) -> dict:
    for rel in ("postgres/globals.sql", "postgres/app.dump", "minio/bucket1/obj", "manifest.json"):
        assert (backup / rel).is_file(), rel
    assert (backup / "minio/empty").is_dir()
    for line in (backup / "SHA256SUMS").read_text().splitlines():
        digest, rel = line.split(maxsplit=1)
        assert hashlib.sha256((backup / rel.lstrip("*")).read_bytes()).hexdigest() == digest, rel
    return json.loads((backup / "manifest.json").read_text())


def test_a_failed_snapshot_keeps_the_data_backup_and_exits_5(stack):
    run = _run("backup.sh", {**stack, "FAKE_OS": "fail"})
    assert run.returncode == 5, run.stdout + run.stderr
    manifest = _assert_complete(_the_backup(stack))
    assert manifest["postgres"]["databases"][0]["name"] == "app"
    assert manifest["minio"] == {"buckets": 2, "objects": 1, "dir": "minio"}
    assert manifest["escrow"]["status"] == "out-of-band"
    os_entry = manifest["opensearch"]
    assert os_entry["status"] == "failed" and os_entry["repository"] == "tn_snapshots"
    assert os_entry["snapshot"].startswith("tn-") and "Failed to connect" in os_entry["error"]
    alerts = [ln for ln in run.stderr.splitlines() if "[ALERT]" in ln]
    assert any("OpenSearch telemetry snapshot" in ln for ln in alerts), run.stderr
    assert not any("backup FAILED" in ln for ln in alerts), run.stderr


def test_a_good_snapshot_exits_0_and_names_it(stack):
    run = _run("backup.sh", stack)
    assert run.returncode == 0, run.stdout + run.stderr
    os_entry = _assert_complete(_the_backup(stack))["opensearch"]
    assert os_entry["status"] == "snapshot" and os_entry["snapshot"].startswith("tn-")
    assert "[ALERT]" not in run.stderr


def test_missing_escrow_still_wins_over_a_failed_snapshot_and_both_alert(stack):
    env = {k: v for k, v in stack.items() if k != "BACKUP_ESCROW"}
    run = _run("backup.sh", {**env, "FAKE_OS": "fail"})
    assert run.returncode == 3, run.stdout + run.stderr
    _assert_complete(_the_backup(stack))
    assert "OpenSearch telemetry snapshot" in run.stderr and "WITHOUT secrets escrow" in run.stderr


def test_restore_of_a_backup_whose_snapshot_failed_restores_the_rest(stack):
    assert _run("backup.sh", {**stack, "FAKE_OS": "fail"}).returncode == 5
    backup = _the_backup(stack)
    Path(stack["FAKE_LOG"]).write_text("")
    # OpenSearch is up again by now: nothing may touch it all the same.
    run = _run("restore.sh", stack, str(backup), "--force", "--no-restart")
    assert run.returncode == 0, run.stdout + run.stderr
    out = run.stdout + run.stderr
    assert "snapshot FAILED" in out and "--skip-opensearch" not in out
    calls = Path(stack["FAKE_LOG"]).read_text()
    assert "pg_restore -U u -d template1 --create" in calls
    assert "mirror --quiet --overwrite /backup/bucket1 tn/bucket1" in calls
    assert "opensearch curl" not in calls
    # opensearch is stopped with the rest: it is not being restored.
    assert any(ln.endswith(" stop opensearch api") for ln in calls.splitlines()), calls


def test_the_pre_upgrade_backup_accepts_a_failed_telemetry_snapshot() -> None:
    """Upgrading to the first release with snapshots: the old OpenSearch has no path.repo
    and 70-telemetry has not registered the repository, so the pre-upgrade backup exits 5
    with the data safe. That must not stop the upgrade (staging rc3 -> rc4, 2026-10-09)."""
    import yaml

    tasks = yaml.safe_load((ROOT / "install/roles/tn_compose/tasks/main.yml").read_text())
    blocks = [t for t in tasks if "block" in t]
    task = next(
        t for b in blocks for t in b["block"] if t.get("name") == "Compose — pre-upgrade backup (the rollback point)"
    )
    assert task["failed_when"] == "tn_pre_backup.rc not in [0, 5]"
