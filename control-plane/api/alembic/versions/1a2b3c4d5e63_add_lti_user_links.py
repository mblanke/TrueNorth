"""add lti user links

Revision ID: 1a2b3c4d5e63
Revises: 1a2b3c4d5e62
Create Date: 2026-10-09 12:00:00.000000

Staff deep linking (app/lti_identity/links.py): ``lti_link_requests`` holds a pending
link (platform, LMS subject, a hash of the asserted email, single use, ten minutes);
``lti_user_links`` is the binding a staff member confirmed, signed in to TrueNorth.
Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "1a2b3c4d5e63"
down_revision: str | None = "1a2b3c4d5e62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "lti_user_links" not in tables:
        op.create_table(
            "lti_user_links",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=False),
            sa.Column("lti_sub", sa.String(255), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("lms_name", sa.String(255), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("platform_id", "lti_sub", name="uq_lti_user_link_sub"),
            sa.UniqueConstraint("platform_id", "user_id", name="uq_lti_user_link_user"),
        )
    if "lti_link_requests" not in tables:
        op.create_table(
            "lti_link_requests",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("bind_hash", sa.String(64), nullable=False),
            sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=False),
            sa.Column("lti_sub", sa.String(255), nullable=False),
            sa.Column("email_hash", sa.String(64), nullable=False),
            sa.Column("lms_name", sa.String(255), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_lti_link_requests_expires", "lti_link_requests", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_lti_link_requests_expires", table_name="lti_link_requests")
    op.drop_table("lti_link_requests")
    op.drop_table("lti_user_links")
