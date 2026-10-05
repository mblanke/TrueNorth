"""seal stored credentials: hypervisor passwords and tokens, AI engine keys

Revision ID: 23df1b265fd2
Revises: f7a8b9c0d1e2
Create Date: 2026-10-05 12:00:00.000000

Data only, no schema change. `hypervisor_connections.password_encrypted`, `.api_token` and
`ai_backend_configs.api_key_encrypted` held the secrets exactly as typed. Upgrade seals every
value that is not sealed yet (app/secretbox.py, key `TN_SECRETS_KEY`); downgrade unseals them
again, for a rollback to code that reads them as typed. Both need the key, and refuse to run
without it if there is anything to convert, leaving every row as it was. A database with no
credentials needs no key.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app import secretbox

revision: str = "23df1b265fd2"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = {
    "hypervisor_connections": ("password_encrypted", "api_token"),
    "ai_backend_configs": ("api_key_encrypted",),
}


def _convert(convert, wanted) -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, columns in COLUMNS.items():
        if table not in tables:
            continue
        for column in columns:
            rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} <> ''"))
            for row_id, value in rows.fetchall():
                if wanted(value):
                    bind.execute(
                        sa.text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": convert(value), "id": row_id}
                    )


def upgrade() -> None:
    _convert(secretbox.seal, lambda v: not secretbox.is_sealed(v))


def downgrade() -> None:
    _convert(secretbox.unseal, secretbox.is_sealed)
