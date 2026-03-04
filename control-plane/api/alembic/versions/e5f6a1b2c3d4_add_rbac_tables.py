"""add rbac tables

Revision ID: e5f6a1b2c3d4
Revises: d4e5f6a1b2c3
Create Date: 2026-02-26 06:04:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.models import GUID

revision: str = "e5f6a1b2c3d4"
down_revision: Union[str, None] = "d4e5f6a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "custom_permissions",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("permission", sa.String(length=100), nullable=False),
        sa.Column("granted_by", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "tenant_id", "permission", name="uq_user_tenant_permission"),
    )
    op.create_index("ix_custom_perms_user", "custom_permissions", ["user_id"])
    op.create_index("ix_custom_perms_tenant", "custom_permissions", ["tenant_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("permissions_json", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_keys_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_user", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_user", table_name="api_keys")
    op.drop_index("ix_api_keys_hash", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_custom_perms_tenant", table_name="custom_permissions")
    op.drop_index("ix_custom_perms_user", table_name="custom_permissions")
    op.drop_table("custom_permissions")