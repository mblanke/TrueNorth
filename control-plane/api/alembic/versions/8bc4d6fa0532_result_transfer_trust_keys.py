"""add purpose-bound public key trust registry

Revision ID: 8bc4d6fa0532
Revises: 7ab3c5e9f421
Create Date: 2026-10-24 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import CHAR, TypeDecorator

revision: str = "8bc4d6fa0532"
down_revision: str | None = "7ab3c5e9f421"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GUID(TypeDecorator):
    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))


def upgrade() -> None:
    op.create_table(
        "result_transfer_trust_keys",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("remote_enclave", sa.String(length=100), nullable=False),
        sa.Column("key_id", sa.String(length=16), nullable=False),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("purpose", "remote_enclave", "key_id", name="uq_result_trust_purpose_route_key"),
    )
    op.create_index(
        "ix_result_trust_route_active",
        "result_transfer_trust_keys",
        ["remote_enclave", "purpose", "active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_result_trust_route_active", table_name="result_transfer_trust_keys")
    op.drop_table("result_transfer_trust_keys")
