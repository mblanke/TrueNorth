"""course release immutability, enforced by the database

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-10-07 09:00:00.000000

Course releases were immutable only through ORM hooks (app/course_releases/models.py,
``before_update`` / ``before_delete``): a Core or raw SQL statement bypassed them
(CR1-14 in docs/review/codereview1.md). PostgreSQL triggers now hold the same rules for
every writer:

* ``course_releases``: what a release is (its digests, content, version, course) never
  changes; the state moves only candidate -> accepted -> superseded; the acceptance
  record (accepted_at/by, acknowledged_actions, notes) is written only by the move to
  accepted; rows are never deleted.
* ``course_release_blobs``: content-addressed, never rewritten or deleted.
* ``enrollment_release_pins``: never updated or deleted, so an enrollment's release never
  moves (not by a delete and re-insert either). Nothing in the app deletes enrollments.
* None of the three can be TRUNCATEd (a row trigger does not see a TRUNCATE).

SQLite (tests, ``create_all``) keeps the ORM hooks only. Downgrade drops the triggers.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIXED = (
    "tenant_id",
    "course_id",
    "catalogue_code",
    "arc2_code",
    "run_id",
    "slug",
    "title",
    "version",
    "release_digest",
    "learner_digest",
    "platform_digest",
    "instructor_digest",
    "blob_sha256",
    "meta",
    "created_by",
    "created_at",
)
ACCEPTANCE = ("accepted_at", "accepted_by", "acknowledged_actions", "notes")
TRUNCATE_GUARDED = ("course_releases", "course_release_blobs", "enrollment_release_pins")


def _changed(cols) -> str:
    return " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in cols)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"""
        CREATE OR REPLACE FUNCTION tn_course_release_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'course release %: releases are never deleted', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF {_changed(FIXED)} THEN
                RAISE EXCEPTION 'course release %: what a release is is fixed at upload', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
                (OLD.state = 'candidate' AND NEW.state = 'accepted')
                OR (OLD.state = 'accepted' AND NEW.state = 'superseded')
            ) THEN
                RAISE EXCEPTION 'course release %: % -> % is not a release transition', OLD.id, OLD.state, NEW.state
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF ({_changed(ACCEPTANCE)})
                AND NOT (OLD.state = 'candidate' AND NEW.state = 'accepted') THEN
                RAISE EXCEPTION 'course release %: the acceptance record is written once', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER tn_course_release_guard BEFORE UPDATE OR DELETE ON course_releases
            FOR EACH ROW EXECUTE FUNCTION tn_course_release_guard();
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION tn_refuse_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '%: rows are never changed or deleted (%)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER tn_course_release_blob_guard BEFORE UPDATE OR DELETE ON course_release_blobs
            FOR EACH ROW EXECUTE FUNCTION tn_refuse_change();
    """)
    op.execute("""
        CREATE TRIGGER tn_release_pin_guard BEFORE UPDATE OR DELETE ON enrollment_release_pins
            FOR EACH ROW EXECUTE FUNCTION tn_refuse_change();
    """)
    for table in TRUNCATE_GUARDED:
        op.execute(f"""
            CREATE TRIGGER tn_{table}_truncate_guard BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION tn_refuse_change();
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TRUNCATE_GUARDED:
        op.execute(f"DROP TRIGGER IF EXISTS tn_{table}_truncate_guard ON {table}")
    op.execute("DROP TRIGGER IF EXISTS tn_release_pin_guard ON enrollment_release_pins")
    op.execute("DROP TRIGGER IF EXISTS tn_course_release_blob_guard ON course_release_blobs")
    op.execute("DROP TRIGGER IF EXISTS tn_course_release_guard ON course_releases")
    op.execute("DROP FUNCTION IF EXISTS tn_refuse_change()")
    op.execute("DROP FUNCTION IF EXISTS tn_course_release_guard()")
