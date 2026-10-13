"""Signed daily exports of audit_logs, and their verification (ADR 0011).

Shared by the Celery tasks (audit_tasks.py) and the verifier (audit_verify.py). Core
statements on the generated mirror (tables.py); no raw SQL.

For each UTC day the archive holds three files::

    YYYY/MM/audit-YYYY-MM-DD[.sN].jsonl.gz           one canonical JSON object per row,
                                                     ordered by (timestamp, id)
    YYYY/MM/audit-YYYY-MM-DD[.sN].manifest.json      what the file is (below)
    YYYY/MM/audit-YYYY-MM-DD[.sN].manifest.json.sig  Ed25519 over the manifest's exact bytes

and ``keys/<key_id>.pub.pem``. ``.sN`` is a supplement (seq N > 0): rows of an already
exported day that committed after its export (a long transaction).

The manifest (compact JSON, sorted keys)::

    {"schema": 1, "day", "seq", "row_count", "first_id", "last_id", "file",
     "file_sha256", "prev_manifest_sha256", "key_id", "generated_at"}

``prev_manifest_sha256`` is the SHA-256 of the manifest written before it, so the
manifests form one chain in the order they were written: a missing, reordered or edited
manifest breaks it. An empty day still gets a manifest (row_count 0), so the chain has no
gaps.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import sqlalchemy as sa

from . import audit_signing
from .audit_archive import AuditArchive, AuditArchiveError
from .tables import audit_export_checks, audit_exports, audit_logs

logger = logging.getLogger("truenorth.worker.audit")

SCHEMA = 1
COLUMNS = ("id", "timestamp", "user_id", "tenant_id", "action", "resource_type", "resource_id", "detail")


class AuditExportError(RuntimeError):
    """An export or a verification failed; nothing was recorded as exported."""


# ── names and encodings ────────────────────────────────────────────────
def _stem(day: date, seq: int) -> str:
    return f"{day:%Y/%m}/audit-{day.isoformat()}" + (f".s{seq}" if seq else "")


def file_name(day: date, seq: int) -> str:
    return _stem(day, seq) + ".jsonl.gz"


def manifest_name(day: date, seq: int) -> str:
    return _stem(day, seq) + ".manifest.json"


def key_name(key_id: str) -> str:
    return f"keys/{key_id}.pub.pem"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _value(col: str, v: Any) -> Any:
    if v is None:
        return None
    if col == "timestamp":
        return _utc(v).isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v if isinstance(v, str | int | float | bool) else str(v)


def row_json(row: Any) -> bytes:
    m = row._mapping if hasattr(row, "_mapping") else row
    return json.dumps({c: _value(c, m[c]) for c in COLUMNS}, sort_keys=True, separators=(",", ":")).encode()


def encode_rows(rows: Iterable[Any]) -> bytes:
    """Deterministic gzip (mtime 0) of the JSON lines: the same rows, the same bytes."""
    return gzip.compress(b"".join(row_json(r) + b"\n" for r in rows), mtime=0)


def decode_ids(data: bytes) -> list[str]:
    return [json.loads(line)["id"] for line in gzip.decompress(data).splitlines() if line.strip()]


def manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


# ── database reads ─────────────────────────────────────────────────────
def day_rows(db, day: date) -> list[Any]:
    start, end = day_bounds(day)
    q = (
        sa.select(*[audit_logs.c[c] for c in COLUMNS])
        .where(audit_logs.c.timestamp >= start, audit_logs.c.timestamp < end)
        .order_by(audit_logs.c.timestamp, audit_logs.c.id)
    )
    return list(db.execute(q))


def day_count(db, day: date) -> int:
    start, end = day_bounds(day)
    q = sa.select(sa.func.count()).select_from(audit_logs).where(
        audit_logs.c.timestamp >= start, audit_logs.c.timestamp < end
    )
    return int(db.execute(q).scalar() or 0)


def exports_for(db, day: date) -> list[Any]:
    return list(db.execute(sa.select(audit_exports).where(audit_exports.c.day == day).order_by(audit_exports.c.seq)))


def chain_head(db) -> str | None:
    """The manifest_sha256 no other export points back to (the last written), or None."""
    rows = list(db.execute(sa.select(audit_exports.c.manifest_sha256, audit_exports.c.prev_manifest_sha256)))
    if not rows:
        return None
    referenced = {r.prev_manifest_sha256 for r in rows if r.prev_manifest_sha256}
    heads = [r.manifest_sha256 for r in rows if r.manifest_sha256 not in referenced]
    if len(heads) != 1:
        raise AuditExportError(f"audit_exports does not form one chain ({len(heads)} heads)")
    return heads[0]


# ── writing and checking one export ────────────────────────────────────
@dataclass(frozen=True)
class Written:
    day: date
    seq: int
    row_count: int
    manifest_sha256: str


def trusted_key_ids(db, key: audit_signing.SigningKey | None = None) -> set[str]:
    """Key ids allowed to have signed an export: the worker's current key and every key id
    audit_exports recorded (prior keys, after a rotation). A PEM someone drops into the
    archive's keys/ is never trusted on its own."""
    ids = {r[0] for r in db.execute(sa.select(audit_exports.c.key_id).distinct())}
    if key is not None:
        ids.add(key.key_id)
    return ids


