"""TrueNorth Range -- Directory Router.

OUs (hierarchical tree), Security Groups, Nations, Coalitions.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Nation,
    Coalition,
    CoalitionMembership,
    OrganizationalUnit,
    SecurityGroup,
    SecurityGroupMembership,
)
from ..schemas import (
    NationOut,
    CoalitionOut,
    OUIn,
    OUOut,
    OUUpdate,
    OUTreeOut,
    SecurityGroupIn,
    SecurityGroupOut,
    SecurityGroupUpdate,
    SecurityGroupMembershipIn,
)

router = APIRouter(prefix="/directory", tags=["Directory"])


# -- Nations -------------------------------------------------------------
@router.get("/nations", response_model=list[NationOut])
def list_nations(nato_only: bool = False, fvey_only: bool = False, db: Session = Depends(get_db)):
    q = db.query(Nation).filter(Nation.is_active == True)
    if nato_only:
        q = q.filter(Nation.is_nato == True)
    if fvey_only:
        q = q.filter(Nation.is_fvey == True)
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
    return db.query(Coalition).filter(Coalition.is_active == True).order_by(Coalition.name).all()


@router.get("/coalitions/{coalition_id}", response_model=CoalitionOut)
def get_coalition(coalition_id: uuid.UUID, db: Session = Depends(get_db)):
    coalition = db.get(Coalition, str(coalition_id))
    if not coalition:
        raise HTTPException(404, "Coalition not found")
    return coalition


@router.get("/coalitions/{coalition_id}/members", response_model=list[NationOut])
def coalition_members(coalition_id: uuid.UUID, db: Session = Depends(get_db)):
    memberships = db.query(CoalitionMembership).filter(
        CoalitionMembership.coalition_id == str(coalition_id)
    ).all()
    nation_ids = [m.nation_id for m in memberships]
    if not nation_ids:
        return []
    return db.query(Nation).filter(Nation.id.in_(nation_ids)).all()


# -- Organizational Units ------------------------------------------------
@router.get("/ous", response_model=list[OUOut])
def list_ous(db: Session = Depends(get_db)):
    return db.query(OrganizationalUnit).order_by(OrganizationalUnit.name).all()


@router.post("/ous", response_model=OUOut, status_code=201)
def create_ou(payload: OUIn, db: Session = Depends(get_db)):
    ou = OrganizationalUnit(
        name=payload.name,
        slug=payload.slug,
        ou_type=payload.ou_type,
        parent_id=str(payload.parent_id) if payload.parent_id else None,
        nation_id=str(payload.nation_id) if payload.nation_id else None,
    )
    db.add(ou)
    db.commit()
    db.refresh(ou)
    return ou


@router.get("/ous/tree", response_model=list[OUTreeOut])
def ou_tree(db: Session = Depends(get_db)):
    all_ous = db.query(OrganizationalUnit).all()
    by_id = {}
    roots = []
    for ou in all_ous:
        node = OUTreeOut(
            id=ou.id, name=ou.name, slug=ou.slug,
            ou_type=ou.ou_type, parent_id=ou.parent_id,
            nation_id=ou.nation_id, children=[],
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
def get_ou(ou_id: uuid.UUID, db: Session = Depends(get_db)):
    ou = db.get(OrganizationalUnit, str(ou_id))
    if not ou:
        raise HTTPException(404, "OU not found")
    return ou


@router.patch("/ous/{ou_id}", response_model=OUOut)
def update_ou(ou_id: uuid.UUID, payload: OUUpdate, db: Session = Depends(get_db)):
    ou = db.get(OrganizationalUnit, str(ou_id))
    if not ou:
        raise HTTPException(404, "OU not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "parent_id" and value is not None:
            value = str(value)
        setattr(ou, field, value)
    db.commit()
    db.refresh(ou)
    return ou


@router.delete("/ous/{ou_id}", status_code=204)
def delete_ou(ou_id: uuid.UUID, db: Session = Depends(get_db)):
    ou = db.get(OrganizationalUnit, str(ou_id))
    if not ou:
        raise HTTPException(404, "OU not found")
    db.delete(ou)
    db.commit()


# -- Security Groups -----------------------------------------------------
@router.get("/groups", response_model=list[SecurityGroupOut])
def list_groups(db: Session = Depends(get_db)):
    return db.query(SecurityGroup).order_by(SecurityGroup.name).all()


@router.post("/groups", response_model=SecurityGroupOut, status_code=201)
def create_group(payload: SecurityGroupIn, db: Session = Depends(get_db)):
    sg = SecurityGroup(
        name=payload.name,
        slug=payload.slug,
        group_type=payload.group_type,
        description=payload.description,
        ou_id=str(payload.ou_id) if payload.ou_id else None,
    )
    db.add(sg)
    db.commit()
    db.refresh(sg)
    return sg


@router.get("/groups/{group_id}", response_model=SecurityGroupOut)
def get_group(group_id: uuid.UUID, db: Session = Depends(get_db)):
    sg = db.get(SecurityGroup, str(group_id))
    if not sg:
        raise HTTPException(404, "Group not found")
    return sg


@router.patch("/groups/{group_id}", response_model=SecurityGroupOut)
def update_group(group_id: uuid.UUID, payload: SecurityGroupUpdate, db: Session = Depends(get_db)):
    sg = db.get(SecurityGroup, str(group_id))
    if not sg:
        raise HTTPException(404, "Group not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(sg, field, value)
    db.commit()
    db.refresh(sg)
    return sg


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(group_id: uuid.UUID, db: Session = Depends(get_db)):
    sg = db.get(SecurityGroup, str(group_id))
    if not sg:
        raise HTTPException(404, "Group not found")
    db.delete(sg)
    db.commit()


@router.post("/groups/{group_id}/members", status_code=201)
def add_group_member(group_id: uuid.UUID, payload: SecurityGroupMembershipIn, db: Session = Depends(get_db)):
    sg = db.get(SecurityGroup, str(group_id))
    if not sg:
        raise HTTPException(404, "Group not found")
    membership = SecurityGroupMembership(user_id=str(payload.user_id), group_id=str(group_id))
    db.add(membership)
    db.commit()
    return {"message": "Member added"}


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
def remove_group_member(group_id: uuid.UUID, user_id: uuid.UUID, db: Session = Depends(get_db)):
    membership = db.query(SecurityGroupMembership).filter(
        SecurityGroupMembership.group_id == str(group_id),
        SecurityGroupMembership.user_id == str(user_id),
    ).first()
    if not membership:
        raise HTTPException(404, "Membership not found")
    db.delete(membership)
    db.commit()