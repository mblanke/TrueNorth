"""add lab sessions

Revision ID: c1e2f3a4b5c6
Revises: b0d1e2f3a4b5
Create Date: 2026-10-05 20:00:00.000000

Each student's own small range for a range activity (app/lab_sessions): the session with
its idempotent key, state, lease and kept evidence, and the pool of isolated hypervisor
networks sessions lease. Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "c1e2f3a4b5c6"
down_revision: str | None = "b0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("lab_sessions"):
        op.create_table(
            "lab_sessions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("release_id", GUID(), sa.ForeignKey("course_releases.id"), nullable=False),
            sa.Column("activity_id", sa.String(16), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(24), nullable=False),
            sa.Column("profile_id", sa.String(64), nullable=False),
            sa.Column("profile_digest", sa.String(64), nullable=False),
            sa.Column("backend", sa.String(50), nullable=False),
            sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id"), nullable=True),
            sa.Column("baseline_snapshot_id", GUID(), nullable=True),
            sa.Column("vcpu", sa.Integer(), nullable=False),
            sa.Column("ram_mb", sa.Integer(), nullable=False),
            sa.Column("error", sa.Text(), nullable=False),
            sa.Column("probes", sa.Text(), nullable=False),
            sa.Column("evidence", sa.Text(), nullable=False),
            sa.Column("readiness_seconds", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("provisioning_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("max_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("end_reason", sa.String(32), nullable=False),
            sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("state_since", sa.DateTime(timezone=True), nullable=True),
            sa.Column("step_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pending", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("retired_ranges", sa.Text(), nullable=False, server_default="[]"),
            sa.UniqueConstraint(
                "tenant_id", "user_id", "release_id", "activity_id", "attempt", name="uq_lab_session_key"
            ),
        )
        op.create_index("ix_lab_sessions_state", "lab_sessions", ["state"])
        op.create_index("ix_lab_sessions_user", "lab_sessions", ["user_id", "state"])
    if not _has_table("lab_network_leases"):
        op.create_table(
            "lab_network_leases",
            sa.Column("port_group", sa.String(128), primary_key=True),
            sa.Column("session_id", GUID(), sa.ForeignKey("lab_sessions.id"), nullable=True),
            sa.Column("network_name", sa.String(64), nullable=False),
            sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_lab_network_leases_session_id", "lab_network_leases", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_lab_network_leases_session_id", table_name="lab_network_leases")
    op.drop_table("lab_network_leases")
    op.drop_index("ix_lab_sessions_user", table_name="lab_sessions")
    op.drop_index("ix_lab_sessions_state", table_name="lab_sessions")
    op.drop_table("lab_sessions")
