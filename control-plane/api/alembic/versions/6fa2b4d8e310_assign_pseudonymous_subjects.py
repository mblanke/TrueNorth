"""assign imported subject credentials to pseudonymous users

Revision ID: 6fa2b4d8e310
Revises: 5e9a1c3d7f20
Create Date: 2026-10-24 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import CHAR, TypeDecorator

revision: str = "6fa2b4d8e310"
down_revision: str | None = "5e9a1c3d7f20"
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
    with op.batch_alter_table("provisioned_subject_credentials") as batch:
        batch.add_column(sa.Column("user_id", GUID(), nullable=True))
        batch.add_column(sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_provisioned_subject_user", "users", ["user_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("provisioned_subject_credentials") as batch:
        batch.drop_constraint("fk_provisioned_subject_user", type_="foreignkey")
        batch.drop_column("assigned_at")
        batch.drop_column("user_id")
