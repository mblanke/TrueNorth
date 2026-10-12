"""audit_logs is append-only in PostgreSQL itself (migration b7c8d9e0f1a2, ADR 0011).

Each test runs on a fresh database built by ``alembic upgrade head`` (tests/conftest.py
``postgres_engine``); skipped without TEST_POSTGRES_ADMIN_URL (CI sets it).

Inserting an OLD row needs the insert stamp off (it sets every new row's timestamp to the
database clock): the tests do that as the table owner, ``ALTER TABLE ... DISABLE TRIGGER
tn_audit_insert_stamp``, insert, and enable it again. That the owner can do so is the
residual risk ADR 0011 records (R3); the signed export chain is what detects it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

API_DIR = Path(__file__).resolve().parents[2] / "control-plane" / "api"
REFUSED = "audit_logs rows are deleted only by retention"


def _insert(conn, when: datetime | None = None, action: str = "probe") -> uuid.UUID:
    rid = uuid.uuid4()
    conn.execute(
        text("INSERT INTO audit_logs (id, timestamp, action, resource_type, resource_id) "
             "VALUES (:id, :ts, :a, 'probe', 'x')"),
        {"id": rid, "ts": when or datetime.now(UTC), "a": action},
    )
    return rid


def _old_row(engine, days_ago: int) -> uuid.UUID:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER tn_audit_insert_stamp"))
        rid = _insert(conn, datetime.now(UTC) - timedelta(days=days_ago))
        conn.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER tn_audit_insert_stamp"))
    return rid


def _export_row(engine, day: date, *, checked: bool = True, check_sha: str = "0" * 64,
                check_age: timedelta | None = None) -> None:
    """An export of ``day`` and (``checked``) the re-verification the trigger requires."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit_exports (day, seq, row_count, file_name, file_sha256, manifest_sha256, key_id) "
                 "VALUES (:d, 0, 1, 'f', :h, :h, '0123456789abcdef')"),
            {"d": day, "h": "0" * 64},
        )
        if checked:
            if check_age:
                conn.execute(text("ALTER TABLE audit_export_checks DISABLE TRIGGER tn_audit_check_stamp"))
            conn.execute(
                text("INSERT INTO audit_export_checks (id, day, seq, manifest_sha256, row_count, key_id, checked_at) "
                     "VALUES (:id, :d, 0, :h, 1, '0123456789abcdef', :at)"),
                {"id": uuid.uuid4(), "d": day, "h": check_sha, "at": datetime.now(UTC) - (check_age or timedelta())},
            )
            if check_age:
                conn.execute(text("ALTER TABLE audit_export_checks ENABLE TRIGGER tn_audit_check_stamp"))


def _refused(engine, sql: str, params: dict | None = None, *, purge: bool = False, needle: str = "") -> str:
    with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
        if purge:
            conn.execute(text("SELECT set_config('tn.audit_purge', 'on', true)"))
        conn.execute(text(sql), params or {})
    assert needle in str(exc.value), str(exc.value)
    return str(exc.value)


def test_update_is_refused(postgres_engine):
    with postgres_engine.begin() as conn:
        rid = _insert(conn)
    _refused(postgres_engine, "UPDATE audit_logs SET action = 'x' WHERE id = :id", {"id": rid},
             needle="never changed")
    _refused(postgres_engine, "UPDATE audit_logs SET action = 'x' WHERE id = :id", {"id": rid}, purge=True,
             needle="never changed")


def test_delete_without_the_purge_setting_is_refused(postgres_engine):
    rid = _old_row(postgres_engine, 60)
    _export_row(postgres_engine, (datetime.now(UTC) - timedelta(days=60)).date())
    _refused(postgres_engine, "DELETE FROM audit_logs WHERE id = :id", {"id": rid}, needle=REFUSED)


