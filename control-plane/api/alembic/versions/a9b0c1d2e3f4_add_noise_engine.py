"""add background noise engine tables

Revision ID: a9b0c1d2e3f4
Revises: c0d1e2f3a4b5
Create Date: 2026-10-03 12:00:00.000000

Synthetic personas and the in-VM agents that act them out. `noise_activities` is the
ground-truth log of what those agents did, for the white cell only.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def upgrade() -> None:
    # Tolerant of create_all() having run first, as every other recent revision is.
    if not _has_table("noise_profiles"):
        op.create_table(
            "noise_profiles",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("level", sa.Integer(), nullable=False, server_default="40"),
            sa.Column("seed", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("utc_offset", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("overrides", sa.JSON(), nullable=True),
            sa.Column("pack", sa.String(120), nullable=False, server_default="builtin"),
            sa.Column("targets", sa.JSON(), nullable=True),
            sa.Column("plan_key", sa.String(64), nullable=False),
            *_timestamps(),
            sa.UniqueConstraint("range_id", name="uq_noise_profiles_range"),
        )
        op.create_index("ix_noise_profiles_tenant", "noise_profiles", ["tenant_id"])

    if not _has_table("noise_personas"):
        op.create_table(
            "noise_personas",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("profile_id", GUID(), sa.ForeignKey("noise_profiles.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("handle", sa.String(64), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("title", sa.String(255), nullable=False, server_default=""),
            sa.Column("department", sa.String(120), nullable=False, server_default=""),
            sa.Column("node", sa.String(255), nullable=False, server_default=""),
            sa.Column("work_start", sa.Integer(), nullable=False, server_default="8"),
            sa.Column("work_end", sa.Integer(), nullable=False, server_default="17"),
            sa.Column("habits", sa.JSON(), nullable=True),
            sa.Column("lookalikes", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("attrs", sa.JSON(), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("profile_id", "handle", name="uq_noise_personas_handle"),
        )
        op.create_index("ix_noise_personas_tenant", "noise_personas", ["tenant_id"])
        op.create_index("ix_noise_personas_profile_node", "noise_personas", ["profile_id", "node"])

    if not _has_table("noise_agents"):
        op.create_table(
            "noise_agents",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("profile_id", GUID(), sa.ForeignKey("noise_profiles.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("node", sa.String(255), nullable=False),
            sa.Column("zone", sa.String(120), nullable=False, server_default=""),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("agent_version", sa.String(40), nullable=False, server_default=""),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
            sa.UniqueConstraint("profile_id", "node", name="uq_noise_agents_node"),
            sa.UniqueConstraint("token_hash", name="uq_noise_agents_token"),
        )
        op.create_index("ix_noise_agents_tenant", "noise_agents", ["tenant_id"])

    if not _has_table("noise_activities"):
        op.create_table(
            "noise_activities",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("profile_id", GUID(), sa.ForeignKey("noise_profiles.id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_id", GUID(), sa.ForeignKey("noise_agents.id"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("persona", sa.String(64), nullable=False, server_default=""),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("target", sa.String(512), nullable=False, server_default=""),
            sa.Column("lookalike", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("agent_id", "at", "persona", "kind", name="uq_noise_activities_action"),
        )
        op.create_index("ix_noise_activities_profile_at", "noise_activities", ["profile_id", "at"])
        op.create_index("ix_noise_activities_tenant", "noise_activities", ["tenant_id"])


def downgrade() -> None:
    for table in ("noise_activities", "noise_agents", "noise_personas", "noise_profiles"):
        if _has_table(table):
            op.drop_table(table)
