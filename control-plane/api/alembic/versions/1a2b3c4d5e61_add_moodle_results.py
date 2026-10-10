"""add moodle results

Revision ID: 1a2b3c4d5e61
Revises: b7c8d9e0f1a3
Create Date: 2026-10-09 10:00:00.000000

Results pulled back from Moodle (app/moodle_results): ``moodle_result_cursors`` holds
where TrueNorth is in each Moodle's change log and who is pulling it now;
``moodle_result_records`` is the ledger that makes recording idempotent (one row per
Moodle, person and fact, pointing at the quiz attempt it mirrors). Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "1a2b3c4d5e61"
down_revision: str | None = "b7c8d9e0f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "moodle_result_cursors" not in tables:
        op.create_table(
            "moodle_result_cursors",
            sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("cursor", sa.String(64), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False),
            sa.Column("rows_seen", sa.Integer(), nullable=False),
            sa.Column("rows_applied", sa.Integer(), nullable=False),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_holder", sa.String(32), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    if "moodle_result_records" not in tables:
        op.create_table(
            "moodle_result_records",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("course_id", GUID(), sa.ForeignKey("courses.id"), nullable=False),
            sa.Column("ref", sa.String(200), nullable=False),
            sa.Column("state", sa.Integer(), nullable=False),
            sa.Column("grade", sa.Float(), nullable=True),
            sa.Column("grade_max", sa.Float(), nullable=True),
            sa.Column("source_time", sa.Integer(), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("quiz_attempt_id", GUID(), sa.ForeignKey("quiz_attempts.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("platform_id", "user_id", "course_id", "ref", name="uq_moodle_result_record"),
        )
        op.create_index(
            "ix_moodle_result_records_course", "moodle_result_records", ["platform_id", "course_id", "user_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_moodle_result_records_course", table_name="moodle_result_records")
    op.drop_table("moodle_result_records")
    op.drop_table("moodle_result_cursors")
