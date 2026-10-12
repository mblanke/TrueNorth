"""Backups hold the signed audit export archive and verify it (ADR 0011).

backup.sh copies AUDIT_ARCHIVE_PATH into ``audit-archive/``, verifies every manifest's
Ed25519 signature and file hash with OpenSSL 3, and records ``audit_archive`` in the
manifest; a copy that does not verify is kept but the exit code is 6. restore.sh puts the
archive back before the databases.

The real scripts run against a stand-in ``docker`` (as in test_backup_moodle.py); the
archive is a real one, written by the worker's export code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts/backup"


def _openssl3() -> bool:
    exe = shutil.which("openssl")
    return bool(exe) and subprocess.run([exe, "version"], capture_output=True, text=True).stdout.startswith("OpenSSL 3")


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not _openssl3(), reason="needs bash and OpenSSL 3 (pkeyutl -rawin)"
)

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
all="$*"
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
    *" ps --services"*) printf 'postgres\nminio\nworker-scenario\n' ;;
    *" stop "*|*" start "*) : ;;
    *pg_dumpall*) echo "-- roles" ;;
    *"select datname"*) printf 'app\n' ;;
    *"show server_version"*) echo 16.4 ;;
    *"pg_dump "*) echo DUMP ;;
    *"pg_restore --list"*) cat >/dev/null; echo "; archive" ;;
    *"pg_restore "*) cat >/dev/null ;;
    *"exec -T postgres psql"*) cat >/dev/null ;;
    *) echo "fake docker: unhandled: $all" >&2; exit 99 ;;
esac
"""


def _make_archive(path: Path) -> int:
    """Three days of real signed exports (worker code, SQLite). Returns the manifest count."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from worker import audit_signing, audit_tasks
    from worker.audit_archive import FilesystemArchive
    from worker.tables import audit_exports, audit_logs

    eng = sa.create_engine("sqlite://", poolclass=StaticPool)
    audit_logs.metadata.create_all(eng, tables=[audit_logs, audit_exports])
    today = date(2026, 10, 12)
    with Session(eng) as db:
        for n in (3, 2, 1):
            db.execute(audit_logs.insert().values(
                id=uuid.uuid4(), timestamp=datetime.combine(today - timedelta(days=n), time(9), tzinfo=UTC),
                user_id=None, tenant_id=None, action="probe", resource_type="probe", resource_id=str(n), detail=None))
        db.commit()
        written = audit_tasks.run_export(db, FilesystemArchive(path), audit_signing.from_seed("b" * 40), today)
    return len(written)


@pytest.fixture
def stack(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "logger").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp_path / "compose.yml").write_text("services: {}\n")
    (tmp_path / "env").write_text("POSTGRES_USER=u\nPOSTGRES_DB=app\nMINIO_ACCESS_KEY=k\nMINIO_SECRET_KEY=s\n")
    archive = tmp_path / "audit-archive"
    count = _make_archive(archive)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "COMPOSE_FILE": str(tmp_path / "compose.yml"),
        "ENV_FILE": str(tmp_path / "env"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_MIN_FREE_GB": "0",
        "BACKUP_ESCROW": "out-of-band",
        "AUDIT_ARCHIVE_PATH": str(archive),
        "_MANIFESTS": str(count),
    }


def _run(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(BACKUP / script), *args], env={k: v for k, v in env.items() if k[0] != "_"},
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


def _the_backup(env: dict) -> Path:
    [backup] = [p for p in Path(env["BACKUP_DIR"]).glob("truenorth-backup-*") if p.is_dir()]
    return backup


def test_backup_copies_and_verifies_the_archive(stack):
    run = _run("backup.sh", stack)
    assert run.returncode == 0, run.stdout + run.stderr
    backup = _the_backup(stack)
    entry = json.loads((backup / "manifest.json").read_text())["audit_archive"]
    assert entry["status"] == "backed-up" and entry["verified"] is True
    assert entry["manifests"] == int(stack["_MANIFESTS"]) == 3 and entry["last_day"] == "2026-10-11"
    copied = sorted(p.relative_to(backup / "audit-archive").as_posix()
                    for p in (backup / "audit-archive").rglob("*") if p.is_file())
    original = sorted(p.relative_to(stack["AUDIT_ARCHIVE_PATH"]).as_posix()
                      for p in Path(stack["AUDIT_ARCHIVE_PATH"]).rglob("*") if p.is_file())
    assert copied == original and entry["files"] == len(original)
    sums = (backup / "SHA256SUMS").read_text()
    assert "./audit-archive/2026/10/audit-2026-10-11.manifest.json" in sums


def test_a_tampered_archive_is_kept_alerted_and_exits_6(stack):
    target = Path(stack["AUDIT_ARCHIVE_PATH"]) / "2026/10/audit-2026-10-10.jsonl.gz"
    target.write_bytes(target.read_bytes() + b"\0")
    run = _run("backup.sh", stack)
    assert run.returncode == 6, run.stdout + run.stderr
    assert "audit export archive does NOT verify" in run.stderr
    entry = json.loads((_the_backup(stack) / "manifest.json").read_text())["audit_archive"]
    assert entry["verified"] is False and "SHA-256" in entry["error"]


def test_a_forged_signature_exits_6(stack):
    sig = Path(stack["AUDIT_ARCHIVE_PATH"]) / "2026/10/audit-2026-10-09.manifest.json.sig"
    sig.write_bytes(bytes(64))
    run = _run("backup.sh", stack)
    assert run.returncode == 6 and "signature does not verify" in run.stderr


def test_without_the_variable_nothing_audit_runs(stack):
    env = {k: v for k, v in stack.items() if k != "AUDIT_ARCHIVE_PATH"}
    assert _run("backup.sh", env).returncode == 0
    backup = _the_backup(env)
    assert json.loads((backup / "manifest.json").read_text())["audit_archive"] == {"status": "not-configured"}
    assert not (backup / "audit-archive").exists()


def test_restore_puts_the_archive_back_before_the_databases(stack):
    assert _run("backup.sh", stack).returncode == 0
    backup = _the_backup(stack)
    live = Path(stack["AUDIT_ARCHIVE_PATH"])
    shutil.rmtree(live)
    run = _run("restore.sh", stack, str(backup), "--force")
    assert run.returncode == 0, run.stdout + run.stderr
    assert (live / "2026/10/audit-2026-10-11.manifest.json.sig").is_file()
    from worker.audit_archive import FilesystemArchive
    from worker.audit_verify import verify_archive

    problems, summary = verify_archive(FilesystemArchive(live))
    assert problems == [] and summary["manifests"] == 3
    out = run.stdout + run.stderr
    assert out.index("audit: archive restored") < out.index("Restoring database app")
