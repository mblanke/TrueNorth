"""seal stored credentials: hypervisor passwords and tokens, AI engine keys

Revision ID: f4b5c6d7e8a9
Revises: e3a4b5c6d7e8
Create Date: 2026-10-07 12:00:00.000000

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

revision: str = "f4b5c6d7e8a9"
down_revision: str | None = "e3a4b5c6d7e8"
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
        t = sa.table(table, sa.column("id"), *(sa.column(c) for c in columns))
        for column in columns:
            col = t.c[column]
            rows = bind.execute(sa.select(t.c.id, col).where(col.is_not(None), col != "")).fetchall()
            for row_id, value in rows:
                if wanted(value):
                    bind.execute(sa.update(t).where(t.c.id == row_id).values({column: convert(value)}))


def upgrade() -> None:
    _convert(secretbox.seal, lambda v: not secretbox.is_sealed(v))


def downgrade() -> None:
    _convert(secretbox.unseal, secretbox.is_sealed)
