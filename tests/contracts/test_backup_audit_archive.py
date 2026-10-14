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
if [ "$1" = run ] && [[ "$all" == *":/archive"* ]]; then   # lib.sh audit_archive_tool
    host=""; prev=""; script=""; args=()
    for a in "$@"; do
        [ "$prev" = "-v" ] && { host="${a%%:/archive*}"; }
        [ "$prev" = "-c" ] && script="$a"
        prev="$a"
    done
    seen=0
    for a in "$@"; do
        if (( seen )); then args+=("$a"); fi
        [ "$a" = audit-archive ] && seen=1
    done
    case "$script" in
        *"--numeric-owner -cf - ."*) COPYFILE_DISABLE=1 tar -C "$host" -cf - . ;;
        *"stat -c %u:%g /archive"*"tar -C /archive --no-same-owner -xpf -"*) mkdir -p "$host"; tar -C "$host" -xpf - ;;
        *'q="/archive/quarantine/$1"'*)
            q="$host/quarantine/${args[0]}"; mkdir -p "$q"
            while IFS= read -r f; do
                [ -n "$f" ] && [ -e "$host/$f" ] || continue
                mkdir -p "$(dirname "$q/$f")"; mv -- "$host/$f" "$q/$f"
            done
            printf '%s\n' "${args[1]}" > "$q/RESTORE.json" ;;
        *"sha256sum"*)
            (cd "$host" && find . -path ./quarantine -prune -o -type f -name '*.manifest.json' -print | sort \
             | while IFS= read -r m; do
                m="${m#./}"
                s="$(openssl dgst -sha256 -r "$m" | cut -d' ' -f1)"
                f="$(sed -n 's/.*"file":"\([^"]*\)".*/\1/p' "$m")"
                printf '%s\t%s\t%s\n' "$m" "$s" "$f"
             done) ;;
        *"stat -c %u:%g /archive"*) echo 10001:10001 ;;
        *) echo "fake docker: unhandled archive script: $script" >&2; exit 99 ;;
    esac
    exit $?
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
    *" ps --services"*) printf 'postgres\nminio\nworker-scenario\n' ;;
    *" stop "*|*" start "*) : ;;
    *pg_dumpall*) echo "-- roles" ;;
    *"select datname"*) printf 'app\n' ;;
    *"show server_version"*) echo 16.4 ;;
    *"pg_dump "*) echo DUMP ;;
    *"pg_restore --list"*) cat >/dev/null; echo "; archive" ;;
    *"pg_restore "*) cat >/dev/null ;;
    *"to_regclass('public.audit_exports')"*) echo "${FAKE_AUDIT_TABLE:-f}" ;;
    *"from audit_exports"*) cat "$FAKE_AUDIT_ROWS" ;;
    *"-v qts="*) { echo "INSERT $all"; cat; } >> "$FAKE_LOG.inserts" ;;
    *"exec -T postgres psql"*) cat >/dev/null ;;
    *) echo "fake docker: unhandled: $all" >&2; exit 99 ;;
