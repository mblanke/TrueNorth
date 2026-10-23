"""acknowledgement form: versions and signatures

Revision ID: 4c0dab4abfa8
Revises: c1465c4964a6
Create Date: 2026-10-23 12:00:00.000000

Classes 2 (ADR 0014 decision 3; issue #279), ``app/acknowledgement``:

* ``acknowledgement_versions``: the rules-of-use text (Markdown), platform-wide. A draft
  until published; publishing numbers it and freezes it.
* ``acknowledgement_signatures``: who signed which version, when, in which tenant, as which
  email, name and role, from which address and browser.

Additive. One unpublished draft (the shipped default text) is added when the table is
empty, so nobody is asked to sign until an administrator publishes a version. Downgrade
drops both tables.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.acknowledgement.default_text import DEFAULT_TEXT
from app.models import GUID

revision: str = "4c0dab4abfa8"
down_revision: str | None = "c1465c4964a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "acknowledgement_versions" not in existing:
        versions = op.create_table(
            "acknowledgement_versions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("version", sa.Integer(), nullable=True),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("version", name="uq_acknowledgement_versions_version"),
        )
        op.bulk_insert(versions, [{"id": uuid.uuid4(), "version": None, "text": DEFAULT_TEXT}])
    if "acknowledgement_signatures" not in existing:
        op.create_table(
            "acknowledgement_signatures",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("version_id", GUID(), sa.ForeignKey("acknowledgement_versions.id"), nullable=False),
            sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("email", sa.String(255), nullable=False, server_default=""),
            sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("role", sa.String(32), nullable=False, server_default=""),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column("user_agent", sa.String(255), nullable=True),
        )
        op.create_index("ix_ack_signatures_user_version", "acknowledgement_signatures",
                        ["user_id", "version_id", "signed_at"])
        op.create_index("ix_ack_signatures_tenant_signed", "acknowledgement_signatures", ["tenant_id", "signed_at"])


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "acknowledgement_signatures" in existing:
        op.drop_index("ix_ack_signatures_tenant_signed", table_name="acknowledgement_signatures")
        op.drop_index("ix_ack_signatures_user_version", table_name="acknowledgement_signatures")
        op.drop_table("acknowledgement_signatures")
    if "acknowledgement_versions" in existing:
        op.drop_table("acknowledgement_versions")
