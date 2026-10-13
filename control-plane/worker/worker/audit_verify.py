"""Verify the signed audit export archive, and optionally the database against it.

    python -m worker.audit_verify [--dir /var/lib/truenorth/audit-archive] [--db] [--json]

Run by scripts/backup/drill.sh after a restore (and by hand). It checks:

1. every manifest's Ed25519 signature (with ``keys/<key_id>.pub.pem`` from the archive)
   and its file's SHA-256;
2. the chain: following ``prev_manifest_sha256`` from the one manifest nothing points to
   reaches the first one (prev null) and visits every manifest; a missing, edited or
   reordered manifest breaks it;
3. with ``--db`` (DATABASE_URL): every manifest is signed by a key id the database
   recorded (or the worker's current key), audit_exports and the archive agree (same
   manifests, same hashes), and every exported day still has exactly the exported number
   of rows, or none at all when the day is older than the configured retention
   (AUDIT_HOT_RETENTION_DAYS; purged).

Without ``--db`` the public keys come from the archive itself and are only as trustworthy
as the archive: compare ``key_id`` with the fingerprint recorded out of band at install
(ADR 0011, residual risk). ``quarantine/`` (archive days moved aside by restore.sh) is not
part of the chain and is not checked.
Exit 0 when everything verifies, 1 otherwise; problems are printed one per line.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from .audit_archive import AuditArchive, FilesystemArchive
from .audit_export import AuditExportError, sha256, verify_manifest


def manifests(archive: AuditArchive) -> list[str]:
    return [n for n in archive.list() if n.endswith(".manifest.json") and not n.startswith("quarantine/")]


def verify_archive(archive: AuditArchive, trusted: set[str] | None = None) -> tuple[list[str], dict]:
    """(problems, summary). ``trusted``: the only key ids allowed (audit_export.trusted_key_ids)."""
    problems: list[str] = []
    by_sha: dict[str, dict] = {}
    for name in manifests(archive):
        try:
            m = verify_manifest(archive, name, trusted=trusted)
        except AuditExportError as exc:
            problems.append(str(exc))
            continue
        by_sha[sha256(archive.get(name))] = {**m, "_name": name}
    referenced = {m["prev_manifest_sha256"] for m in by_sha.values() if m["prev_manifest_sha256"]}
    missing_prev = sorted(referenced - set(by_sha))
    for sha in missing_prev:
        problems.append(f"chain broken: a manifest points back to {sha[:16]}..., which is not in the archive")
    heads = [sha for sha in by_sha if sha not in referenced]
    if by_sha and len(heads) != 1:
        problems.append(f"chain broken: {len(heads)} manifests have no successor (expected 1)")
    visited: list[str] = []
    if len(heads) == 1:
        sha: str | None = heads[0]
        while sha and sha in by_sha and sha not in visited:
            visited.append(sha)
            sha = by_sha[sha]["prev_manifest_sha256"]
        if sha in visited:
            problems.append("chain broken: it loops")
        unvisited = [by_sha[s]["_name"] for s in by_sha if s not in visited]
        if unvisited:
            problems.append(f"chain broken: not on the chain: {', '.join(sorted(unvisited))}")
    chain = [by_sha[s] for s in reversed(visited)]
    summary = {
        "manifests": len(by_sha),
        "first_day": chain[0]["day"] if chain else None,
        "last_day": chain[-1]["day"] if chain else None,
        "rows": sum(m["row_count"] for m in by_sha.values()),
        "key_ids": sorted({m["key_id"] for m in by_sha.values()}),
    }
    return problems, summary


def verify_database(db, archive: AuditArchive, today: date, floor_days: int | None = None) -> list[str]:
    """``floor_days``: the retention in force (default AUDIT_HOT_RETENTION_DAYS; 0 = never purged)."""
    import sqlalchemy as sa

    from .audit_export import day_count, hot_retention_days, manifest_name
    from .tables import audit_exports

    problems: list[str] = []
    rows = list(db.execute(sa.select(audit_exports)))
    names = {manifest_name(r.day, r.seq): r for r in rows}
    in_archive = set(manifests(archive))
    for name in sorted(set(names) - in_archive):
        problems.append(f"audit_exports lists {name}, which is not in the archive")
    for name in sorted(in_archive - set(names)):
        problems.append(f"{name} is in the archive but not in audit_exports")
    for name, r in names.items():
        if name in in_archive and sha256(archive.get(name)) != r.manifest_sha256:
            problems.append(f"{name}: audit_exports.manifest_sha256 does not match the archived manifest")
    if floor_days is None:
        floor_days = hot_retention_days()
    floor = (today - timedelta(days=floor_days)) if floor_days else date.min
    totals: dict[date, int] = {}
    for r in rows:
        totals[r.day] = totals.get(r.day, 0) + r.row_count
    for day, exported in sorted(totals.items()):
        live = day_count(db, day)
        if live == exported:
            continue
        if live == 0 and day < floor:
            continue  # purged by retention after the export
        problems.append(f"{day}: {live} rows in audit_logs, {exported} exported")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dir", help="the archive directory (default AUDIT_ARCHIVE_DIR)")
    ap.add_argument("--db", action="store_true", help="also check audit_logs/audit_exports (DATABASE_URL)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    archive = FilesystemArchive(args.dir)
    if args.db:
        from datetime import UTC, datetime

        from . import audit_signing
        from .audit_export import trusted_key_ids
        from .task_plumbing import db_session

        try:
            key = audit_signing.load()
        except audit_signing.SigningKeyError:
            key = None  # verify with the key ids the database recorded
        with db_session() as db:
            problems, summary = verify_archive(archive, trusted_key_ids(db, key))
            problems += verify_database(db, archive, datetime.now(UTC).date())
    else:
        problems, summary = verify_archive(archive)
    summary["verified"] = not problems
    summary["problems"] = problems
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        for p in problems:
            print(f"FAIL {p}")
        print(f"audit archive {archive.root}: {summary['manifests']} manifests, {summary['rows']} rows, "
              f"{summary['first_day']} .. {summary['last_day']}, keys {', '.join(summary['key_ids']) or '-'}: "
              + ("verified" if not problems else f"{len(problems)} problem(s)"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
