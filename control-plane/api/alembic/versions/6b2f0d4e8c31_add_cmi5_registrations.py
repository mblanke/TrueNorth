"""add cmi5 registrations and sessions

Revision ID: 6b2f0d4e8c31
Revises: 5a1e9c3d7b20
Create Date: 2026-10-09 15:00:00.000000

TrueNorth as the cmi5 LMS for its own releases (app/cmi5, docs/cmi5.md): a registration per
enrolment (its id is the enrolment's), and a session per AU launch with the hashes of its
one-time fetch secret and auth token. Additive; downgrade drops both tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "6b2f0d4e8c31"
down_revision: str | None = "5a1e9c3d7b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("cmi5_registrations"):
        op.create_table(
            "cmi5_registrations",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("enrollment_id", GUID(), sa.ForeignKey("enrollments.id"), nullable=False, unique=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("release_id", GUID(), sa.ForeignKey("course_releases.id"), nullable=False),
            sa.Column("progress", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_cmi5_reg_user", "cmi5_registrations", ["user_id"])
    if not _has_table("cmi5_sessions"):
        op.create_table(
            "cmi5_sessions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("registration_id", GUID(), sa.ForeignKey("cmi5_registrations.id"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("au_index", sa.Integer(), nullable=False),
            sa.Column("launch_mode", sa.String(8), nullable=False),
            sa.Column("move_on", sa.String(24), nullable=False),
            sa.Column("mastery_score", sa.Float(), nullable=True),
            sa.Column("context_template", sa.Text(), nullable=False),
            sa.Column("fetch_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("token_hash", sa.String(64), nullable=True),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("sent", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("launch_data_fetched", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("prefs_fetched", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("launched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_cmi5_session_reg_state", "cmi5_sessions", ["registration_id", "state"])


def downgrade() -> None:
    if _has_table("cmi5_sessions"):
        op.drop_index("ix_cmi5_session_reg_state", table_name="cmi5_sessions")
        op.drop_table("cmi5_sessions")
    if _has_table("cmi5_registrations"):
        op.drop_index("ix_cmi5_reg_user", table_name="cmi5_registrations")
        op.drop_table("cmi5_registrations")
