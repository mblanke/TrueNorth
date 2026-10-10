"""add cmi5_grades: the server-marked cmi5 quiz attempts

Revision ID: 7c3a1e5f9d42
Revises: 6b2f0d4e8c31
Create Date: 2026-10-10 02:00:00.000000

Each ``POST /cmi5/releases/{id}/aus/{n}/grade`` is recorded: a TrueNorth session's
passed/failed must report that mark (app/cmi5/rules.py, TN-GRADE), and the rows count a
Student's attempts (CMI5_GRADE_ATTEMPTS). Its own revision, not folded into 6b2f0d4e8c31,
so a database that already ran that one gets the table too. Additive; downgrade drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "7c3a1e5f9d42"
down_revision: str | None = "6b2f0d4e8c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "cmi5_grades" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "cmi5_grades",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("release_id", GUID(), sa.ForeignKey("course_releases.id"), nullable=False),
        sa.Column("au_index", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("scaled", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cmi5_grade_user_au", "cmi5_grades", ["user_id", "release_id", "au_index", "created_at"])


def downgrade() -> None:
    if "cmi5_grades" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_cmi5_grade_user_au", table_name="cmi5_grades")
        op.drop_table("cmi5_grades")
