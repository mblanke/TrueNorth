"""class attendance and close-out: attendance per session, results, close-out marks

Revision ID: 0fdeb8975b40
Revises: 57e2c4b65b98
Create Date: 2026-10-27 12:00:00.000000

Classes 6 (ADR 0014 §12-13; issue #283):

* ``class_attendance``: one row per (class session, roll-call entry): present, absent or
  excused, recorded by the clock after the session (``auto``) or set by the lead or an admin
  (``instructor``);
* ``class_results``: one row per (class, roll-call entry): pass, fail or incomplete and a
  comment. No numeric column (Unclassified DLP);
* ``class_closeouts``: when a class was last closed and by whom, and its last reopening.

A closed class is ``classes.status = 'closed'``: a value of the existing string column, so
``classes`` itself is unchanged.

Additive; no rows are written. Downgrade refuses while a class is closed (the previous
revision has no such status: reopen it first), then drops the three tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "0fdeb8975b40"
down_revision: str | None = "57e2c4b65b98"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    ]


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "class_attendance" not in existing:
        op.create_table(
            "class_attendance",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("class_id", GUID(), sa.ForeignKey("classes.id"), nullable=False),
            sa.Column("session_id", GUID(), sa.ForeignKey("class_sessions.id"), nullable=False),
            sa.Column("entry_id", GUID(), sa.ForeignKey("class_roll_call.id"), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("evidence_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("note", sa.String(500), nullable=True),
            sa.Column("corrected_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.UniqueConstraint("session_id", "entry_id", name="uq_class_attendance_entry"),
        )
        op.create_index("ix_class_attendance_class", "class_attendance", ["class_id"])
    if "class_results" not in existing:
        op.create_table(
            "class_results",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("class_id", GUID(), sa.ForeignKey("classes.id"), nullable=False),
            sa.Column("entry_id", GUID(), sa.ForeignKey("class_roll_call.id"), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("suggested_outcome", sa.String(16), nullable=True),
            sa.Column("comment", sa.String(1000), nullable=True),
            sa.Column("sessions_held", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sessions_present", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sessions_excused", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("decided_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.UniqueConstraint("class_id", "entry_id", name="uq_class_results_entry"),
        )
        op.create_index("ix_class_results_user", "class_results", ["user_id"])
    if "class_closeouts" not in existing:
        op.create_table(
            "class_closeouts",
            sa.Column("class_id", GUID(), sa.ForeignKey("classes.id"), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reopened_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reopen_reason", sa.String(500), nullable=True),
            *_stamps(),
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    closed = bind.execute(sa.text("SELECT count(*) FROM classes WHERE status = 'closed'")).scalar()
    if closed:
        raise RuntimeError(
            f"{closed} class(es) are closed, a status the previous revision does not know. "
            "Reopen them (an admin, POST /classes/{id}/reopen) before downgrading"
        )
    if "class_closeouts" in existing:
        op.drop_table("class_closeouts")
    if "class_results" in existing:
        op.drop_index("ix_class_results_user", table_name="class_results")
        op.drop_table("class_results")
    if "class_attendance" in existing:
        op.drop_index("ix_class_attendance_class", table_name="class_attendance")
        op.drop_table("class_attendance")
