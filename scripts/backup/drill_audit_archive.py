#!/usr/bin/env python3
"""The restore drill's audit export archive (ADR 0011), with the worker's own code.

    drill_audit_archive.py seed <dir>     write five days of signed exports (SQLite, a drill seed)
    drill_audit_archive.py verify <dir>   python -m worker.audit_verify's archive check: every
                                          signature and hash, and the chain; prints the summary

Needs the worker's cryptography and SQLAlchemy (backup-drill.yml installs them). Runs
from the checkout: control-plane/worker is put on the path.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "control-plane" / "worker"))

DRILL_SEED = "drill-audit-signing-seed-" + "d" * 32  # a drill key, never a real one


def seed(directory: str) -> int:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from worker import audit_signing, audit_tasks
    from worker.audit_archive import FilesystemArchive
    from worker.tables import audit_exports, audit_logs

    eng = sa.create_engine("sqlite://", poolclass=StaticPool)
    audit_logs.metadata.create_all(eng, tables=[audit_logs, audit_exports])
    today = datetime.now(UTC).date()
    with Session(eng) as db:
        for n in range(5, 0, -1):
            for i in range(n):  # a different count each day
                db.execute(audit_logs.insert().values(
                    id=uuid.uuid4(), timestamp=datetime.combine(today - timedelta(days=n), time(8, i), tzinfo=UTC),
                    user_id=None, tenant_id=None, action="drill", resource_type="probe", resource_id=str(i),
                    detail=None))
        db.commit()
        written = audit_tasks.run_export(db, FilesystemArchive(directory), audit_signing.from_seed(DRILL_SEED), today)
    print(f"{len(written)} signed exports in {directory}")
    return 0


def verify(directory: str) -> int:
    from worker.audit_archive import FilesystemArchive
    from worker.audit_verify import verify_archive

    problems, summary = verify_archive(FilesystemArchive(directory))
    print(json.dumps({**summary, "problems": problems}, sort_keys=True, default=str))
    return 0 if not problems else 1


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("seed", "verify"):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit({"seed": seed, "verify": verify}[sys.argv[1]](sys.argv[2]))
