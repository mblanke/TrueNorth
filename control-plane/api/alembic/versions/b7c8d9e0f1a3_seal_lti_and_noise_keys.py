"""seal the LTI tool private key and the noise plan keys

Revision ID: b7c8d9e0f1a3
Revises: e4f5a6b7c8d9
Create Date: 2026-10-08 12:00:00.000000

The two secrets f4b5c6d7e8a9 left as stored: `lti_tool_keys.private_key_pem` (signs every
LTI, AGS, Moodle SSO and lab-access token) and `noise_profiles.plan_key` (signs the noise
plan handed to agents). Upgrade widens `plan_key` from String(64), too narrow for a sealed
value, to Text, then seals every value not sealed yet (app/secretbox.py, `TN_SECRETS_KEY`).
Downgrade unseals them and narrows `plan_key` back. Both need the key and refuse to run
without it if there is anything to convert; a database with neither secret needs no key.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app import secretbox

revision: str = "b7c8d9e0f1a3"
down_revision: str | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = {
    "lti_tool_keys": ("private_key_pem",),
    "noise_profiles": ("plan_key",),
}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _convert(convert, wanted) -> None:
    bind = op.get_bind()
    tables = _tables()
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


def _plan_key_type(new_type, old_type) -> None:
    if "noise_profiles" not in _tables():
        return
    with op.batch_alter_table("noise_profiles") as batch:
        batch.alter_column("plan_key", existing_type=old_type, type_=new_type, existing_nullable=False)


def upgrade() -> None:
    _plan_key_type(sa.Text(), sa.String(64))
    _convert(secretbox.seal, lambda v: not secretbox.is_sealed(v))


def downgrade() -> None:
    _convert(secretbox.unseal, secretbox.is_sealed)
    _plan_key_type(sa.String(64), sa.Text())
