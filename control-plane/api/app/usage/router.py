# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""Usage and cost (showback).

=============================================  ====================================
Endpoint                                       Permission
=============================================  ====================================
GET  /usage/cost                               usage:read
GET  /usage/cost.csv                           usage:read
GET  /usage/cost/students/{user_id}            usage:read
GET  /usage/rate-cards                         usage:read
POST /usage/rate-cards                         usage:admin (+ platform admin: platform-wide)
=============================================  ====================================

Only admins hold these: Instructors, Observers and Students never see what a Student costs.
A tenant admin sees their own tenant. A platform administrator sees every tenant, or one
with ``tenant_id``. A rate card is never edited: a new price is a new card from a later
``effective_from``, so a report of a past month keeps the price it was run at.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog, User
from ..rbac import Permission, is_platform_admin, require_permission
from ..tenancy import tenant_uuid
from . import report
from .models import RateCard
from .rates import Priced
from .report import GroupBy
from .schemas import CostReportOut, CostRow, Figures, RateCardIn, RateCardOut, StudentCostOut, UsageIntervalOut

router = APIRouter(prefix="/usage", tags=["usage"])

_READ = require_permission(Permission.USAGE_READ)
_ADMIN = require_permission(Permission.USAGE_ADMIN)
MAX_CARDS = 500


def _now() -> datetime:
    return datetime.now(UTC)