def test_delete_of_a_recent_row_is_refused_even_with_the_setting(postgres_engine):
    rid = _old_row(postgres_engine, 10)
    _export_row(postgres_engine, (datetime.now(UTC) - timedelta(days=10)).date())
    _refused(postgres_engine, "DELETE FROM audit_logs WHERE id = :id", {"id": rid}, purge=True, needle=REFUSED)


def test_delete_of_an_old_row_without_an_export_is_refused(postgres_engine):
    rid = _old_row(postgres_engine, 60)
    _refused(postgres_engine, "DELETE FROM audit_logs WHERE id = :id", {"id": rid}, purge=True, needle=REFUSED)


def test_delete_with_an_export_but_no_fresh_matching_check_is_refused(postgres_engine):
    """Review S3-4: an export row alone is not enough; the purge must have re-verified it."""
    day = (datetime.now(UTC) - timedelta(days=60)).date()
    rid = _old_row(postgres_engine, 60)
    _export_row(postgres_engine, day, checked=False)
    _refused(postgres_engine, "DELETE FROM audit_logs WHERE id = :id", {"id": rid}, purge=True, needle=REFUSED)


def test_a_check_of_another_manifest_hash_is_refused(postgres_engine):
    rid = _old_row(postgres_engine, 60)
    _export_row(postgres_engine, (datetime.now(UTC) - timedelta(days=60)).date(), check_sha="f" * 64)
    _refused(postgres_engine, "DELETE FROM audit_logs WHERE id = :id", {"id": rid}, purge=True, needle=REFUSED)


def test_a_stale_check_is_refused(postgres_engine):
    rid = _old_row(postgres_engine, 60)
    _export_row(postgres_engine, (datetime.now(UTC) - timedelta(days=60)).date(), check_age=timedelta(days=2))
    _refused(postgres_engine, "DELETE FROM audit_logs WHERE id = :id", {"id": rid}, purge=True, needle=REFUSED)


def test_checks_are_stamped_and_never_changed(postgres_engine):
    day = date(2026, 1, 2)
    with postgres_engine.begin() as conn:
        conn.execute(text("INSERT INTO audit_export_checks (id, day, seq, manifest_sha256, row_count, key_id, checked_at) "
                          "VALUES (:id, :d, 0, :h, 1, 'k', '2020-01-01')"), {"id": uuid.uuid4(), "d": day, "h": "0" * 64})
        at = conn.execute(text("SELECT checked_at FROM audit_export_checks WHERE day = :d"), {"d": day}).scalar()
    assert at > datetime.now(UTC) - timedelta(minutes=5)
    _refused(postgres_engine, "UPDATE audit_export_checks SET row_count = 2", needle="never changed or deleted")
    _refused(postgres_engine, "DELETE FROM audit_export_checks", needle="never changed or deleted")
    _refused(postgres_engine, "TRUNCATE audit_export_checks", needle="never changed or deleted")


def test_delete_with_the_setting_an_old_row_and_an_export_succeeds(postgres_engine):
    rid = _old_row(postgres_engine, 60)
    _export_row(postgres_engine, (datetime.now(UTC) - timedelta(days=60)).date())
    with postgres_engine.begin() as conn:
        conn.execute(text("SELECT set_config('tn.audit_purge', 'on', true)"))
        assert conn.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": rid}).rowcount == 1
    # Transaction-local: the next transaction is refused again.
    rid2 = _old_row(postgres_engine, 60)
    _refused(postgres_engine, "DELETE FROM audit_logs WHERE id = :id", {"id": rid2}, needle=REFUSED)


@pytest.mark.parametrize("table", ["audit_logs", "audit_exports"])
def test_truncate_is_refused(postgres_engine, table):
    _refused(postgres_engine, f"TRUNCATE {table} CASCADE", needle="never changed or deleted")


def test_audit_exports_rows_are_never_changed(postgres_engine):
    day = date(2026, 1, 1)
    _export_row(postgres_engine, day)
    _refused(postgres_engine, "UPDATE audit_exports SET row_count = 2 WHERE day = :d", {"d": day},
             needle="never changed or deleted")
    _refused(postgres_engine, "DELETE FROM audit_exports WHERE day = :d", {"d": day},
             needle="never changed or deleted")


