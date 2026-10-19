"""range_credentials: the accounts a range's template declares, sealed (ADR 0012 §12-14)

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-10-19 12:00:00.000000

Additive: one new table and one new column, ``wiki_spaces.system_owned`` (default false:
every existing space stays as it is), no data migration. Existing ranges
get rows the next time a build of them is accepted (app/range_credentials/service.py);
until then they have none, and their VMs keep the passwords they were built with.

``password`` holds a sealed value (``tnsec:v1:...``, app/secretbox.py), never plaintext:
the ORM column is ``SealedText``. Frozen here as plain Text.

Downgrade drops the table (and with it every stored range password) and the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "range_credentials",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("machine", sa.String(255), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column("generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("student_visible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("range_id", "machine", "username", name="uq_range_credentials_account"),
    )
    op.create_index("ix_range_credentials_range", "range_credentials", ["range_id"])
    op.create_index("ix_range_credentials_tenant", "range_credentials", ["tenant_id"])
    # The space of generated range pages is the platform's: always staff-only (ADR 0012 §14).
    with op.batch_alter_table("wiki_spaces") as batch:
        batch.add_column(sa.Column("system_owned", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("wiki_spaces") as batch:
        batch.drop_column("system_owned")
    op.drop_index("ix_range_credentials_tenant", table_name="range_credentials")
    op.drop_index("ix_range_credentials_range", table_name="range_credentials")
    op.drop_table("range_credentials")
