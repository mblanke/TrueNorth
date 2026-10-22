"""Which QSP qualifications (and so which performance objectives) a tenant may read.

One rule for every QSP read, routers and services alike: the caller's own tenant's
qualifications plus the shared ones (``tenant_id IS NULL``) — the same rule
``tenancy.get_owned_or_global`` applies to a single catalogue row. There is no admin
bypass; a platform admin reads the spine as a member of their own tenant.

Issue #257: the learning-path and exercise generators read every tenant's
qualifications, so a course author could copy another tenant's objectives into their
own courses. Issue #227 fixed the router reads with an equivalent private helper.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Query, Session

from .models import PerformanceObjective, Qualification


def _as_uuid(tenant_id: uuid.UUID | str | None) -> uuid.UUID | None:
    if tenant_id is None or tenant_id == "":
        return None
    return tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))


def visible_qualifications(db: Session, tenant_id: uuid.UUID | str | None) -> Query:
    """Qualifications ``tenant_id`` may read: its own plus shared ones.

    With no tenant (``None``) only shared qualifications are visible — never all of them.
    """
    tid = _as_uuid(tenant_id)
    shared = Qualification.tenant_id.is_(None)
    if tid is None:
        return db.query(Qualification).filter(shared)
    return db.query(Qualification).filter((Qualification.tenant_id == tid) | shared)


def visible_objectives(db: Session, tenant_id: uuid.UUID | str | None) -> Query:
    """Performance objectives of the qualifications ``tenant_id`` may read."""
    visible = visible_qualifications(db, tenant_id).with_entities(Qualification.id)
    return db.query(PerformanceObjective).filter(PerformanceObjective.qualification_id.in_(visible))