def test_an_explicit_past_timestamp_is_stamped_to_now(postgres_engine):
    with postgres_engine.begin() as conn:
        rid = _insert(conn, datetime(2020, 1, 1, tzinfo=UTC))
        ts = conn.execute(text("SELECT timestamp FROM audit_logs WHERE id = :id"), {"id": rid}).scalar()
    assert ts > datetime.now(UTC) - timedelta(minutes=5)


def test_the_orm_writer_still_inserts(postgres_engine):
    from app.models import AuditLog

    with Session(postgres_engine) as s:
        s.add(AuditLog(action="create", resource_type="range", resource_id="r1"))
        s.commit()
        assert s.query(AuditLog).filter_by(resource_id="r1").count() == 1


def test_worker_export_and_retention_through_the_trigger(postgres_engine, tmp_path, monkeypatch):
    """The real purge path: the worker's export, then its retention, on PostgreSQL."""
    from worker import audit_signing, audit_tasks
    from worker.audit_archive import FilesystemArchive

    monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", "30")
    today = datetime.now(UTC).date()
    old = [_old_row(postgres_engine, 45) for _ in range(2)]
    _old_row(postgres_engine, 5)
    key = audit_signing.from_seed("pg-seed-" + "z" * 40)
    archive = FilesystemArchive(tmp_path / "archive")
    with Session(postgres_engine) as db:
        audit_tasks.run_export(db, archive, key, today)
        purged = audit_tasks.run_retention(db, archive, key, today)
    assert [p["rows"] for p in purged] == [2]
    with postgres_engine.connect() as conn:
        left = conn.execute(text("SELECT count(*) FROM audit_logs WHERE id = ANY(:ids)"), {"ids": old}).scalar()
        notes = conn.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'audit.purge'")).scalar()
    assert left == 0 and notes == 1
    with postgres_engine.connect() as conn:
        checks = conn.execute(text("SELECT count(*) FROM audit_export_checks")).scalar()
    assert checks == 1  # the purge recorded its re-verification


def test_without_the_setting_the_worker_purge_is_refused(postgres_engine, tmp_path, monkeypatch):
    """If the retention job did not set tn.audit_purge, the trigger would stop it."""
    from worker import audit_signing, audit_tasks
    from worker.audit_archive import FilesystemArchive

    monkeypatch.setenv("AUDIT_HOT_RETENTION_DAYS", "30")
    _old_row(postgres_engine, 45)
    key = audit_signing.from_seed("pg-seed-" + "z" * 40)
    archive = FilesystemArchive(tmp_path / "archive")
    today = datetime.now(UTC).date()
    with Session(postgres_engine) as db:
        audit_tasks.run_export(db, archive, key, today)
        monkeypatch.setattr(audit_tasks, "_allow_purge", lambda db: None)  # no tn.audit_purge
        with pytest.raises(DBAPIError, match=REFUSED):
            audit_tasks.run_retention(db, archive, key, today)


def test_downgrade_then_upgrade_round_trips(postgres_engine):
    url = postgres_engine.url.render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API_DIR)}

    def alembic(*args: str) -> None:
        r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API_DIR, env=env,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]

    alembic("downgrade", "2b3c4d5e6f72")
    with postgres_engine.begin() as conn:
        rid = _insert(conn)
        conn.execute(text("UPDATE audit_logs SET action = 'changed' WHERE id = :id"), {"id": rid})  # no guard now
        assert conn.execute(text("SELECT to_regclass('audit_exports')")).scalar() is None
        assert conn.execute(text("SELECT to_regclass('audit_export_checks')")).scalar() is None
    alembic("upgrade", "head")
    _refused(postgres_engine, "UPDATE audit_logs SET action = 'x' WHERE id = :id", {"id": rid}, needle="never changed")