def _period(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Default: this calendar month (UTC) to now. Naive times are read as UTC."""
    now = _now()
    end = end if end and end.tzinfo else end.replace(tzinfo=UTC) if end else now
    start = (
        start
        if start and start.tzinfo
        else start.replace(tzinfo=UTC)
        if start
        else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    )
    if start >= end:
        raise HTTPException(422, "'from' must be before 'to'")
    return start.astimezone(UTC), end.astimezone(UTC)


def _scope(db: Session, user: CurrentUser, tenant_id: uuid.UUID | None) -> set[uuid.UUID] | None:
    """The tenants this caller may report on. None: every tenant (platform administrator)."""
    own = tenant_uuid(user)
    if is_platform_admin(user, db):
        return None if tenant_id is None else {tenant_id}
    if tenant_id is not None and tenant_id != own:
        raise HTTPException(403, "Only a platform administrator can report on another tenant")
    return {own}


def _figures(p: Priced) -> dict:
    r = round
    return {
        "vcpu_hours": r(p.vcpu_hours, 4),
        "ram_gb_hours": r(p.ram_gb_hours, 4),
        "disk_gb_hours": r(p.disk_gb_hours, 4),
        "vm_hours": r(p.vm_hours, 4),
        "cost_vcpu": r(p.cost_vcpu, 2),
        "cost_ram": r(p.cost_ram, 2),
        "cost_disk": r(p.cost_disk, 2),
        "cost_vm": r(p.cost_vm, 2),
        "cost_total": r(p.total, 2),
        "currency": p.currency,
        "unpriced_hours": r(p.unpriced_hours, 4),
    }


@router.get("/cost", response_model=CostReportOut, summary="What each Student (course, class, tenant) cost")
def cost_report(
    period_from: datetime | None = Query(None, alias="from", description="Default: the start of this month (UTC)"),
    period_to: datetime | None = Query(None, alias="to", description="Default: now"),
    group_by: GroupBy = Query(GroupBy.student),
    tenant_id: uuid.UUID | None = Query(None, description="Platform administrator: one tenant (default: all)"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> CostReportOut:
    start, end = _period(period_from, period_to)
    rows, total = report.build(db, _scope(db, user, tenant_id), start, end, group_by, _now())
    return CostReportOut(
        period_from=start,
        period_to=end,
        group_by=group_by.value,
        totals=Figures(**_figures(total)),
        rows=[
            CostRow(
                key=r.key,
                label=r.label,
                tenant_id=r.tenant_id,
                intervals=r.intervals,
                open=r.open,
                approximate=r.approximate,
                **_figures(r.priced),
            )
            for r in rows
        ],
    )


@router.get("/cost.csv", summary="The cost report as CSV", response_class=Response)
def cost_report_csv(
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    group_by: GroupBy = Query(GroupBy.student),
    tenant_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> Response:
    start, end = _period(period_from, period_to)
    rows, _ = report.build(db, _scope(db, user, tenant_id), start, end, group_by, _now())
    name = f"usage-cost-{group_by.value}-{start:%Y%m%d}-{end:%Y%m%d}.csv"
    return Response(
        report.to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get(
    "/cost/students/{user_id}", response_model=StudentCostOut, summary="One Student's usage, interval by interval"
)
def student_cost(
    user_id: uuid.UUID = Path(...),
    period_from: datetime | None = Query(None, alias="from"),
    period_to: datetime | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> StudentCostOut:
    start, end = _period(period_from, period_to)
    scope = _scope(db, user, None)
    if scope is not None:
        # A deleted Student's history stays readable; another tenant's Student is not found.
        other = db.query(User.tenant_id).filter(User.id == user_id).scalar()
        if other is not None and other not in scope:
            raise HTTPException(404, "Student not found")
    total = Priced()
    items = []
    for iv, p in report.student_intervals(db, scope, user_id, start, end, _now()):
        total.add(p)
        items.append(
            UsageIntervalOut(
                source=iv.source,
                source_id=iv.source_id,
                range_id=iv.range_id,
                course_id=iv.course_id,
                class_id=iv.class_id,
                start=iv.start,
                end=iv.end,
                vcpu=round(iv.vcpu, 4),
                ram_gb=round(iv.ram_gb, 4),
                disk_gb=round(iv.disk_gb, 4),
                vms=round(iv.vms, 4),
                approximate=iv.approximate,
                open=iv.open,
                compute=iv.compute,
                **_figures(p),
            )
        )
    return StudentCostOut(
        user_id=user_id, period_from=start, period_to=end, intervals=items, totals=Figures(**_figures(total))
    )


@router.get(
    "/rate-cards", response_model=list[RateCardOut], summary="Rate cards: the tenant's and the platform default"
)
def list_rate_cards(db: Session = Depends(get_db), user: CurrentUser = Depends(_READ)) -> list[RateCard]:
    q = db.query(RateCard)
    if not is_platform_admin(user, db):
        q = q.filter((RateCard.tenant_id.is_(None)) | (RateCard.tenant_id == tenant_uuid(user)))
    return q.order_by(RateCard.effective_from.desc(), RateCard.created_at.desc()).limit(MAX_CARDS).all()


@router.post("/rate-cards", response_model=RateCardOut, status_code=201, summary="Add a rate card (a new price)")
def create_rate_card(body: RateCardIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)) -> RateCard:
    """A new price from ``effective_from`` on. Platform-wide needs a platform administrator.
    Audit-logged."""
    if body.platform_wide and not is_platform_admin(user, db):
        raise HTTPException(403, "Only a platform administrator can set the platform default rate card")
    tenant = None if body.platform_wide else tenant_uuid(user)
    card = RateCard(
        id=uuid.uuid4(),
        tenant_id=tenant,
        effective_from=body.effective_from,
        currency=body.currency,
        per_vcpu_hour=body.per_vcpu_hour,
        per_gb_ram_hour=body.per_gb_ram_hour,
        per_gb_disk_hour=body.per_gb_disk_hour,
        per_vm_hour=body.per_vm_hour,
        notes=body.notes,
        created_by=_user_uuid(user),
    )
    db.add(card)
    db.flush()
    db.add(
        AuditLog(
            user_id=_user_uuid(user),
            tenant_id=tenant_uuid(user),
            action="create",
            resource_type="rate_card",
            resource_id=str(card.id),
            detail=(
                f"{'platform default' if tenant is None else 'tenant'} from {body.effective_from.isoformat()} "
                f"{body.currency}: vcpu {body.per_vcpu_hour}/h, ram {body.per_gb_ram_hour}/GB-h, "
                f"disk {body.per_gb_disk_hour}/GB-h, vm {body.per_vm_hour}/h"
            )[:2000],
        )
    )
    db.commit()
    db.refresh(card)
    return card


def _user_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None
