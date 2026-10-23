"""class booking approval: class sessions, booking approvals, two booking states

Revision ID: 5e6d8a033133
Revises: 4c0dab4abfa8
Create Date: 2026-10-23 12:00:00.000000

Classes 4 (ADR 0014 §14-17; issue #281):

* ``class_sessions``: a class's sessions, each one a scheduler booking (unique per booking);
* ``scheduler_booking_approvals``: the approval of a booking that needs one (every class
  session): who asked, the approver's proposed time, who decided and why it was rejected;
* ``class_roll_call.pending_reason``: why a Student added after approval waits for an
  approver (the roll-call status ``pending`` is a plain string, no type change);
* the booking states ``pending_approval`` and ``rejected`` (PostgreSQL enum labels; SQLite
  stores the enum as VARCHAR).

Additive. Downgrade drops the two tables and the column. PostgreSQL cannot drop an enum
label, so the two states stay in the type; refuse the downgrade while a booking is in
either, rather than leave rows the older code cannot read. A roll-call addition still
waiting for an approver becomes ``removed``: the older code would count a ``pending`` entry
as on the roll call, and invite and admit someone nobody approved.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "5e6d8a033133"
down_revision: str | None = "4c0dab4abfa8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Not used in this transaction (PostgreSQL forbids using a label in the transaction
        # that adds it), so adding them here is safe.
        op.execute("ALTER TYPE eventstate ADD VALUE IF NOT EXISTS 'pending_approval' AFTER 'draft'")
        op.execute("ALTER TYPE eventstate ADD VALUE IF NOT EXISTS 'rejected' AFTER 'pending_approval'")
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if "class_sessions" not in existing:
        op.create_table(
            "class_sessions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("class_id", GUID(), sa.ForeignKey("classes.id"), nullable=False),
            sa.Column("event_id", GUID(), sa.ForeignKey("scheduled_events.id"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            *_stamps(),
            sa.UniqueConstraint("event_id", name="uq_class_sessions_event"),
        )
        op.create_index("ix_class_sessions_class", "class_sessions", ["class_id"])
    if "scheduler_booking_approvals" not in existing:
        op.create_table(
            "scheduler_booking_approvals",
            sa.Column("event_id", GUID(), sa.ForeignKey("scheduled_events.id"), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("requested_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("proposed_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("proposed_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("proposed_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            *_stamps(),
        )
        op.create_index("ix_booking_approvals_tenant", "scheduler_booking_approvals", ["tenant_id"])
    if "pending_reason" not in {c["name"] for c in insp.get_columns("class_roll_call")}:
        op.add_column("class_roll_call", sa.Column("pending_reason", sa.String(1000), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text("SELECT count(*) FROM scheduled_events WHERE state IN ('pending_approval', 'rejected')")
    ).scalar()
    if held:
        raise RuntimeError(
            f"{held} booking(s) are pending approval or rejected, states the previous revision does not know; "
            "cancel them before downgrading"
        )
    insp = sa.inspect(bind)
    if "pending_reason" in {c["name"] for c in insp.get_columns("class_roll_call")}:
        with op.batch_alter_table("class_roll_call") as batch:
            batch.drop_column("pending_reason")
    bind.execute(sa.text("UPDATE class_roll_call SET status = 'removed' WHERE status = 'pending'"))
    existing = set(insp.get_table_names())
    if "scheduler_booking_approvals" in existing:
        op.drop_index("ix_booking_approvals_tenant", table_name="scheduler_booking_approvals")
        op.drop_table("scheduler_booking_approvals")
    if "class_sessions" in existing:
        op.drop_index("ix_class_sessions_class", table_name="class_sessions")
        op.drop_table("class_sessions")
