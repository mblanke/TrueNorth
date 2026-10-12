"""audit_logs append-only; audit_exports for the signed daily exports (ADR 0011)

Revision ID: b7c8d9e0f1a2
Revises: 2b3c4d5e6f72
Create Date: 2026-10-12 12:00:00.000000

* ``audit_exports``: one row per signed export of a UTC day (worker/audit_tasks.py).
* ``audit_logs`` rows are never changed: UPDATE is refused, and so is TRUNCATE.
* ``audit_export_checks``: one row per re-verification of an export, written by the
  retention job immediately before (and in the same transaction as) a purge.
* A row is deleted only by the retention job's path: the transaction has set
  ``tn.audit_purge`` to ``on`` (``set_config(..., true)``, transaction-local), the row is
  at least 30 days old (UTC days; a floor no setting can lower), and its day has an export
  with a **check** less than a day old whose manifest_sha256 equals the export's. The job
  writes that check only after the files verify (signature by a known key, hashes, the
  table's manifest hash) and hold exactly the table's rows.
* Every inserted row is stamped with the database clock (``clock_timestamp()``), whatever
  the writer supplied, so nothing can be backdated into a day already exported.
* ``audit_exports`` and ``audit_export_checks`` rows are never updated, deleted or
  truncated (``tn_refuse_change``, created by a5b6c7d8e9f0 and reused here); a check's
  ``checked_at`` is stamped with the database clock.

The superuser (the API and worker connect as the database owner today) can still disable
triggers; that residual risk, and detection by the signed hash chain, is in ADR 0011.

PostgreSQL only; SQLite (tests, ``create_all``) gets the table and no triggers.
Downgrade drops the triggers, this migration's functions and the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "2b3c4d5e6f72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURGE_FLOOR_DAYS = 30


def upgrade() -> None:
    op.create_table(
        "audit_exports",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("first_id", GUID(), nullable=True),
        sa.Column("last_id", GUID(), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("prev_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("key_id", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_exports_created_at", "audit_exports", ["created_at"])
    op.create_table(
        "audit_export_checks",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.String(16), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_export_checks_day", "audit_export_checks", ["day"])
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION tn_audit_insert_stamp() RETURNS trigger AS $$
        BEGIN
            NEW.timestamp := clock_timestamp();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER tn_audit_insert_stamp BEFORE INSERT ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION tn_audit_insert_stamp();
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION tn_audit_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'audit_logs rows are never changed (row %)', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF current_setting('tn.audit_purge', true) IS DISTINCT FROM 'on'
               OR OLD.timestamp >= (date_trunc('day', now() AT TIME ZONE 'UTC')
                                    - interval '{PURGE_FLOOR_DAYS} days') AT TIME ZONE 'UTC'
               OR NOT EXISTS (SELECT 1 FROM audit_exports e
                              JOIN audit_export_checks c
                                ON c.day = e.day AND c.seq = e.seq AND c.manifest_sha256 = e.manifest_sha256
                              WHERE e.day = (OLD.timestamp AT TIME ZONE 'UTC')::date
                                AND c.checked_at > now() - interval '1 day') THEN
                RAISE EXCEPTION 'audit_logs rows are deleted only by retention after a verified export (row %)', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER tn_audit_guard BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION tn_audit_guard();
    """)
    op.execute("""
        CREATE TRIGGER tn_audit_exports_guard BEFORE UPDATE OR DELETE ON audit_exports
            FOR EACH ROW EXECUTE FUNCTION tn_refuse_change();
    """)
    op.execute("""
        CREATE TRIGGER tn_audit_export_checks_guard BEFORE UPDATE OR DELETE ON audit_export_checks
            FOR EACH ROW EXECUTE FUNCTION tn_refuse_change();
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION tn_audit_check_stamp() RETURNS trigger AS $$
        BEGIN
            NEW.checked_at := clock_timestamp();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER tn_audit_check_stamp BEFORE INSERT ON audit_export_checks
            FOR EACH ROW EXECUTE FUNCTION tn_audit_check_stamp();
    """)
    for table in ("audit_logs", "audit_exports", "audit_export_checks"):
        op.execute(f"""
            CREATE TRIGGER tn_{table}_truncate_guard BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION tn_refuse_change();
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("audit_logs", "audit_exports", "audit_export_checks"):
            op.execute(f"DROP TRIGGER IF EXISTS tn_{table}_truncate_guard ON {table}")
        op.execute("DROP TRIGGER IF EXISTS tn_audit_check_stamp ON audit_export_checks")
        op.execute("DROP TRIGGER IF EXISTS tn_audit_export_checks_guard ON audit_export_checks")
        op.execute("DROP TRIGGER IF EXISTS tn_audit_exports_guard ON audit_exports")
        op.execute("DROP TRIGGER IF EXISTS tn_audit_guard ON audit_logs")
        op.execute("DROP TRIGGER IF EXISTS tn_audit_insert_stamp ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS tn_audit_guard()")
        op.execute("DROP FUNCTION IF EXISTS tn_audit_insert_stamp()")
        op.execute("DROP FUNCTION IF EXISTS tn_audit_check_stamp()")
    op.drop_index("ix_audit_export_checks_day", table_name="audit_export_checks")
    op.drop_table("audit_export_checks")
    op.drop_index("ix_audit_exports_created_at", table_name="audit_exports")
    op.drop_table("audit_exports")
