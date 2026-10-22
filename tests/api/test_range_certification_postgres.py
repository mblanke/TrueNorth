"""The Range Certification Record is append-only in PostgreSQL itself (migration
68d00729cc34, ADR 0013 §6): UPDATE, DELETE and TRUNCATE are refused on both tables, and
every insert is stamped with the database clock, so evidence cannot be backdated.

Each test runs on a fresh database built by ``alembic upgrade head`` (tests/conftest.py
``postgres_engine``); skipped without TEST_POSTGRES_ADMIN_URL (CI sets it). Plain SQL, so
the ORM guards (tests/api/test_range_certification.py) are not what refuses here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

REFUSED = "rows are never changed or deleted"
LONG_AGO = datetime(2001, 1, 1, tzinfo=UTC)
TENANT = uuid.uuid4()


def _record(conn) -> uuid.UUID:
    rid = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO range_certification_records (id, deployment_id, generation, tenant_id, range_id, range_name, "
        "template_name, template_version, plan_hash, plan_hash_basis, opened_by, opened_at) VALUES "
        "(:id, :dep, 1, :t, :r, 'r', 't', '1', :h, 'template_yaml_sha256', 'system', :at)"),
        {"id": rid, "dep": uuid.uuid4(), "t": TENANT, "r": uuid.uuid4(), "h": "0" * 64, "at": LONG_AGO})
    conn.execute(text(
        "INSERT INTO range_certification_entries (id, record_id, tenant_id, seq, section, status, summary, detail, "
        "source, actor, recorded_at) VALUES (:id, :rec, :t, 1, 'isolation', 'fail', 'reachable', '{}', 'test', "
        "'system', :at)"), {"id": uuid.uuid4(), "rec": rid, "t": TENANT, "at": LONG_AGO})
    return rid


@pytest.fixture
def seeded(postgres_engine):
    with postgres_engine.begin() as conn:
        rid = _record(conn)
    return postgres_engine, rid


@pytest.mark.parametrize("statement", [
    "UPDATE range_certification_entries SET status = 'pass'",
    "DELETE FROM range_certification_entries",
    "TRUNCATE range_certification_entries",
    "UPDATE range_certification_records SET plan_hash = 'x'",
    "DELETE FROM range_certification_records",
    "TRUNCATE range_certification_records CASCADE",
])
def test_evidence_cannot_be_changed_deleted_or_truncated(seeded, statement):
    engine, _ = seeded
    with pytest.raises(DBAPIError, match=REFUSED), engine.begin() as conn:
        conn.execute(text(statement))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM range_certification_entries")).scalar() == "fail"


def test_inserts_are_stamped_with_the_database_clock(seeded):
    engine, rid = seeded
    with engine.connect() as conn:
        opened = conn.execute(text("SELECT opened_at FROM range_certification_records WHERE id = :i"), {"i": rid})
        recorded = conn.execute(text("SELECT recorded_at FROM range_certification_entries WHERE record_id = :i"),
                                {"i": rid})
        assert opened.scalar().year > 2001 and recorded.scalar().year > 2001, "a writer cannot backdate evidence"


def test_appending_still_works(seeded):
    engine, rid = seeded
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO range_certification_entries (id, record_id, tenant_id, seq, section, status, summary, "
            "detail, source, actor, corrects_seq, reason) VALUES (:id, :rec, :t, 2, 'isolation', 'pass', 'fixed', "
            "'{}', 'correction', 'Ops Lead', 1, 'probe rerun')"), {"id": uuid.uuid4(), "rec": rid, "t": TENANT})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM range_certification_entries")).scalar() == 2


# ── The one delete path: retention of archived records (app/range_certification/retention.py) ──
def _two_builds(engine, *, old_days: int) -> tuple[uuid.UUID, uuid.UUID]:
    """An archived record opened ``old_days`` ago (stamp trigger off, as the table owner can)
    and the range's newer, current one."""
    tenant, rng = uuid.uuid4(), uuid.uuid4()
    ids = []
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE range_certification_records DISABLE TRIGGER tn_range_certification_records_stamp"))
        for generation, age in ((1, old_days), (2, 0)):
            rid = uuid.uuid4()
            conn.execute(text(
                "INSERT INTO range_certification_records (id, deployment_id, generation, tenant_id, range_id, "
                "range_name, template_name, template_version, plan_hash, plan_hash_basis, opened_by, opened_at) "
                "VALUES (:id, :dep, :g, :t, :r, 'r', 't', '1', :h, 'b', 'system', now() - make_interval(days => :age))"),
                {"id": rid, "dep": uuid.uuid4(), "g": generation, "t": tenant, "r": rng, "h": "0" * 64, "age": age})
            conn.execute(text(
                "INSERT INTO range_certification_entries (id, record_id, tenant_id, seq, section, status, summary, "
                "detail, source, actor) VALUES (:id, :rec, :t, 1, 'guest_readiness', 'pass', 'ok', '{}', 't', 's')"),
                {"id": uuid.uuid4(), "rec": rid, "t": tenant})
            ids.append(rid)
        conn.execute(text("ALTER TABLE range_certification_records ENABLE TRIGGER tn_range_certification_records_stamp"))
    return ids[0], ids[1]


