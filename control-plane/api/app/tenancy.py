"""Tenant-scoped lookups.

Every by-id fetch of a tenant-owned row must go through here.

The failure this prevents is quiet: a `db.query(Model).filter(Model.id == x)` with no
tenant predicate returns another tenant's row with a 200 and a well-formed body. List
endpoints are usually scoped correctly, which makes the surface *look* safe, and no
existing test notices because the response shape is right.

404, never 403: telling a caller "this exists but is not yours" is itself disclosure and
lets them enumerate ids across tenants.
"""

from __future__ import annotations

import uuid
from typing import TypeVar

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .auth import CurrentUser

T = TypeVar("T")


def tenant_uuid(user: CurrentUser) -> uuid.UUID:
    """The caller's tenant as a UUID, tolerant of str/UUID on CurrentUser."""
    tid = user.tenant_id
    return tid if isinstance(tid, uuid.UUID) else uuid.UUID(str(tid))


def get_owned(
    db: Session,
    model: type[T],
    obj_id: uuid.UUID | str,
    user: CurrentUser,
    *,
    not_found: str | None = None,
) -> T:
    """Fetch `model` by id, scoped to the caller's tenant, or raise 404.

    Honours a `deleted_at` soft-delete column when the model has one.
    """
    oid = obj_id if isinstance(obj_id, uuid.UUID) else uuid.UUID(str(obj_id))
    q = db.query(model).filter(
        model.id == oid,
        model.tenant_id == tenant_uuid(user),
    )
    if hasattr(model, "deleted_at"):
        q = q.filter(model.deleted_at.is_(None))
    obj = q.first()
    if not obj:
        raise HTTPException(404, not_found or f"{model.__name__} not found")
    return obj


def owned_or_404(
    db: Session,
    model: type[T],
    ids: list,
    user: CurrentUser,
) -> list[T]:
    """Bulk equivalent: every id must belong to the caller's tenant.

    Raises 404 if any id is missing or foreign — deliberately not a partial result,
    so a batch operation cannot silently act on the subset it happens to own.
    """
    q = db.query(model).filter(
        model.id.in_(ids),
        model.tenant_id == tenant_uuid(user),
    )
    if hasattr(model, "deleted_at"):
        q = q.filter(model.deleted_at.is_(None))
    found = q.all()
    if len(found) != len(set(ids)):
        raise HTTPException(404, f"{model.__name__} not found")
    return found
