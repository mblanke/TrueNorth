"""TrueNorth Range -- Directory Router.

OUs (hierarchical tree), Security Groups, Nations, Coalitions.

Nations and coalitions are global reference data (no tenant). OUs and security groups
belong to a tenant: every list is filtered to the caller's tenant, every by-id lookup
goes through ``app.tenancy.get_owned`` (a foreign id is 404, never 403), and every
create stamps the caller's tenant. Reads need ``user:read``; anything that changes the
directory needs ``user:update`` — reshaping groups changes who is in a cohort.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    Coalition,
    CoalitionMembership,
    Nation,
    OrganizationalUnit,
    SecurityGroup,
    SecurityGroupMembership,
    User,
)
from ..rbac import Permission, require_permission
from ..schemas import (
    CoalitionOut,
    NationOut,
    OUIn,
    OUOut,
    OUTreeOut,
    OUUpdate,
    SecurityGroupIn,
    SecurityGroupMembershipIn,
    SecurityGroupOut,
    SecurityGroupUpdate,
)
from ..tenancy import get_owned, tenant_uuid

# Router-level authentication. Organisational units, security groups and nations. USER_READ excludes
# students and observers, which is the intent.
#
# Every route here was previously reachable with no credentials at all.
router = APIRouter(prefix="/directory", tags=["Directory"], dependencies=[Depends(require_permission(Permission.USER_READ))])

# Mutations additionally need USER_UPDATE (admin only today). Until 2026-10-07 every
# POST/PATCH/DELETE here rode on the router's USER_READ, so an instructor could
# rewrite the whole tree.
WRITE = [Depends(require_permission(Permission.USER_UPDATE))]


# -- Nations -------------------------------------------------------------
@router.get("/nations", response_model=list[NationOut])
def list_nations(nato_only: bool = False, fvey_only: bool = False, db: Session = Depends(get_db)):
    q = db.query(Nation).filter(Nation.is_active)
    if nato_only:
        q = q.filter(Nation.is_nato)
    if fvey_only:
        q = q.filter(Nation.is_fvey)
    return q.order_by(Nation.name).all()


@router.get("/nations/{nation_id}", response_model=NationOut)
def get_nation(nation_id: uuid.UUID, db: Session = Depends(get_db)):
    nation = db.get(Nation, str(nation_id))
    if not nation:
        raise HTTPException(404, "Nation not found")
    return nation


# -- Coalitions ----------------------------------------------------------
@router.get("/coalitions", response_model=list[CoalitionOut])
def list_coalitions(db: Session = Depends(get_db)):
    return db.query(Coalition).filter(Coalition.is_active).order_by(Coalition.name).all()


@router.get("/coalitions/{coalition_id}", response_model=CoalitionOut)
def get_coalition(coalition_id: uuid.UUID, db: Session = Depends(get_db)):
    coalition = db.get(Coalition, str(coalition_id))
    if not coalition:
        raise HTTPException(404, "Coalition not found")
    return coalition


@router.get("/coalitions/{coalition_id}/members", response_model=list[NationOut])
def coalition_members(coalition_id: uuid.UUID, db: Session = Depends(get_db)):
    memberships = db.query(CoalitionMembership).filter(CoalitionMembership.coalition_id == str(coalition_id)).all()
    nation_ids = [m.nation_id for m in memberships]
    if not nation_ids:
        return []
    return db.query(Nation).filter(Nation.id.in_(nation_ids)).all()


# -- Organizational Units ------------------------------------------------
def _ous(db: Session, user: CurrentUser):
    return db.query(OrganizationalUnit).filter(OrganizationalUnit.tenant_id == tenant_uuid(user))


def _nation_or_404(db: Session, nation_id) -> None:
    if nation_id is not None and db.get(Nation, str(nation_id)) is None:
        raise HTTPException(404, "Nation not found")


@router.get("/ous", response_model=list[OUOut])
def list_ous(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return _ous(db, user).order_by(OrganizationalUnit.name).all()


@router.post("/ous", response_model=OUOut, status_code=201, dependencies=WRITE)
def create_ou(payload: OUIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    # A parent in another tenant would graft this OU into their tree.
    if payload.parent_id:
        get_owned(db, OrganizationalUnit, payload.parent_id, user, not_found="Parent OU not found")
    _nation_or_404(db, payload.nation_id)
    ou = OrganizationalUnit(
        name=payload.name,
        slug=payload.slug,
        ou_type=payload.ou_type,
        parent_id=payload.parent_id,
        nation_id=payload.nation_id,
        tenant_id=tenant_uuid(user),
    )
    db.add(ou)
    db.commit()
    db.refresh(ou)
    return ou


@router.get("/ous/tree", response_model=list[OUTreeOut])
def ou_tree(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    all_ous = _ous(db, user).all()
    by_id = {}
    roots = []
    for ou in all_ous:
        node = OUTreeOut(
            id=ou.id,
            name=ou.name,
            slug=ou.slug,
            ou_type=ou.ou_type,
            parent_id=ou.parent_id,
            nation_id=ou.nation_id,
            children=[],
        )
        by_id[str(ou.id)] = node
    for ou in all_ous:
        node = by_id[str(ou.id)]
        if ou.parent_id and str(ou.parent_id) in by_id:
            by_id[str(ou.parent_id)].children.append(node)
        else:
            roots.append(node)
    return roots


@router.get("/ous/{ou_id}", response_model=OUOut)
def get_ou(ou_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return get_owned(db, OrganizationalUnit, ou_id, user, not_found="OU not found")


@router.patch("/ous/{ou_id}", response_model=OUOut, dependencies=WRITE)
def update_ou(
    ou_id: uuid.UUID, payload: OUUpdate, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    ou = get_owned(db, OrganizationalUnit, ou_id, user, not_found="OU not found")
    changes = payload.model_dump(exclude_unset=True)
    parent_id = changes.get("parent_id")
    if parent_id is not None:
        if parent_id == ou.id:
            raise HTTPException(422, "An OU cannot be its own parent")
        get_owned(db, OrganizationalUnit, parent_id, user, not_found="Parent OU not found")
    for field, value in changes.items():
        setattr(ou, field, value)
    db.commit()
    db.refresh(ou)
    return ou


@router.delete("/ous/{ou_id}", status_code=204, response_class=Response, dependencies=WRITE)
def delete_ou(ou_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    ou = get_owned(db, OrganizationalUnit, ou_id, user, not_found="OU not found")
    db.delete(ou)
    db.commit()


# -- Security Groups -----------------------------------------------------
@router.get("/groups", response_model=list[SecurityGroupOut])
def list_groups(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return (
        db.query(SecurityGroup)
        .filter(SecurityGroup.tenant_id == tenant_uuid(user))
        .order_by(SecurityGroup.name)
        .all()
    )


@router.post("/groups", response_model=SecurityGroupOut, status_code=201, dependencies=WRITE)
def create_group(payload: SecurityGroupIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    if payload.ou_id:
        get_owned(db, OrganizationalUnit, payload.ou_id, user, not_found="OU not found")
    sg = SecurityGroup(
        name=payload.name,
        slug=payload.slug,
        group_type=payload.group_type,
        description=payload.description,
        ou_id=payload.ou_id,
        tenant_id=tenant_uuid(user),
    )
    db.add(sg)
    db.commit()
    db.refresh(sg)
    return sg


@router.get("/groups/{group_id}", response_model=SecurityGroupOut)
def get_group(group_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return get_owned(db, SecurityGroup, group_id, user, not_found="Group not found")


@router.patch("/groups/{group_id}", response_model=SecurityGroupOut, dependencies=WRITE)
def update_group(
    group_id: uuid.UUID,
    payload: SecurityGroupUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    sg = get_owned(db, SecurityGroup, group_id, user, not_found="Group not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(sg, field, value)
    db.commit()
    db.refresh(sg)
    return sg


@router.delete("/groups/{group_id}", status_code=204, response_class=Response, dependencies=WRITE)
def delete_group(group_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    sg = get_owned(db, SecurityGroup, group_id, user, not_found="Group not found")
    db.delete(sg)
    db.commit()


@router.post("/groups/{group_id}/members", status_code=201, dependencies=WRITE)
def add_group_member(
    group_id: uuid.UUID,
    payload: SecurityGroupMembershipIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    get_owned(db, SecurityGroup, group_id, user, not_found="Group not found")
    # The member must be in the caller's tenant too, or a group becomes a way to
    # enrol a foreign user into this tenant's cohort.
    get_owned(db, User, payload.user_id, user, not_found="User not found")
    existing = (
        db.query(SecurityGroupMembership)
        .filter(
            SecurityGroupMembership.group_id == group_id,
            SecurityGroupMembership.user_id == payload.user_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(409, "User already in group")
    membership = SecurityGroupMembership(user_id=payload.user_id, group_id=group_id)
    db.add(membership)
    db.commit()
    return {"message": "Member added"}


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204, response_class=Response, dependencies=WRITE)
def remove_group_member(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    get_owned(db, SecurityGroup, group_id, user, not_found="Group not found")
    # tenant-safe: the group was just checked against the caller's tenant.
    membership = (
        db.query(SecurityGroupMembership)
        .filter(
            SecurityGroupMembership.group_id == group_id,
            SecurityGroupMembership.user_id == user_id,
        )
        .first()
    )
    if not membership:
        raise HTTPException(404, "Membership not found")
    db.delete(membership)
    db.commit()
