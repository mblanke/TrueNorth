"""lab session idle warning, disk and booking

Revision ID: 3e8b1c7a9d42
Revises: 0fdeb8975b40
Create Date: 2026-10-27 12:00:00.000000

Self-serve labs close after the idle limit (ADR 0014 §18, #288; ``LAB_IDLE_LIMIT_MINUTES``,
4 hours by default). ``idle_warned_at`` records that the Student was told the lab closes
soon, so the sweep tells them once; activity clears it. ``disk_gb`` is the lab's disk, held
through a reset as its vCPU and RAM are. ``booking_id`` names the booking whose hold a booked
class's own Student's lab draws on (None: a self-serve lab). Additive, no data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "3e8b1c7a9d42"
down_revision: str | None = "0fdeb8975b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("lab_sessions")}
    if "idle_warned_at" not in cols:
        op.add_column("lab_sessions", sa.Column("idle_warned_at", sa.DateTime(timezone=True), nullable=True))
    if "disk_gb" not in cols:
        op.add_column("lab_sessions", sa.Column("disk_gb", sa.Integer(), nullable=False, server_default="0"))
    if "booking_id" not in cols:
        op.add_column("lab_sessions", sa.Column("booking_id", GUID(), nullable=True))
        op.create_index("ix_lab_sessions_booking_id", "lab_sessions", ["booking_id"])


def downgrade() -> None:
    op.drop_index("ix_lab_sessions_booking_id", table_name="lab_sessions")
    op.drop_column("lab_sessions", "booking_id")
    op.drop_column("lab_sessions", "disk_gb")
    op.drop_column("lab_sessions", "idle_warned_at")
