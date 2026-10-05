"""Reads of a range row shared by the range tasks (destroy, stop, start)."""

from __future__ import annotations

import json
import os


def provisioner_output(range_id: str) -> tuple[dict, str]:
    """(what provision recorded about the range's VMs, the backend that built them)."""
    from sqlalchemy import text

    from .tasks import _db_session

    with _db_session() as db:
        row = db.execute(
            text("SELECT provisioner_output, provisioner_backend FROM ranges WHERE id = :rid"),
            {"rid": range_id},
        ).first()
    output = json.loads(row[0]) if row and row[0] else {}
    backend = (row[1] if row and row[1] else None) or os.getenv("PROVISIONER_BACKEND", "mock")
    return output, backend
