"""classes, class staff and roll call

Revision ID: 6a5f8e732b95
Revises: b62353ae80af
Create Date: 2026-10-22 18:00:00.000000

Classes 1 (ADR 0014 §1, §2, §5, §8; issue #278). ``classes`` (one running of a course,
with its lead instructor and dates), ``class_staff`` (assistants and observers) and
``class_roll_call`` (the Students on it, by lower-cased email). Additive.

Downgrade drops the three tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "6a5f8e732b95"
down_revision: str | None = "b62353ae80af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    ]


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "classes" not in existing:
        op.create_table(
            "classes",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("course_id", GUID(), sa.ForeignKey("courses.id"), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("lead_instructor_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="open"),
            sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            *_stamps(),
        )
        op.create_index("ix_classes_tenant_start", "classes", ["tenant_id", "start_date"])
    if "class_staff" not in existing:
        op.create_table(
            "class_staff",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("class_id", GUID(), sa.ForeignKey("classes.id"), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            *_stamps(),
            sa.UniqueConstraint("class_id", "user_id", name="uq_class_staff_user"),
        )
        op.create_index("ix_class_staff_user", "class_staff", ["user_id"])
    if "class_roll_call" not in existing:
        op.create_table(
            "class_roll_call",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("class_id", GUID(), sa.ForeignKey("classes.id"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
            sa.Column("added_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("onboarding_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("signed_in_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.UniqueConstraint("class_id", "email", name="uq_class_roll_call_email"),
        )
        op.create_index("ix_class_roll_call_email", "class_roll_call", ["email"])
        op.create_index("ix_class_roll_call_user", "class_roll_call", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_class_roll_call_user", table_name="class_roll_call")
    op.drop_index("ix_class_roll_call_email", table_name="class_roll_call")
    op.drop_table("class_roll_call")
    op.drop_index("ix_class_staff_user", table_name="class_staff")
    op.drop_table("class_staff")
    op.drop_index("ix_classes_tenant_start", table_name="classes")
    op.drop_table("classes")
