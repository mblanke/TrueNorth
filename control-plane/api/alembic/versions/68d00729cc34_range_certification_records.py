"""range_certification_records / _entries: the append-only Range Certification Record

Revision ID: 68d00729cc34
Revises: ce2a35194081
Create Date: 2026-10-21 18:00:00.000000

ADR 0013 §6, range certification S6 (issue #244). Three new tables and one new column:

* ``range_certification_records``: one row per deployment (a range's provision
  operation; ``prior_output_sha256`` binds its evidence to that build);
  ``range_certification_entries``: its evidence, appended over time;
  ``range_certification_signing_keys``: every public key that has signed an export, kept
  after rotation.
* PostgreSQL: rows are never updated and no table can be truncated (``tn_refuse_change``,
  created by a5b6c7d8e9f0); signing keys are never deleted. Records and entries are
  deleted only by the retention purge (owner decision 2026-10-21): a transaction that set
  ``tn.cert_purge`` may delete an **archived** record (its range has a newer one) at least
  30 days old (``tn_range_cert_record_guard``); its entries go by ON DELETE CASCADE, and an
  entry DELETE is refused while its record exists (``tn_range_cert_entry_guard``). Every
  insert is stamped with the database clock; an entry must carry its record's tenant.
* ``scheduled_events.require_certified_range`` (default false): stored, not enforced yet.

Additive. Existing ranges get a record at their next provision (or on
``POST /ranges/{id}/certification/capture``). SQLite (tests, ``create_all``) gets the
tables and the ORM guards only. Downgrade drops the triggers, the tables (and with them
every record) and the column.

Revision id: random, not a hex sequence (S1, S2 and S3 each used e0f1a2b3c4d5). Merge
order is S1, S3, S2, S6: at the final rebase ``down_revision`` becomes github/main's head.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "68d00729cc34"
down_revision: str | None = "ce2a35194081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURGE_FLOOR_DAYS = 30
KEYS = "range_certification_signing_keys"
STAMPED = {"range_certification_records": "opened_at", "range_certification_entries": "recorded_at"}


def upgrade() -> None:
    # Guarded like the other additive revisions; b0c1d2e3f4a5 leaves the column to this one
    # (LATER_COLUMNS).
    inspector = sa.inspect(op.get_bind())
    if "range_certification_records" not in inspector.get_table_names():
        _create_tables()
    if "require_certified_range" not in {c["name"] for c in inspector.get_columns("scheduled_events")}:
        op.add_column(
            "scheduled_events",
            sa.Column("require_certified_range", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    if op.get_bind().dialect.name == "postgresql":
        _guard()


def _create_tables() -> None:
    op.create_table(
        "range_certification_records",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("deployment_id", GUID(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("range_id", GUID(), nullable=False),
        sa.Column("range_name", sa.String(255), nullable=False),
        sa.Column("template_id", GUID(), nullable=True),
        sa.Column("template_name", sa.String(255), nullable=False),
        sa.Column("template_version", sa.String(50), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash_basis", sa.String(64), nullable=False),
        sa.Column("prior_output_sha256", sa.String(64), server_default="", nullable=False),
        sa.Column("opened_by_id", GUID(), nullable=True),
        sa.Column("opened_by", sa.String(255), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("deployment_id", name="uq_range_certification_records_deployment"),
    )
    op.create_index("ix_range_certification_records_range", "range_certification_records", ["tenant_id", "range_id"])
    op.create_table(
        "range_certification_entries",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("record_id", GUID(), sa.ForeignKey("range_certification_records.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("actor_id", GUID(), nullable=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("corrects_seq", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("record_id", "seq", name="uq_range_certification_entries_seq"),
    )
    op.create_index("ix_range_certification_entries_tenant", "range_certification_entries", ["tenant_id"])
    op.create_table(
        "range_certification_signing_keys",
        sa.Column("key_id", sa.String(16), primary_key=True),
        sa.Column("alg", sa.String(16), nullable=False),
        sa.Column("public_key", sa.String(128), nullable=False),
        sa.Column("first_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def _guard() -> None:
    # Records: UPDATE never; DELETE only by the retention purge
    # (app/range_certification/retention.py): the transaction set tn.cert_purge, and the record
    # is archived (its range has a newer one) and at least PURGE_FLOOR_DAYS old, a floor no
    # setting lowers.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION tn_range_cert_record_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('tn.cert_purge', true) = 'on'
               AND OLD.opened_at < now() - interval '{PURGE_FLOOR_DAYS} days'
               AND EXISTS (SELECT 1 FROM range_certification_records n
                           WHERE n.tenant_id = OLD.tenant_id AND n.range_id = OLD.range_id
                             AND n.generation > OLD.generation) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '%: rows are never changed or deleted (%)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;
    """)
    # Entries: UPDATE never; DELETE only as the cascade of their record's purge, that is,
    # in a purge transaction once the record row is gone. A direct DELETE of an entry whose
    # record exists is refused, purge or not.
    op.execute("""
        CREATE OR REPLACE FUNCTION tn_range_cert_entry_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('tn.cert_purge', true) = 'on'
               AND NOT EXISTS (SELECT 1 FROM range_certification_records r WHERE r.id = OLD.record_id) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '%: rows are never changed or deleted (%)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;
    """)
    guards = {"range_certification_records": "tn_range_cert_record_guard",
              "range_certification_entries": "tn_range_cert_entry_guard"}
    for table, column in STAMPED.items():
        # An entry carries its record's tenant: one with another tenant would be invisible to
        # the record's readers (they filter on both), so a wrong-tenant FAIL could vanish.
        same_tenant = "" if table == "range_certification_records" else """
                IF NOT EXISTS (SELECT 1 FROM range_certification_records r
                               WHERE r.id = NEW.record_id AND r.tenant_id = NEW.tenant_id) THEN
                    RAISE EXCEPTION 'range_certification_entries: tenant_id must be its record''s'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;"""
        op.execute(f"""
            CREATE OR REPLACE FUNCTION tn_{table}_stamp() RETURNS trigger AS $$
            BEGIN{same_tenant}
                NEW.{column} := clock_timestamp();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        op.execute(f"""
            CREATE TRIGGER tn_{table}_stamp BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION tn_{table}_stamp();
        """)
        op.execute(f"""
            CREATE TRIGGER tn_{table}_guard BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION {guards[table]}();
        """)
    # Signing keys: kept for good, so exports signed before a rotation still verify.
    op.execute(f"""
        CREATE TRIGGER tn_{KEYS}_guard BEFORE UPDATE OR DELETE ON {KEYS}
            FOR EACH ROW EXECUTE FUNCTION tn_refuse_change();
    """)
    for table in (*STAMPED, KEYS):
        op.execute(f"""
            CREATE TRIGGER tn_{table}_truncate_guard BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION tn_refuse_change();
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in (*STAMPED, KEYS):
            for trigger in ("stamp", "guard", "truncate_guard"):
                op.execute(f"DROP TRIGGER IF EXISTS tn_{table}_{trigger} ON {table}")
            op.execute(f"DROP FUNCTION IF EXISTS tn_{table}_stamp()")
        op.execute("DROP FUNCTION IF EXISTS tn_range_cert_record_guard()")
        op.execute("DROP FUNCTION IF EXISTS tn_range_cert_entry_guard()")
    with op.batch_alter_table("scheduled_events") as batch:
        batch.drop_column("require_certified_range")
    op.drop_table(KEYS)
    op.drop_index("ix_range_certification_entries_tenant", table_name="range_certification_entries")
    op.drop_table("range_certification_entries")
    op.drop_index("ix_range_certification_records_range", table_name="range_certification_records")
    op.drop_table("range_certification_records")
