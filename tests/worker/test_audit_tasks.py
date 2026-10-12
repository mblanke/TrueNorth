"""Signed daily audit export, chain, verification and retention (worker/audit_tasks.py, ADR 0011).

SQLite here, for the logic; the database triggers and the purge path through them are in
tests/api/test_audit_append_only_postgres.py. A tmp-dir archive and a fixed seed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from worker import audit_export, audit_signing, audit_tasks, audit_verify
from worker.audit_archive import AuditArchiveError, FilesystemArchive, get_archive
from worker.tables import audit_exports, audit_logs

SEED = "test-seed-" + "x" * 40
TODAY = date(2026, 10, 12)


@pytest.fixture
def db():
    eng = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    audit_logs.metadata.create_all(eng, tables=[audit_logs, audit_exports])
    with Session(eng) as session:
        yield session
    eng.dispose()


@pytest.fixture
def archive(tmp_path):
    return FilesystemArchive(tmp_path / "archive")


@pytest.fixture
def key():
    return audit_signing.from_seed(SEED)


@pytest.fixture(autouse=True)
def hot_days(monkeypatch):
    monkeypatch.delenv("AUDIT_HOT_RETENTION_DAYS", raising=False)


def add(db, day: date, n: int = 1, hour: int = 12, action: str = "probe") -> list[uuid.UUID]:
    ids = []
    for i in range(n):
        rid = uuid.uuid4()
        when = datetime.combine(day, time(hour, 0, i % 60), tzinfo=UTC)
        db.execute(audit_logs.insert().values(id=rid, timestamp=when, user_id=None, tenant_id=None, action=action,
                                              resource_type="probe", resource_id=str(i), detail=None))
        ids.append(rid)
    db.commit()
    return ids


def exports(db) -> list:
    return list(db.execute(sa.select(audit_exports).order_by(audit_exports.c.day, audit_exports.c.seq)))


# ── signing ────────────────────────────────────────────────────────────
def test_key_is_derived_from_the_seed_and_stable():
    a, b = audit_signing.from_seed(SEED), audit_signing.from_seed(SEED)
    assert a.key_id == b.key_id and re.fullmatch(r"[0-9a-f]{16}", a.key_id)
    assert audit_signing.from_seed(SEED + "!").key_id != a.key_id


@pytest.mark.parametrize("seed", ["", "short", "x" * 31])
def test_missing_or_short_seed_raises(seed):
    with pytest.raises(audit_signing.SigningKeyError):
        audit_signing.from_seed(seed)


def test_the_task_writes_nothing_without_a_seed(monkeypatch):
    monkeypatch.delenv("AUDIT_SIGNING_SEED", raising=False)

    def no_db():
        raise AssertionError("the database must not be touched without a key")

    monkeypatch.setattr(audit_tasks, "_db_session", no_db)
    with pytest.raises(audit_signing.SigningKeyError):
        audit_tasks.export_audit_days.run()
    with pytest.raises(audit_signing.SigningKeyError):
        audit_tasks.enforce_audit_retention.run()


# ── export ─────────────────────────────────────────────────────────────
def test_export_writes_file_manifest_and_signature(db, archive, key):
    d = TODAY - timedelta(days=1)
    ids = add(db, d, 3)
    [w] = audit_tasks.run_export(db, archive, key, TODAY)
    assert (w.day, w.seq, w.row_count) == (d, 0, 3)
    stem = f"2026/10/audit-{d.isoformat()}"
    for suffix in (".jsonl.gz", ".manifest.json", ".manifest.json.sig"):
        assert archive.exists(stem + suffix), suffix
    assert archive.exists(f"keys/{key.key_id}.pub.pem")
    manifest = json.loads(archive.get(stem + ".manifest.json"))
    assert manifest["schema"] == 1 and manifest["row_count"] == 3 and manifest["key_id"] == key.key_id
    assert manifest["prev_manifest_sha256"] is None
    assert audit_export.decode_ids(archive.get(stem + ".jsonl.gz")) == [str(i) for i in ids]
    assert audit_export.verify_manifest(archive, stem + ".manifest.json") == manifest
    [row] = exports(db)
    assert row.manifest_sha256 == audit_export.sha256(archive.get(stem + ".manifest.json"))


def _openssl3() -> str | None:
    exe = shutil.which("openssl")
    if not exe:
        return None
    out = subprocess.run([exe, "version"], capture_output=True, text=True).stdout
    return exe if out.startswith("OpenSSL 3") else None


def test_openssl_verifies_the_signature(db, archive, key, tmp_path):
    exe = _openssl3()
    if not exe:
        pytest.skip("needs OpenSSL 3 (pkeyutl -rawin)")
    add(db, TODAY - timedelta(days=1))
    audit_tasks.run_export(db, archive, key, TODAY)
    root = archive.root
    manifest = next(root.rglob("*.manifest.json"))
    pub = root / f"keys/{key.key_id}.pub.pem"
    cmd = [exe, "pkeyutl", "-verify", "-pubin", "-inkey", str(pub), "-rawin", "-in", str(manifest),
           "-sigfile", f"{manifest}.sig"]
    ok = subprocess.run(cmd, capture_output=True, text=True)
    assert ok.returncode == 0 and "Signature Verified Successfully" in ok.stdout, ok.stdout + ok.stderr
    tampered = tmp_path / "tampered.json"
    tampered.write_bytes(manifest.read_bytes().replace(b'"row_count":1', b'"row_count":2'))
    bad = subprocess.run([*cmd[:-4], "-in", str(tampered), "-sigfile", f"{manifest}.sig"], capture_output=True)
    assert bad.returncode != 0


def test_days_are_chained_and_empty_days_get_a_manifest(db, archive, key):
    add(db, TODAY - timedelta(days=3), 2)
    add(db, TODAY - timedelta(days=1), 1)  # TODAY-2 has no rows
    written = audit_tasks.run_export(db, archive, key, TODAY)
    assert [(w.day, w.row_count) for w in written] == [
        (TODAY - timedelta(days=3), 2), (TODAY - timedelta(days=2), 0), (TODAY - timedelta(days=1), 1)]
    rows = exports(db)
    assert rows[0].prev_manifest_sha256 is None
    assert rows[1].prev_manifest_sha256 == rows[0].manifest_sha256
    assert rows[2].prev_manifest_sha256 == rows[1].manifest_sha256
    problems, summary = audit_verify.verify_archive(archive)
    assert problems == [] and summary["manifests"] == 3 and summary["rows"] == 3


def test_catch_up_after_downtime_exports_every_missing_day(db, archive, key):
    add(db, TODAY - timedelta(days=10))
    audit_tasks.run_export(db, archive, key, TODAY - timedelta(days=8))  # last run 8 days ago
    assert [r.day for r in exports(db)] == [TODAY - timedelta(days=10), TODAY - timedelta(days=9)]
    add(db, TODAY - timedelta(days=1))
    audit_tasks.run_export(db, archive, key, TODAY)
    assert [r.day for r in exports(db)] == [TODAY - timedelta(days=n) for n in range(10, 0, -1)]
    assert audit_verify.verify_archive(archive)[0] == []


def test_today_is_never_exported_and_a_rerun_changes_nothing(db, archive, key):
    add(db, TODAY)
    add(db, TODAY - timedelta(days=1))
    audit_tasks.run_export(db, archive, key, TODAY)
    assert [r.day for r in exports(db)] == [TODAY - timedelta(days=1)]
    assert audit_tasks.run_export(db, archive, key, TODAY) == []


def test_a_late_commit_is_exported_as_a_supplement(db, archive, key, caplog):
    d2 = TODAY - timedelta(days=2)
    add(db, d2, 2)
    audit_tasks.run_export(db, archive, key, TODAY - timedelta(days=1))  # exports d2
    late = add(db, d2, 1, hour=23)  # committed after d2 was exported
    written = audit_tasks.run_export(db, archive, key, TODAY)
    supplement = [w for w in written if w.seq == 1]
    assert supplement and supplement[0].day == d2 and supplement[0].row_count == 1
    assert "AUDIT_EXPORT_MISMATCH" in caplog.text
    assert audit_export.decode_ids(archive.get(audit_export.file_name(d2, 1))) == [str(late[0])]
    assert audit_verify.verify_archive(archive)[0] == []  # the supplement is on the chain


def test_an_interrupted_export_is_adopted_on_rerun(db, archive, key, monkeypatch):
    d = TODAY - timedelta(days=1)
    add(db, d, 2)
    real_execute = db.execute

    def crash_on_record(stmt, *a, **k):
        if getattr(stmt, "table", None) is audit_exports:
            raise RuntimeError("crash after the archive, before the database")
        return real_execute(stmt, *a, **k)

    monkeypatch.setattr(db, "execute", crash_on_record)
    with pytest.raises(RuntimeError):
        audit_tasks.run_export(db, archive, key, TODAY)
    monkeypatch.setattr(db, "execute", real_execute)
    db.rollback()
    assert exports(db) == [] and archive.exists(audit_export.manifest_name(d, 0))
    [w] = audit_tasks.run_export(db, archive, key, TODAY)
    assert w.row_count == 2 and len(exports(db)) == 1


def test_archive_files_are_never_replaced(archive):
    archive.put("a/b.txt", b"one")
    archive.put("a/b.txt", b"one")  # identical: fine
    with pytest.raises(AuditArchiveError):
        archive.put("a/b.txt", b"two")
    with pytest.raises(AuditArchiveError):
        archive.put("../escape", b"x")
    assert oct((archive.root / "a/b.txt").stat().st_mode & 0o777) == "0o640"


def test_unknown_backend_is_refused(monkeypatch):
    monkeypatch.setenv("AUDIT_ARCHIVE_BACKEND", "s3")
    with pytest.raises(AuditArchiveError):
        get_archive()


# ── verification ───────────────────────────────────────────────────────
def test_a_changed_archive_byte_fails_verification(db, archive, key):
    d = TODAY - timedelta(days=1)
    add(db, d, 2)
    audit_tasks.run_export(db, archive, key, TODAY)
    path = archive.root / audit_export.file_name(d, 0)
    data = bytearray(path.read_bytes())
    data[-5] ^= 0x01
    path.write_bytes(bytes(data))
    problems, _ = audit_verify.verify_archive(archive)
    assert any("file_sha256" in p for p in problems)


def test_a_missing_day_breaks_the_chain(db, archive, key):
    for n in (3, 2, 1):
        add(db, TODAY - timedelta(days=n))
    audit_tasks.run_export(db, archive, key, TODAY)
    middle = archive.root / audit_export.manifest_name(TODAY - timedelta(days=2), 0)
    middle.unlink()
    (archive.root / (audit_export.manifest_name(TODAY - timedelta(days=2), 0) + ".sig")).unlink()
    problems, _ = audit_verify.verify_archive(archive)
    assert any("chain broken" in p for p in problems)


def test_a_forged_signature_fails(db, archive, key):
    d = TODAY - timedelta(days=1)
    add(db, d)
    audit_tasks.run_export(db, archive, key, TODAY)
    other = audit_signing.from_seed("another-seed-" + "y" * 40)
    name = audit_export.manifest_name(d, 0)
    (archive.root / (name + ".sig")).write_bytes(other.sign(archive.get(name)))
    assert any("signature" in p for p in audit_verify.verify_archive(archive)[0])


def test_verify_database_matches_counts(db, archive, key):
    d = TODAY - timedelta(days=1)
    add(db, d, 2)
    audit_tasks.run_export(db, archive, key, TODAY)
    assert audit_verify.verify_database(db, archive, TODAY) == []
    db.execute(audit_logs.delete())  # SQLite has no trigger: simulate tampering
    db.commit()
    assert any("0 rows in audit_logs, 2 exported" in p for p in audit_verify.verify_database(db, archive, TODAY))


# ── retention ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(("value", "days"), [(None, 90), ("10", 30), ("45", 45), ("0", 0), ("-1", 0)])
def test_hot_retention_is_clamped_to_30(monkeypatch, value, days):
    if value is not None:
        monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", value)
    assert audit_tasks.hot_retention_days() == days


def test_retention_deletes_only_exported_days_older_than_the_cutoff(db, archive, key, monkeypatch):
    monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", "30")
    old, edge, recent = TODAY - timedelta(days=40), TODAY - timedelta(days=30), TODAY - timedelta(days=5)
    for d in (old, edge, recent):
        add(db, d, 2)
    audit_tasks.run_export(db, archive, key, TODAY)
    purged = audit_tasks.run_retention(db, archive, key, TODAY)
    assert [p["day"] for p in purged] == [old.isoformat()]
    assert audit_export.day_count(db, old) == 0
    assert audit_export.day_count(db, edge) == 2 and audit_export.day_count(db, recent) == 2
    [note] = db.execute(sa.select(audit_logs).where(audit_logs.c.action == "audit.purge")).all()
    assert note.resource_id == old.isoformat() and json.loads(note.detail)["rows"] == 2
    assert audit_verify.verify_database(db, archive, TODAY) == []  # purged, and older than the floor


def test_retention_off_with_zero(db, archive, key, monkeypatch):
    monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", "0")
    add(db, TODAY - timedelta(days=400))
    audit_tasks.run_export(db, archive, key, TODAY)
    assert audit_tasks.run_retention(db, archive, key, TODAY) == []
    assert audit_export.day_count(db, TODAY - timedelta(days=400)) == 1


def test_retention_stops_at_a_day_without_an_export(db, archive, key, caplog):
    add(db, TODAY - timedelta(days=200))  # never exported
    assert audit_tasks.run_retention(db, archive, key, TODAY) == []
    assert "AUDIT_RETENTION_STOPPED" in caplog.text and "no export" in caplog.text


def test_retention_stops_when_the_archive_does_not_verify(db, archive, key, caplog):
    d = TODAY - timedelta(days=120)
    add(db, d, 2)
    add(db, d + timedelta(days=1), 1)
    audit_tasks.run_export(db, archive, key, d + timedelta(days=2))
    path = archive.root / audit_export.file_name(d, 0)
    path.write_bytes(path.read_bytes() + b"x")
    assert audit_tasks.run_retention(db, archive, key, TODAY) == []
    assert audit_export.day_count(db, d) == 2 and audit_export.day_count(db, d + timedelta(days=1)) == 1
    assert "AUDIT_RETENTION_STOPPED" in caplog.text


def test_retention_stops_on_a_count_mismatch(db, archive, key, caplog):
    d = TODAY - timedelta(days=120)
    add(db, d, 2)
    audit_tasks.run_export(db, archive, key, d + timedelta(days=1))
    add(db, d, 1, hour=23)  # a row the export does not hold (no supplement ran for it)
    assert audit_tasks.run_retention(db, archive, key, TODAY) == []
    assert audit_export.day_count(db, d) == 3
    assert "3 rows in the table, 2 exported" in caplog.text


# ── beat ───────────────────────────────────────────────────────────────
def test_beat_runs_both_daily_with_expiry_on_a_consumed_queue():
    from pathlib import Path

    import yaml
    from worker.celery_app import app

    sched = app.conf.beat_schedule
    for entry, task in (("audit-export-daily", "worker.audit_tasks.export_audit_days"),
                        ("audit-retention-daily", "worker.audit_tasks.enforce_audit_retention")):
        assert sched[entry]["task"] == task
        assert sched[entry]["options"]["expires"] > 0
        assert task in app.tasks
    routes = app.conf.task_routes
    queue = routes["worker.audit_tasks.*"]["queue"]
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "infra/platform/docker/compose.prod.yml").read_text())
    consumed = set()
    for svc in compose["services"].values():
        cmd = svc.get("command") or ""
        m = re.search(r"-Q\s+(\S+)", cmd if isinstance(cmd, str) else " ".join(cmd))
        if m:
            consumed.update(m.group(1).split(","))
    assert queue in consumed
