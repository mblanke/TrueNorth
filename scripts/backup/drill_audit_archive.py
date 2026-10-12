#!/usr/bin/env python3
"""The restore drill's audit export archive (ADR 0011), with the worker's own code.

    drill_audit_archive.py seed <dir>     write five days of signed exports (SQLite, a drill seed)
    drill_audit_archive.py rows <dir>     the audit_exports rows those exports would have, as
                                          SQL VALUES tuples (the drill loads them into its database)
    drill_audit_archive.py extend <dir>   export one more day after the last (what the worker
                                          does after a backup was taken)
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
    from worker.tables import audit_export_checks, audit_exports, audit_logs

    eng = sa.create_engine("sqlite://", poolclass=StaticPool)
    audit_logs.metadata.create_all(eng, tables=[audit_logs, audit_exports, audit_export_checks])
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


def _manifests(directory: str) -> list[dict]:
    from worker.audit_archive import FilesystemArchive
    from worker.audit_export import sha256
    from worker.audit_verify import manifests

    archive = FilesystemArchive(directory)
    out = []
    for name in manifests(archive):
        raw = archive.get(name)
        out.append({**json.loads(raw), "_sha": sha256(raw)})
    return out


def rows(directory: str) -> int:
    tuples = [f"('{m['day']}', {m['seq']}, '{m['_sha']}', '{m['key_id']}')" for m in _manifests(directory)]
    print(",\n".join(tuples))
    return 0


def extend(directory: str) -> int:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from worker import audit_signing, audit_tasks
    from worker.audit_archive import FilesystemArchive
    from worker.tables import audit_export_checks, audit_exports, audit_logs

    eng = sa.create_engine("sqlite://", poolclass=StaticPool)
    audit_logs.metadata.create_all(eng, tables=[audit_logs, audit_exports, audit_export_checks])
    found = _manifests(directory)
    last = max(datetime.fromisoformat(m["day"]).date() for m in found)
    with Session(eng) as db:
        for m in found:  # the database state the worker had: every export so far
            db.execute(audit_exports.insert().values(
                day=datetime.fromisoformat(m["day"]).date(), seq=m["seq"], row_count=m["row_count"],
                first_id=None, last_id=None, file_name=m["file"], file_sha256=m["file_sha256"],
                manifest_sha256=m["_sha"], prev_manifest_sha256=m["prev_manifest_sha256"], key_id=m["key_id"],
                created_at=datetime.now(UTC)))
        new_day = last + timedelta(days=1)
        db.execute(audit_logs.insert().values(
            id=uuid.uuid4(), timestamp=datetime.combine(new_day, time(9), tzinfo=UTC), user_id=None,
            tenant_id=None, action="drill-after-backup", resource_type="probe", resource_id="late", detail=None))
        db.commit()
        written = audit_tasks.run_export(db, FilesystemArchive(directory), audit_signing.from_seed(DRILL_SEED),
                                         new_day + timedelta(days=1))
    print(f"{len(written)} more export(s): {', '.join(str(w.day) for w in written)}")
    return 0


def verify(directory: str) -> int:
    from worker.audit_archive import FilesystemArchive
    from worker.audit_verify import verify_archive

    problems, summary = verify_archive(FilesystemArchive(directory))
    print(json.dumps({**summary, "problems": problems}, sort_keys=True, default=str))
    return 0 if not problems else 1


if __name__ == "__main__":
    commands = {"seed": seed, "rows": rows, "extend": extend, "verify": verify}
    if len(sys.argv) != 3 or sys.argv[1] not in commands:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(commands[sys.argv[1]](sys.argv[2]))