esac
"""


def _make_archive(path: Path) -> int:
    """Three days of real signed exports (worker code, SQLite). Returns the manifest count."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from worker import audit_jobs, audit_signing
    from worker.audit_archive import FilesystemArchive
    from worker.tables import audit_export_checks, audit_exports, audit_logs

    eng = sa.create_engine("sqlite://", poolclass=StaticPool)
    audit_logs.metadata.create_all(eng, tables=[audit_logs, audit_exports, audit_export_checks])
    today = date(2026, 10, 12)
    with Session(eng) as db:
        for n in (3, 2, 1):
            db.execute(audit_logs.insert().values(
                id=uuid.uuid4(), timestamp=datetime.combine(today - timedelta(days=n), time(9), tzinfo=UTC),
                user_id=None, tenant_id=None, action="probe", resource_type="probe", resource_id=str(n), detail=None))
        db.commit()
        written = audit_jobs.run_export(db, FilesystemArchive(path), audit_signing.from_seed("b" * 40), today)
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
    rows = tmp_path / "audit_rows.txt"
    rows.write_text(_db_rows(archive))
    return {
        "FAKE_AUDIT_ROWS": str(rows),
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


def _db_rows(archive: Path) -> str:
    """audit_exports as the database holds it for this archive: day|seq|manifest_sha256|key_id."""
    import hashlib

    lines = []
    for m in sorted(p for p in archive.rglob("*.manifest.json") if "quarantine" not in p.parts):
        doc = json.loads(m.read_bytes())
        lines.append(f"{doc['day']}|{doc['seq']}|{hashlib.sha256(m.read_bytes()).hexdigest()}|{doc['key_id']}")
    return "\n".join(lines) + "\n"


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



# ── Review S3-1/S3-2/S3-3: the database is the reference ──────────────
def test_backup_compares_the_archive_with_audit_exports(stack):
    env = {**stack, "FAKE_AUDIT_TABLE": "t"}
    run = _run("backup.sh", env)
    assert run.returncode == 0, run.stdout + run.stderr
    entry = json.loads((_the_backup(env) / "manifest.json").read_text())["audit_archive"]
    assert entry["db_checked"] is True and entry["not_in_db"] == 0 and entry["verified"] is True


def test_a_deleted_newest_day_is_caught(stack):
    """The chain alone cannot see a missing LAST day; audit_exports can."""
    newest = Path(stack["AUDIT_ARCHIVE_PATH"]) / "2026/10/audit-2026-10-11.manifest.json"
    newest.unlink()
    Path(str(newest) + ".sig").unlink()
    run = _run("backup.sh", {**stack, "FAKE_AUDIT_TABLE": "t"})
    assert run.returncode == 6 and "has no manifest with its hash" in run.stderr


def test_a_key_the_database_never_recorded_is_caught(stack):
    rows = Path(stack["FAKE_AUDIT_ROWS"])
    rows.write_text("".join(line.rsplit("|", 1)[0] + "|0000000000000000\n" for line in rows.read_text().splitlines()))
    run = _run("backup.sh", {**stack, "FAKE_AUDIT_TABLE": "t"})
    assert run.returncode == 6 and "audit_exports never recorded" in run.stderr


def test_a_broken_chain_is_caught_without_the_database(stack):
    middle = Path(stack["AUDIT_ARCHIVE_PATH"]) / "2026/10/audit-2026-10-10.manifest.json"
    middle.unlink()
    Path(str(middle) + ".sig").unlink()
    run = _run("backup.sh", stack)
    assert run.returncode == 6 and "chain broken" in run.stderr


def test_restore_quarantines_archive_days_the_restored_database_does_not_list(stack):
    env = {**stack, "FAKE_AUDIT_TABLE": "t"}
    assert _run("backup.sh", env).returncode == 0
    backup = _the_backup(env)
    # After the backup the worker exported another day; then the database is restored.
    live = Path(stack["AUDIT_ARCHIVE_PATH"])
    rows = Path(stack["FAKE_AUDIT_ROWS"]).read_text()
    _extend(live)
    run = _run("restore.sh", env, str(backup), "--force")
    assert run.returncode == 0, run.stdout + run.stderr
    [record] = list((live / "quarantine").rglob("RESTORE.json"))
    doc = json.loads(record.read_text())
    assert doc["moved"] == ["2026/10/audit-2026-10-12.manifest.json"]
    qdir = record.parent
    assert (qdir / "2026/10/audit-2026-10-12.manifest.json.sig").is_file()
    assert (qdir / "2026/10/audit-2026-10-12.jsonl.gz").is_file()
    assert not (live / "2026/10/audit-2026-10-12.manifest.json").exists()
    from worker.audit_archive import FilesystemArchive
    from worker.audit_verify import verify_archive

    assert verify_archive(FilesystemArchive(live))[0] == []
    assert Path(stack["FAKE_LOG"] + ".inserts").read_text().count("audit.archive_quarantine") == 1
    assert _db_rows(live) == rows  # the archive now matches the restored database
    assert "moved to" in run.stdout + run.stderr


def test_restore_leaves_an_agreeing_archive_alone(stack):
    env = {**stack, "FAKE_AUDIT_TABLE": "t"}
    assert _run("backup.sh", env).returncode == 0
    run = _run("restore.sh", env, str(_the_backup(env)), "--force")
    assert run.returncode == 0 and "archive and restored database agree" in run.stdout + run.stderr
    assert not (Path(stack["AUDIT_ARCHIVE_PATH"]) / "quarantine").exists()


def _extend(archive: Path) -> None:
    """One more signed export after the last day, as the worker would write it."""
    import subprocess as sp
    import sys

    r = sp.run([sys.executable, str(BACKUP / "drill_audit_archive.py"), "extend", str(archive)],
               capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cron_backup_uploads_and_rotates_on_exit_6_with_a_security_alert(stack, tmp_path):
    scripts = tmp_path / "scripts-copy"
    shutil.copytree(BACKUP, scripts)
    (scripts / "s3-backup.sh").write_text('#!/usr/bin/env bash\necho "UPLOADED $1" >> "$FAKE_LOG.s3"\n')
    target = Path(stack["AUDIT_ARCHIVE_PATH"]) / "2026/10/audit-2026-10-10.jsonl.gz"
    target.write_bytes(target.read_bytes() + b"\0")
    env = {k: v for k, v in stack.items() if k[0] != "_"}
    env.update(S3_BUCKET="s3://bucket/tn", DAILY_KEEP="7")
    run = subprocess.run(["bash", str(scripts / "cron-backup.sh")], env=env, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, timeout=120)
    assert run.returncode == 6, run.stdout + run.stderr
    assert "UPLOADED" in Path(stack["FAKE_LOG"] + ".s3").read_text()
    assert "SECURITY: the audit export archive does NOT verify" in run.stderr
    assert "Cron backup complete" in run.stdout + run.stderr


# ── Review 2 ───────────────────────────────────────────────────────────
def test_escrow_missing_and_a_tampered_archive_both_show(stack, tmp_path):
    """Exit 3 wins over 6, so the archive has its own gauge, and cron its own notice."""
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    target = Path(stack["AUDIT_ARCHIVE_PATH"]) / "2026/10/audit-2026-10-10.jsonl.gz"
    target.write_bytes(target.read_bytes() + b"\0")
    env = {k: v for k, v in stack.items() if k != "BACKUP_ESCROW"}
    env["METRICS_DIR"] = str(metrics)
    run = _run("backup.sh", env)
    assert run.returncode == 3, run.stdout + run.stderr
    prom = (metrics / "truenorth_backup.prom").read_text()
    assert "truenorth_backup_audit_verified 0" in prom and "truenorth_backup_last_exit_code 3" in prom


def test_a_verified_archive_sets_the_gauge_to_1(stack, tmp_path):
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    assert _run("backup.sh", {**stack, "METRICS_DIR": str(metrics)}).returncode == 0
    assert "truenorth_backup_audit_verified 1" in (metrics / "truenorth_backup.prom").read_text()


def test_cron_sends_the_security_notice_on_exit_3_with_a_tampered_archive(stack, tmp_path):
    scripts = tmp_path / "scripts-copy"
    shutil.copytree(BACKUP, scripts)
    target = Path(stack["AUDIT_ARCHIVE_PATH"]) / "2026/10/audit-2026-10-10.jsonl.gz"
    target.write_bytes(target.read_bytes() + b"\0")
    env = {k: v for k, v in stack.items() if k[0] != "_" and k != "BACKUP_ESCROW"}
    run = subprocess.run(["bash", str(scripts / "cron-backup.sh")], env=env, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, timeout=120)
    assert run.returncode == 3, run.stdout + run.stderr
    assert "SECURITY: the audit export archive does NOT verify (backup exit 3)" in run.stderr


def test_restore_never_moves_a_file_a_manifest_points_outside_its_day(stack, tmp_path):
    """The manifest is untrusted and restore.sh runs as root: a traversal 'file' is ignored."""
    env = {**stack, "FAKE_AUDIT_TABLE": "t"}
    assert _run("backup.sh", env).returncode == 0
    backup = _the_backup(env)
    live = Path(stack["AUDIT_ARCHIVE_PATH"])
    victim = tmp_path / "victim.txt"
    victim.write_text("root-owned file elsewhere")
    evil = live / "2026/10/audit-2026-10-30.manifest.json"  # not in audit_exports: quarantined
    rel_victim = os.path.relpath(victim, live)
    evil.write_text(json.dumps({"day": "2026-10-30", "seq": 0, "file": rel_victim, "file_sha256": "0" * 64,
                                "key_id": "0" * 16}, separators=(",", ":")))
    run = _run("restore.sh", env, str(backup), "--force")
    assert run.returncode == 0, run.stdout + run.stderr
    assert victim.read_text() == "root-owned file elsewhere"
    assert "names an unexpected data file" in run.stdout + run.stderr
    assert not evil.exists()  # the manifest itself was quarantined


def test_the_newest_manifest_may_carry_a_key_not_yet_recorded(stack):
    """Just after a rotation, the head of the chain is written before its audit_exports row."""
    _extend(Path(stack["AUDIT_ARCHIVE_PATH"]))  # signed by another key, not in the rows
    run = _run("backup.sh", {**stack, "FAKE_AUDIT_TABLE": "t"})
    assert run.returncode == 0, run.stdout + run.stderr
    entry = json.loads((_the_backup(stack) / "manifest.json").read_text())["audit_archive"]
    assert entry["verified"] is True and entry["not_in_db"] == 1
