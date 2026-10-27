"""allow only one provisioned subject token per local user

Revision ID: 7ab3c5e9f421
Revises: 6fa2b4d8e310
Create Date: 2026-10-24 12:15:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "7ab3c5e9f421"
down_revision: str | None = "6fa2b4d8e310"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("provisioned_subject_credentials") as batch:
        batch.create_unique_constraint("uq_provisioned_subject_user", ["user_id"])


def downgrade() -> None:
    with op.batch_alter_table("provisioned_subject_credentials") as batch:
        batch.drop_constraint("uq_provisioned_subject_user", type_="unique")
