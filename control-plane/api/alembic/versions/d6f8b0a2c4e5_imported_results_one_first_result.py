# Author: Marc-Andre Blanke
# Created: 2026-11-19T12:00:00Z
"""one first result per attempt

Revision ID: d6f8b0a2c4e5
Revises: c5e7a9b1d3f4
Create Date: 2026-11-19 12:00:00.000000

Audit #443 f34: ``import_result`` checks for an existing result of the attempt and then
inserts, so two concurrent first results of one attempt both landed. A partial unique index
now backs the check: at most one result per ``(tenant_id, source_enclave, attempt_id)`` with
no ``supersedes_package_id`` (a correction names its predecessor and keeps the attempt id,
so it is outside the index). Additive. The upgrade refuses, naming the attempts, if the
table already holds two first results of one attempt; resolve those by hand first.
Downgrade drops the index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6f8b0a2c4e5"
down_revision: str | None = "c5e7a9b1d3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_imported_result_first_per_attempt"
COLUMNS = ["tenant_id", "source_enclave", "attempt_id"]
WHERE = sa.text("supersedes_package_id IS NULL")


def _index_names(bind) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes("imported_results")}


def upgrade() -> None:
    bind = op.get_bind()
    if "imported_results" not in sa.inspect(bind).get_table_names() or INDEX in _index_names(bind):
        return
    dupes = bind.execute(
        sa.text(
            "SELECT source_enclave, attempt_id, COUNT(*) FROM imported_results "
            "WHERE supersedes_package_id IS NULL "
            "GROUP BY tenant_id, source_enclave, attempt_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dupes:
        raise RuntimeError(
            "imported_results holds more than one first result for these attempts "
            f"(source_enclave, attempt_id, count): {[tuple(map(str, d)) for d in dupes]}; "
            "keep one and supersede or remove the others, then upgrade again"
        )
    op.create_index(INDEX, "imported_results", COLUMNS, unique=True, postgresql_where=WHERE, sqlite_where=WHERE)


def downgrade() -> None:
    bind = op.get_bind()
    if "imported_results" in sa.inspect(bind).get_table_names() and INDEX in _index_names(bind):
        op.drop_index(INDEX, table_name="imported_results")
