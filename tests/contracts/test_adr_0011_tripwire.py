"""Tripwires for ADR 0011 (audit log integrity): each test pins one decision of the ADR
(docs/adr/0011-audit-log-integrity.md) and turns red the day the code drifts from it.

Structure is asserted where the enforcement is a PostgreSQL trigger (no live server here;
the behavioural version is tests/api/test_audit_append_only_postgres.py, skipped without
TEST_POSTGRES_ADMIN_URL) and behaviour is exercised in-process where the code is plain
Python: the worker's export, verification and retention run on SQLite (worker/tables.py
Core tables) against a throwaway archive directory, with no mocks of the code under test
and no network. One test per enforceable ADR decision; the docstring quotes the line.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from celery.schedules import crontab
from sqlalchemy.orm import Session
from worker import audit_export, audit_jobs, audit_signing
from worker.audit_archive import BACKENDS, AuditArchive, FilesystemArchive, get_archive
from worker.audit_archive.filesystem import DEFAULT_DIR, FILE_MODE
from worker.celery_app import app as celery_app
from worker.tables import audit_export_checks, audit_exports, audit_logs, metadata

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "control-plane/api/alembic/versions/b7c8d9e0f1a2_audit_log_append_only.py"
AUDIT_EVENTS_MIGRATION = ROOT / "control-plane/api/alembic/versions/d4e5f6a1b2c3_add_audit_events_table.py"
COMPOSE = yaml.safe_load((ROOT / "infra/platform/docker/compose.prod.yml").read_text())
OPENAPI = json.loads((ROOT / "docs/interfaces/openapi.json").read_text())

SEED = "k" * 32


@pytest.fixture
def key():
    return audit_signing.from_seed(SEED)


@pytest.fixture
def db():
    """A fresh SQLite session with the worker's Core tables (audit_logs, audit_exports...):
    the retention job commits and rolls back per day and asks the bind for its dialect, so
    it takes a Session, as in production."""
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def archive(tmp_path):
    return FilesystemArchive(tmp_path / "audit-archive")


def _on_day(today: date, offset: int, hour: int = 12) -> datetime:
    """Noon UTC of the day `offset` days from `today` — fixed instants, so the day a row
    lands on never depends on the wall clock at test time."""
    return datetime.combine(today + timedelta(days=offset), time(hour), tzinfo=UTC)


def _insert(db, when: datetime, action: str = "probe") -> None:
    db.execute(audit_logs.insert().values(
        id=uuid.uuid4(), timestamp=when, user_id=None, tenant_id=None, action=action,
        resource_type="probe", resource_id="x", detail=None,
    ))
    db.commit()


def _migration_source() -> str:
    return MIGRATION.read_text()


def _code_files() -> list[Path]:
    out = []
    for top in ("control-plane", "scripts", "install", "infra", "scenario-engine", "tools"):
        root = ROOT / top
        if root.is_dir():
            out.extend(p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".sh", ".j2", ".yml", ".yaml"))
    return out


# ── Decision 1: append-only by trigger ────────────────────────────────────────────
def test_decision_1_the_trigger_refuses_change_and_stamps_the_database_clock():
    """"`UPDATE` on `audit_logs` is refused; so is `TRUNCATE` of `audit_logs` and
    `audit_exports`; `audit_exports` rows are never updated or deleted." / "Every inserted
    row's `timestamp` is set to `clock_timestamp()`" (migration b7c8d9e0f1a2, PostgreSQL only)."""
    spec = importlib.util.spec_from_file_location("tn_adr0011_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "b7c8d9e0f1a2"
    src = _migration_source()
    # PostgreSQL only: the trigger DDL sits behind this guard.
    assert 'op.get_bind().dialect.name != "postgresql"' in src
    # Insert stamp: the writer's timestamp is replaced by the database clock.
    assert "NEW.timestamp := clock_timestamp()" in src
    assert "CREATE TRIGGER tn_audit_insert_stamp BEFORE INSERT ON audit_logs" in src
    # UPDATE is refused outright; DELETE only through the retention path (decision 2).
    guard = src[src.index("FUNCTION tn_audit_guard"):src.index("CREATE TRIGGER tn_audit_guard")]
    assert "IF TG_OP = 'UPDATE' THEN" in guard and "RAISE EXCEPTION" in guard
    assert "CREATE TRIGGER tn_audit_guard BEFORE UPDATE OR DELETE ON audit_logs" in src
    # audit_exports / audit_export_checks are never updated or deleted; nothing is truncated.
    for table in ("audit_exports", "audit_export_checks"):
        assert f"CREATE TRIGGER tn_{table}_guard BEFORE UPDATE OR DELETE ON {table}" in src
    assert 'for table in ("audit_logs", "audit_exports", "audit_export_checks"):' in src
    assert "CREATE TRIGGER tn_{table}_truncate_guard BEFORE TRUNCATE ON {table}" in src
    # The two row guards + the (looped) TRUNCATE guards all refuse through the same function.
    assert src.count("EXECUTE FUNCTION tn_refuse_change();") == 3
    # A check's checked_at is stamped by the database clock too.
    assert "NEW.checked_at := clock_timestamp()" in src


# ── Decision 2: one DELETE path, the retention job's ──────────────────────────────
def test_decision_2_the_trigger_deletes_only_after_the_purge_setting_age_and_fresh_check():
    """"the transaction set `tn.audit_purge = on` (`set_config(..., true)`: transaction-local);
    the row is at least **30 days** old (UTC days; hard-coded in the trigger, no setting
    lowers it); its UTC day has an `audit_exports` row **and** an `audit_export_checks` row
    for that export, with the same `manifest_sha256`, less than a day old."""
    src = _migration_source()
    guard = src[src.index("FUNCTION tn_audit_guard"):src.index("CREATE TRIGGER tn_audit_guard")]
    assert "current_setting('tn.audit_purge', true) IS DISTINCT FROM 'on'" in guard
    assert "interval '{PURGE_FLOOR_DAYS} days'" in guard  # hard-coded floor, no setting
    assert "PURGE_FLOOR_DAYS = 30" in src[:src.index("def upgrade")]
    assert "c.manifest_sha256 = e.manifest_sha256" in guard
    assert "c.checked_at > now() - interval '1 day'" in guard
    # The job's side of the same path: the setting is transaction-local (the third argument).
    assert 'set_config("tn.audit_purge", "on", True)' in inspect.getsource(audit_jobs._allow_purge)


def test_decision_2_retention_purges_a_verified_whole_day_and_writes_the_audit_purge_row(db, archive, key, monkeypatch):
    """"It deletes a whole day in one transaction, writes an `audit.purge` row (day, row
    count, manifest hashes) and stops at the first day that fails a check." Exercise the
    real job on SQLite: export every day, then purge what is older than the hot window."""
    monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", "30")
    today = datetime.now(UTC).date()
    _insert(db, _on_day(today, -31, 12), "old1")
    _insert(db, _on_day(today, -31, 13), "old2")
    _insert(db, _on_day(today, -10), "recent")

    written = audit_jobs.run_export(db, archive, key, today)
    counts = {w.day: w.row_count for w in written}
    assert list(counts) == [today - timedelta(days=n) for n in range(31, 0, -1)]
    assert counts[today - timedelta(days=31)] == 2  # the day's two rows ...
    assert counts[today - timedelta(days=10)] == 1  # ... the later day's row ...
    assert sum(1 for n in counts.values() if n == 0) == 29  # ... and every empty day too
    purged = audit_jobs.run_retention(db, archive, key, today)
    assert [(p["day"], p["rows"]) for p in purged] == [((today - timedelta(days=31)).isoformat(), 2)]
    left = db.execute(sa.text("SELECT action FROM audit_logs ORDER BY timestamp")).scalars().all()
    # The purged day's rows are gone; the job's own audit.purge row is the only addition.
    assert left == ["recent", "audit.purge"]
    # The purge row records the day, the row count and the manifest hashes.
    row = db.execute(sa.text(
        "SELECT resource_type, resource_id, detail FROM audit_logs WHERE action = 'audit.purge'")).one()
    assert row[0] == "audit_logs" and row[1] == (today - timedelta(days=31)).isoformat()
    assert json.loads(row[2])["rows"] == 2 and len(json.loads(row[2])["manifest_sha256"]) == 1
    # The check the trigger requires was written, matching the export's manifest hash.
    day = (today - timedelta(days=31)).isoformat()
    check = db.execute(sa.select(audit_export_checks.c.manifest_sha256)
                       .where(audit_export_checks.c.day == day)).one()
    export = db.execute(sa.select(audit_exports.c.manifest_sha256)
                        .where(audit_exports.c.day == day)).one()
    assert check[0] == export[0]


def test_decision_2_retention_stops_at_the_first_day_that_fails_a_check(db, archive, key, tmp_path, monkeypatch):
    """"...and stops at the first day that fails a check." A day whose archived file no
    longer hashes to its manifest stops the purge; the not-yet-reached day keeps its rows."""
    monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", "30")
    today = datetime.now(UTC).date()
    _insert(db, _on_day(today, -32, 12), "older")
    _insert(db, _on_day(today, -31, 12), "old")
    audit_jobs.run_export(db, archive, key, today)
    # Tamper with the second purgeable day's data file (after export, before retention).
    victim = tmp_path / "audit-archive" / audit_export.file_name(today - timedelta(days=31), 0)
    victim.write_bytes(b"tampered")
    purged = audit_jobs.run_retention(db, archive, key, today)
    assert [(p["day"], p["rows"]) for p in purged] == [((today - timedelta(days=32)).isoformat(), 1)]
    left = db.execute(sa.text("SELECT action FROM audit_logs ORDER BY timestamp")).scalars().all()
    assert left == ["old", "audit.purge"]  # the day that failed its check was not touched


# ── Decision 3: signed daily export ───────────────────────────────────────────────
def test_decision_3_exports_form_one_hash_chain_and_are_signed(db, archive, key):
    """"The manifests form **one hash chain** in the order they were written; a missing,
    reordered or edited manifest breaks it." and "an Ed25519 signature over the manifest's
    exact bytes"; the beat task runs the export at 00:45 UTC."""
    beat = {e["task"]: e["schedule"] for e in celery_app.conf.beat_schedule.values()}
    assert beat["worker.audit_tasks.export_audit_days"] == crontab(hour=0, minute=45)
    assert beat["worker.audit_tasks.enforce_audit_retention"] == crontab(hour=2, minute=15)

    today = datetime.now(UTC).date()
    _insert(db, _on_day(today, -2, 12), "day1")
    _insert(db, _on_day(today, -1, 12), "day2")
    audit_jobs.run_export(db, archive, key, today)
    name1 = audit_export.manifest_name(today - timedelta(days=2), 0)
    name2 = audit_export.manifest_name(today - timedelta(days=1), 0)
    m1 = json.loads(archive.get(name1))
    m2 = json.loads(archive.get(name2))
    assert m1["prev_manifest_sha256"] is None
    assert m2["prev_manifest_sha256"] == hashlib.sha256(archive.get(name1)).hexdigest()
    assert audit_export.chain_head(db) == hashlib.sha256(archive.get(name2)).hexdigest()
    # The signature is over the manifest's exact bytes and breaks on any edit.
    sig = archive.get(name2 + ".sig")
    assert audit_signing.verify(key.public_pem, audit_export.manifest_bytes(m2), sig)
    assert not audit_signing.verify(key.public_pem, audit_export.manifest_bytes(m2) + b" ", sig)


def test_decision_3_rows_that_commit_after_their_day_was_exported_go_into_a_supplement(db, archive, key, caplog):
    """"A recount of the day before yesterday catches rows that committed after their day
    was exported (long transactions): they go into a supplement (`seq` + 1), with an ERROR
    logged."""
    today = datetime.now(UTC).date()
    day = today - timedelta(days=2)
    _insert(db, _on_day(day, 0, 12), "in time")
    audit_jobs.write_export(db, archive, key, day, 0, audit_export.day_rows(db, day), None)
    _insert(db, _on_day(day, 0, 12) + timedelta(minutes=1), "late")
    with caplog.at_level(logging.ERROR, logger="truenorth.worker.audit"):
        supplement = audit_jobs.recount(db, archive, key, day)
    assert supplement is not None and (supplement.day, supplement.seq, supplement.row_count) == (day, 1, 1)
    assert "AUDIT_EXPORT_MISMATCH" in caplog.text


# ── Decision 4: archive behind an adapter ─────────────────────────────────────────
def test_decision_4_the_archive_is_an_adapter_registry_with_a_write_once_filesystem_backend(tmp_path, monkeypatch):
    """"`worker/audit_archive/`, registry by `AUDIT_ARCHIVE_BACKEND`, default `filesystem`
    (`AUDIT_ARCHIVE_DIR`, ... default `/var/lib/truenorth/audit-archive`). Writes are
    temp-file, fsync, rename; files 0640; a name is never overwritten with other bytes."""
    monkeypatch.delenv("AUDIT_ARCHIVE_BACKEND", raising=False)
    assert "filesystem" in BACKENDS
    assert isinstance(get_archive(), FilesystemArchive)
    with pytest.raises(audit_export.AuditArchiveError):
        get_archive("not-a-backend")
    assert DEFAULT_DIR == "/var/lib/truenorth/audit-archive"
    # The adapter interface is write-once: no delete for the retention job (or anyone) to call.
    assert not any(hasattr(AuditArchive, name) for name in ("delete", "remove", "unlink"))
    archive = FilesystemArchive(tmp_path / "fs-archive")
    name = "2026/10/audit-2026-10-11.jsonl.gz"
    archive.put(name, b"rows")
    assert (tmp_path / "fs-archive" / name).stat().st_mode & 0o777 == FILE_MODE == 0o640
    assert archive.list() == [name]  # no *.tmp left behind
    archive.put(name, b"rows")  # identical bytes: a no-op
    with pytest.raises(audit_export.AuditArchiveError):
        archive.put(name, b"other bytes")  # a name is never replaced with other content


# ── Decision 5: key handling ──────────────────────────────────────────────────────
def test_decision_5_the_signing_key_is_hkdf_of_the_seed_and_there_is_no_fallback(monkeypatch):
    """"Key = Ed25519 from HKDF-SHA256(seed, info `truenorth-audit-export-v1`); `key_id` =
    first 16 hex characters of SHA-256 of the raw public key. No fallback key: without a
    seed of 32+ characters nothing is exported or purged."""
    assert audit_signing.HKDF_INFO == b"truenorth-audit-export-v1"
    assert audit_signing.MIN_SEED_CHARS == 32
    # Derived independently of the code under test, from the formula in the ADR.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    material = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=b"truenorth-audit-export-v1").derive(SEED.encode())
    private = Ed25519PrivateKey.from_private_bytes(material)
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    expected_id = hashlib.sha256(raw).hexdigest()[:16]

    key = audit_signing.from_seed(SEED)
    assert key.key_id == expected_id == audit_signing.from_seed(SEED).key_id  # deterministic
    assert key.public_pem == private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(audit_signing.SigningKeyError):
        audit_signing.from_seed("s" * 31)
    monkeypatch.delenv(audit_signing.SEED_ENV, raising=False)
    with pytest.raises(audit_signing.SigningKeyError):  # no fallback key: load() refuses
        audit_signing.load()


def test_decision_5_only_the_export_worker_is_given_the_seed():
    """"`AUDIT_SIGNING_SEED`, an installer-generated secret ... is passed to the worker that
    runs the export (worker-scenario) and to no other service."""
    def env(service: str) -> dict:
        value = COMPOSE["services"][service].get("environment") or {}
        return value if isinstance(value, dict) else dict(e.split("=", 1) for e in value)

    holders = [name for name in COMPOSE["services"] if "AUDIT_SIGNING_SEED" in env(name)]
    assert holders == ["worker-scenario"]


# ── Decision 6: retention defaults ────────────────────────────────────────────────
def test_decision_6_retention_defaults_90_floor_30_and_zero_turns_purging_off(monkeypatch):
    """"`AUDIT_HOT_RETENTION_DAYS` 90 (floor 30; 0 turns purging off). Archive files are
    never deleted by TrueNorth; `AUDIT_ARCHIVE_RETENTION_DAYS` ... is not acted on."""
    assert audit_export.DEFAULT_HOT_DAYS == 90 and audit_export.FLOOR_DAYS == 30
    monkeypatch.delenv("AUDIT_HOT_RETENTION_DAYS", raising=False)
    assert audit_export.hot_retention_days() == 90
    for raw, expected in (("", 90), ("0", 0), ("10", 30), ("120", 120)):
        monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", raw)
        assert audit_export.hot_retention_days() == expected, raw
    # Not acted on: no code reads it (the string appears in docs only).
    for path in _code_files():
        assert "AUDIT_ARCHIVE_RETENTION_DAYS" not in path.read_text(errors="ignore"), path


# ── Decision 7: tenancy — the API serves no exports ───────────────────────────────
def test_decision_7_no_api_endpoint_serves_the_exports():
    """"Exports are platform-wide files with every tenant's rows. No API serves them in
    v1.2.0." The published contract has exactly one audit path (the row query), and no
    router module reaches the archive or the export code."""
    assert [p for p in OPENAPI["paths"] if "audit" in p.lower()] == ["/audit-log"]
    for path in (ROOT / "control-plane/api/app/routers").rglob("*.py"):
        text = path.read_text()
        for marker in ("audit_archive", "audit_export", "FilesystemArchive", "AUDIT_ARCHIVE"):
            assert marker not in text, f"{path.relative_to(ROOT)} references {marker}"


# ── Decision 8: audit_events is left alone ────────────────────────────────────────
def test_decision_8_audit_events_has_no_model_no_writer_and_no_guard():
    """"`audit_events` (migration `d4e5f6a1b2c3`) has no model and no writer. It is left
    alone (not guarded) so a later cleanup can drop it."""
    from app import models

    assert "audit_events" not in models.Base.metadata.tables
    assert "audit_events" not in metadata.tables
    # No writer: the only code mentioning it is the alembic migration and a models.py comment.
    for path in _code_files():
        if path.suffix != ".py" or "alembic" in path.parts:
            continue
        for line in path.read_text(errors="ignore").splitlines():
            if "audit_events" in line:
                assert path.name == "models.py" and line.strip().startswith("#"), \
                    f"{path.relative_to(ROOT)}: {line.strip()}"
    # Not guarded: the migration creates no trigger on it.
    src = AUDIT_EVENTS_MIGRATION.read_text()
    assert "CREATE TRIGGER" not in src and "tn_refuse_change" not in src