def verify_manifest(
    archive: AuditArchive,
    name: str,
    key: audit_signing.SigningKey | None = None,
    *,
    trusted: set[str] | None = None,
    expected_sha: str | None = None,
) -> dict:
    """Read a manifest back and check its signature and its file's hash. Returns it.

    With ``key`` (the worker's current key) a manifest signed by that key id is checked
    against it, not against the PEM stored next to it in the archive. With ``trusted``
    the manifest must be signed by one of those key ids (trusted_key_ids); with
    ``expected_sha`` its bytes must hash to what audit_exports recorded."""
    try:
        mbytes = archive.get(name)
        sig = archive.get(name + ".sig")
        manifest = json.loads(mbytes)
        if expected_sha is not None and sha256(mbytes) != expected_sha:
            raise AuditExportError(f"{name}: does not match audit_exports.manifest_sha256")
        if trusted is not None and manifest.get("key_id") not in trusted:
            raise AuditExportError(f"{name}: signed by key {manifest.get('key_id')}, which this database does not know")
        if key is not None and manifest.get("key_id") == key.key_id:
            pem = key.public_pem
        else:
            pem = archive.get(key_name(manifest["key_id"]))
        from cryptography.hazmat.primitives import serialization

        if audit_signing.key_id_of(serialization.load_pem_public_key(pem)) != manifest["key_id"]:
            raise AuditExportError(f"{name}: the public key does not match key_id {manifest['key_id']}")
        if not audit_signing.verify(pem, mbytes, sig):
            raise AuditExportError(f"{name}: signature does not verify")
        if sha256(archive.get(manifest["file"])) != manifest["file_sha256"]:
            raise AuditExportError(f"{name}: {manifest['file']} does not match its file_sha256")
    except (AuditArchiveError, ValueError, KeyError) as exc:
        raise AuditExportError(f"{name}: {exc}") from exc
    return manifest


def write_export(
    db,
    archive: AuditArchive,
    key: audit_signing.SigningKey,
    day: date,
    seq: int,
    rows: list[Any],
    prev: str | None,
    *,
    now: datetime | None = None,
) -> Written:
    """Write file, manifest and signature; read them back and verify; only then record the
    export in audit_exports (and commit). A rerun after a crash between the archive and
    the database adopts the files already written when they describe the same rows."""
    data = encode_rows(rows)
    mname = manifest_name(day, seq)
    archive.put(key_name(key.key_id), key.public_pem)
    if archive.exists(mname):
        existing = verify_manifest(archive, mname, key, trusted=trusted_key_ids(db, key))
        if (existing["file_sha256"], existing["prev_manifest_sha256"], existing["row_count"]) != (
            sha256(data), prev, len(rows)
        ):
            raise AuditExportError(
                f"{mname} already exists and describes other rows or another chain position: the archive "
                "holds exports the database does not (a database restored without restore.sh's archive "
                "reconciliation?). docs/runbooks/backup-restore.md, \"Archive newer than the database\""
            )
        mbytes = archive.get(mname)
        logger.warning("audit export %s: adopting files written by an interrupted run", mname)
    else:
        ids = [str(r._mapping["id"]) for r in rows]
        manifest = {
            "schema": SCHEMA,
            "day": day.isoformat(),
            "seq": seq,
            "row_count": len(rows),
            "first_id": ids[0] if ids else None,
            "last_id": ids[-1] if ids else None,
            "file": file_name(day, seq),
            "file_sha256": sha256(data),
            "prev_manifest_sha256": prev,
            "key_id": key.key_id,
            "generated_at": (now or datetime.now(UTC)).isoformat(),
        }
        mbytes = manifest_bytes(manifest)
        archive.put(file_name(day, seq), data)
        archive.put(mname, mbytes)
        archive.put(mname + ".sig", key.sign(mbytes))
        verify_manifest(archive, mname, key, trusted={key.key_id})
    ids = [r._mapping["id"] for r in rows]
    db.execute(
        audit_exports.insert().values(
            day=day,
            seq=seq,
            row_count=len(rows),
            first_id=ids[0] if ids else None,
            last_id=ids[-1] if ids else None,
            file_name=file_name(day, seq),
            file_sha256=sha256(data),
            manifest_sha256=sha256(mbytes),
            prev_manifest_sha256=prev,
            key_id=key.key_id,
            created_at=now or datetime.now(UTC),
        )
    )
    db.commit()
    return Written(day, seq, len(rows), sha256(mbytes))


def record_check(db, export_row: Any) -> None:
    """The re-verification the database trigger requires before a purge (same transaction)."""
    db.execute(audit_export_checks.insert().values(
        id=uuid.uuid4(), day=export_row.day, seq=export_row.seq, manifest_sha256=export_row.manifest_sha256,
        row_count=export_row.row_count, key_id=export_row.key_id, checked_at=datetime.now(UTC),
    ))


def exported_ids(archive: AuditArchive, db, day: date, key: audit_signing.SigningKey | None = None) -> set[str]:
    """Every row id in the verified files of ``day``'s exports."""
    out: set[str] = set()
    trusted = trusted_key_ids(db, key)
    for e in exports_for(db, day):
        manifest = verify_manifest(archive, manifest_name(day, e.seq), key, trusted=trusted,
                                   expected_sha=e.manifest_sha256)
        out.update(decode_ids(archive.get(manifest["file"])))
    return out


# ── retention settings (shared by the tasks and the verifier) ─────────
DEFAULT_HOT_DAYS = 90
FLOOR_DAYS = 30  # the database trigger refuses younger rows whatever this says


def hot_retention_days() -> int:
    """AUDIT_HOT_RETENTION_DAYS, clamped to at least 30; 0 disables purging."""
    import os

    raw = os.getenv("AUDIT_HOT_RETENTION_DAYS", str(DEFAULT_HOT_DAYS)).strip() or str(DEFAULT_HOT_DAYS)
    days = int(raw)
    if days <= 0:
        return 0
    return max(days, FLOOR_DAYS)
