"""Drafts, publication, signatures and the signature export (ADR 0014 decision 3; #279).

The rules:

- A version is a draft until a platform administrator publishes it. A draft can be edited;
  a published version never changes. A correction is a new draft, published as the next
  version, which everyone then signs.
- Publishing gives the draft the next version number. Two publishes at once cannot share a
  number (unique constraint); the loser gets 409.
- A signature is of the current version only. Signing while a valid signature already
  exists records nothing new.
- Publishing, signing and exporting are audited. Publishing is platform-wide, so its audit
  row has no tenant (only the platform administrator sees it, as for other cross-tenant
  rows); a signature's row is in the signer's tenant.
"""

from __future__ import annotations

import csv
import io
import json
import uuid

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditLog, Tenant, User
from . import rule
from .default_text import DEFAULT_TEXT
from .models import AcknowledgementSignature, AcknowledgementVersion


class AcknowledgementError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _audit(db: Session, *, user_id, tenant_id, action: str, resource_id: str, detail: dict) -> None:
    db.add(AuditLog(user_id=uuid.UUID(str(user_id)), tenant_id=tenant_id, action=action,
                    resource_type="acknowledgement_version", resource_id=resource_id,
                    detail=json.dumps(detail, sort_keys=True)))


def seed_default_draft(db: Session) -> bool:
    """Add the shipped draft when there is no version at all. Never publishes it: nobody is
    asked to sign until an administrator publishes a version. Returns whether it added one."""
    if db.query(AcknowledgementVersion.id).first() is not None:
        return False
    db.add(AcknowledgementVersion(text=DEFAULT_TEXT))
    db.flush()
    return True


def list_versions(db: Session) -> list[AcknowledgementVersion]:
    """Drafts first (newest first), then published versions, newest first."""
    rows = db.query(AcknowledgementVersion).all()
    drafts = sorted((v for v in rows if v.version is None), key=lambda v: str(v.created_at or ""), reverse=True)
    published = sorted((v for v in rows if v.version is not None), key=lambda v: v.version, reverse=True)
    return drafts + published


def _get(db: Session, version_id: uuid.UUID) -> AcknowledgementVersion:
    row = db.get(AcknowledgementVersion, version_id)
    if row is None:
        raise AcknowledgementError(404, "No such version")
    return row


def create_draft(db: Session, actor_id, text: str) -> AcknowledgementVersion:
    row = AcknowledgementVersion(text=text, created_by=uuid.UUID(str(actor_id)))
    db.add(row)
    db.flush()
    return row


def update_draft(db: Session, version_id: uuid.UUID, text: str) -> AcknowledgementVersion:
    row = _get(db, version_id)
    if row.is_published:
        raise AcknowledgementError(409, f"Version {row.version} is published and cannot change; "
                                         "make a new draft and publish it")
    row.text = text
    db.flush()
    return row


def publish(db: Session, version_id: uuid.UUID, actor_id) -> AcknowledgementVersion:
    row = _get(db, version_id)
    if row.is_published:
        raise AcknowledgementError(409, f"Version {row.version} is already published")
    latest = db.query(func.max(AcknowledgementVersion.version)).scalar() or 0
    row.version = latest + 1
    row.published_at = rule.now()
    row.published_by = uuid.UUID(str(actor_id))
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise AcknowledgementError(409, "Another version was published at the same time; reload") from exc
    _audit(db, user_id=actor_id, tenant_id=None, action="acknowledgement_publish", resource_id=str(row.id),
           detail={"version": row.version, "chars": len(row.text)})
    return row


def sign(db: Session, user, version_id: uuid.UUID, *, ip: str | None, user_agent: str | None) -> rule.Status:
    """Record ``user``'s signature of the current version. ``user``: a CurrentUser."""
    current = rule.current_version(db)
    if current is None:
        raise AcknowledgementError(409, "There are no rules of use to sign yet")
    if current.id != version_id:
        raise AcknowledgementError(409, "A newer version of the rules of use has been published; read it first")
    before = rule.status(db, user.id)
    if before.required:
        at = rule.now()
        db.add(AcknowledgementSignature(
            user_id=uuid.UUID(str(user.id)), tenant_id=uuid.UUID(str(user.tenant_id)), version_id=current.id,
            signed_at=at, ip_address=(ip or None) and ip[:64], user_agent=(user_agent or None) and user_agent[:255],
        ))
        _audit(db, user_id=user.id, tenant_id=uuid.UUID(str(user.tenant_id)), action="acknowledgement_sign",
               resource_id=str(current.id),
               detail={"version": current.version, "reason": before.reason, "ip": ip or None})
        db.flush()
    return rule.status(db, user.id)


# A spreadsheet runs a cell that starts with one of these as a formula (CSV injection).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


EXPORT_COLUMNS = ("signed_at", "email", "display_name", "role", "tenant", "version", "ip_address", "user_agent")


def export_csv(db: Session, tenant_id: uuid.UUID | None) -> tuple[str, int]:
    """Every signature (``tenant_id`` None: the platform administrator) or one tenant's, oldest
    first, as CSV text with formula cells defused. Returns (text, row count)."""
    q = (
        db.query(AcknowledgementSignature, User, AcknowledgementVersion, Tenant)
        .join(User, User.id == AcknowledgementSignature.user_id)
        .join(AcknowledgementVersion, AcknowledgementVersion.id == AcknowledgementSignature.version_id)
        .join(Tenant, Tenant.id == AcknowledgementSignature.tenant_id)
    )
    if tenant_id is not None:
        q = q.filter(AcknowledgementSignature.tenant_id == tenant_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    n = 0
    for sig, user, version, tenant in q.order_by(AcknowledgementSignature.signed_at, User.email):
        role = getattr(user.role, "value", user.role)
        writer.writerow([csv_cell(v) for v in (
            rule.aware(sig.signed_at).isoformat(), user.email, user.display_name, role, tenant.name,
            version.version, sig.ip_address, sig.user_agent,
        )])
        n += 1
    return out.getvalue(), n


def audit_export(db: Session, actor, scope: str, rows: int) -> None:
    db.add(AuditLog(user_id=uuid.UUID(str(actor.id)), tenant_id=None if scope == "all" else uuid.UUID(str(actor.tenant_id)),
                    action="acknowledgement_export", resource_type="acknowledgement_signatures",
                    resource_id=scope, detail=json.dumps({"rows": rows, "scope": scope}, sort_keys=True)))
