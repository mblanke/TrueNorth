"""range credentials hardening: bound seals, implicit staff logins, rotation, no wiki secrets (ADR 0013 §3)

Revision ID: 0a2276639b3c
Revises: e0f1a2b3c4d5
Create Date: 2026-10-21 12:00:00.000000

Schema (additive): ``range_credentials`` gains ``source`` (declared | implicit),
``scenario`` (a typed-in password authored by design), ``rotated_at`` and
``previous_password`` (sealed; set only while an in-guest change is pending) and
``pending_operation`` (the operation that change belongs to); a new
``range_credential_snapshots`` table keeps, per range snapshot, the passwords the accounts
had when it was taken (sealed).

Data (app/range_credentials/migrate_0013.py, Core SQL only): every stored range password is
resealed as ``tnsec:rk1`` bound to its tenant, range and row, and every typed-in template
password as ``tnsec:rt1`` bound to its template's tenant; every existing range gets its
implicit staff logins (generated, not applied, noted "template password still valid until
rebuilt"); templates whose typed-in passwords are not on scenario accounts are logged as
ACTION NEEDED; every generated wiki range page is rendered again without credentials and the
``{{tn-credential:...}}`` tokens are stripped from earlier revisions.

Downgrade first puts every pending change back to the password the guests have
(``previous_password``), then reseals rows as ``tnsec:v1`` and template passwords as ``tnsec:rc1`` (what the old
code reads) and drops the new table and
columns. Implicit rows stay (the old code shows them as declared accounts until the next
build of the range drops them); stripped wiki tokens are not put back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "0a2276639b3c"
down_revision: str | None = "e0f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("range_credentials") as batch:
        batch.add_column(sa.Column("source", sa.String(16), nullable=False, server_default="declared"))
        batch.add_column(sa.Column("scenario", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("previous_password", sa.Text(), nullable=True))
        batch.add_column(sa.Column("pending_operation", sa.String(64), nullable=True))
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
        batch.drop_column("pending_operation")
        batch.drop_column("previous_password")
        batch.drop_column("rotated_at")
        batch.drop_column("scenario")
        batch.drop_column("source")