def _purge(conn, rid: uuid.UUID) -> None:
    """The record only: its entries go by ON DELETE CASCADE."""
    conn.execute(text("SELECT set_config('tn.cert_purge', 'on', true)"))
    conn.execute(text("DELETE FROM range_certification_records WHERE id = :i"), {"i": rid})


def test_an_old_archived_record_can_be_purged_in_a_purge_transaction(postgres_engine):
    archived, current = _two_builds(postgres_engine, old_days=400)
    with postgres_engine.begin() as conn:
        _purge(conn, archived)
    with postgres_engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM range_certification_records")).scalars().all() == [current]


@pytest.mark.parametrize("case", ["no_flag", "current", "too_young", "entry_while_record_exists"])
def test_anything_else_is_still_refused(postgres_engine, case):
    archived, current = _two_builds(postgres_engine, old_days=10 if case == "too_young" else 400)
    target = current if case == "current" else archived
    with pytest.raises(DBAPIError, match=REFUSED), postgres_engine.begin() as conn:
        if case == "no_flag":
            conn.execute(text("DELETE FROM range_certification_records WHERE id = :i"), {"i": target})
        elif case == "entry_while_record_exists":  # a purge transaction, but the record is still there
            conn.execute(text("SELECT set_config('tn.cert_purge', 'on', true)"))
            conn.execute(text("DELETE FROM range_certification_entries WHERE record_id = :i"), {"i": target})
        else:
            _purge(conn, target)


def test_the_retention_job_purges_through_the_trigger_and_audits(postgres_engine):
    from app.range_certification import retention
    from sqlalchemy.orm import Session

    archived, current = _two_builds(postgres_engine, old_days=400)
    with Session(postgres_engine) as db:
        assert retention.purge(db, months=12) == 1
    with postgres_engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM range_certification_records")).scalars().all() == [current]
        assert conn.execute(text("SELECT count(*) FROM range_certification_entries WHERE record_id = :i"),
                            {"i": archived}).scalar() == 0
        detail = conn.execute(text("SELECT detail FROM audit_logs WHERE action = 'range_certification.purge'")).scalar()
        assert f"record={archived}" in detail and "record_sha256=" in detail


def test_an_entry_must_carry_its_records_tenant(seeded):
    engine, rid = seeded
    with pytest.raises(DBAPIError, match="tenant_id must be its record"), engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO range_certification_entries (id, record_id, tenant_id, seq, section, status, summary, "
            "detail, source, actor) VALUES (:id, :rec, :t, 2, 'isolation', 'pass', 'x', '{}', 't', 's')"),
            {"id": uuid.uuid4(), "rec": rid, "t": uuid.uuid4()})


def test_signing_keys_are_kept_for_good(postgres_engine):
    with postgres_engine.begin() as conn:
        conn.execute(text("INSERT INTO range_certification_signing_keys (key_id, alg, public_key) "
                          "VALUES ('0123456789abcdef', 'Ed25519', 'AAAA')"))
    for statement in ("DELETE FROM range_certification_signing_keys",
                      "UPDATE range_certification_signing_keys SET public_key = 'BBBB'",
                      "TRUNCATE range_certification_signing_keys"):
        with pytest.raises(DBAPIError, match=REFUSED), postgres_engine.begin() as conn:
            conn.execute(text(statement))
