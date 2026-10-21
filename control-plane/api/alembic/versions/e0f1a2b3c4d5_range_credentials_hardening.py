"""range credentials hardening: rc1 sealing, implicit staff logins, rotation, no wiki secrets (ADR 0013 §3)

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-10-21 12:00:00.000000

Schema (additive): ``range_credentials`` gains ``source`` (declared | implicit),
``scenario`` (a typed-in password authored by design), ``rotated_at`` and
``previous_password`` (sealed; set only while an in-guest rotation is pending); a new
``range_credential_snapshots`` table keeps, per range snapshot, the passwords the accounts
had when it was taken (sealed).

Data (app/range_credentials/migrate_0013.py, Core SQL only): every stored range password is
resealed ``tnsec:v1`` -> ``tnsec:rc1``; every existing range gets its implicit staff logins
(generated, not applied, noted "template password still valid until rebuilt"); every
generated wiki range page is rendered again without credentials and the
``{{tn-credential:...}}`` tokens are stripped from earlier revisions.

Downgrade reseals rc1 -> v1 (the old code reads only v1) and drops the new table and
columns. Implicit rows stay (the old code shows them as declared accounts until the next
build of the range drops them); stripped wiki tokens are not put back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "e0f1a2b3c4d5"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("range_credentials") as batch:
        batch.add_column(sa.Column("source", sa.String(16), nullable=False, server_default="declared"))
        batch.add_column(sa.Column("scenario", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("previous_password", sa.Text(), nullable=True))
    op.create_table(
        "range_credential_snapshots",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("snapshot_id", GUID(), sa.ForeignKey("range_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", GUID(), sa.ForeignKey("range_credentials.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "credential_id", name="uq_range_credential_snapshots"),
    )
    op.create_index("ix_range_credential_snapshots_snapshot", "range_credential_snapshots", ["snapshot_id"])

    from app.range_credentials import migrate_0013

    migrate_0013.upgrade(op.get_bind())


def downgrade() -> None:
    from app.range_credentials import migrate_0013

    migrate_0013.downgrade(op.get_bind())
    op.drop_index("ix_range_credential_snapshots_snapshot", table_name="range_credential_snapshots")
    op.drop_table("range_credential_snapshots")
    with op.batch_alter_table("range_credentials") as batch:
        batch.drop_column("previous_password")
        batch.drop_column("rotated_at")
        batch.drop_column("scenario")
        batch.drop_column("source")
