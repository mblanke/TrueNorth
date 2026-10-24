"""opaque subject credential inbox

Revision ID: 5e9a1c3d7f20
Revises: 4d8f7a1c2b90
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import CHAR, TypeDecorator

revision: str = "5e9a1c3d7f20"
down_revision: str | None = "4d8f7a1c2b90"
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
        "provisioned_subject_credentials",
        sa.Column("credential_id", GUID(), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("opaque_subject_id", GUID(), nullable=False),
        sa.Column("source_enclave", sa.String(length=100), nullable=False),
        sa.Column("destination_enclave", sa.String(length=100), nullable=False),
        sa.Column("credential_json", sa.Text(), nullable=False),
        sa.Column("credential_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("credential_id"),
        sa.UniqueConstraint(
            "tenant_id", "source_enclave", "opaque_subject_id", name="uq_provisioned_subject_source_token"
        ),
    )
    op.create_index(
        "ix_provisioned_subject_tenant_state", "provisioned_subject_credentials", ["tenant_id", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_provisioned_subject_tenant_state", table_name="provisioned_subject_credentials")
    op.drop_table("provisioned_subject_credentials")
