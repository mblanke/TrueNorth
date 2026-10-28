"""The unit readiness page (ADR 0014 decision 7; issue #289).

=======================================  ==================================================
Endpoint                                 Who
=======================================  ==================================================
GET /readiness                           readiness:read (observers, instructors, admins):
                                         their own tenant's classes, enrolment, attendance,
                                         results and qualifications earned
GET /readiness/export.csv                the same, one row per Student per class. Audited
GET /readiness/export.pdf                the same as a PDF. Audited
=======================================  ==================================================

All three filter by date (``date_from``, ``date_to``: classes overlapping them) and by unit
(``unit``). The tenant is always the caller's own (``tenancy.tenant_uuid``): no parameter
names another, and a platform admin reads only their own here too. Students and range ops
are refused (403). Nothing is written except an export's audit row. The rules are in
``readiness``.
"""

from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..rbac import Permission, require_permission
from ..tenancy import tenant_uuid
from . import readiness, readiness_pdf
from .readiness_schemas import UnitReadinessOut

router = APIRouter(prefix="/readiness", tags=["readiness"])

_READ = require_permission(Permission.READINESS_READ)


def _report(db: Session, user: CurrentUser, date_from: date | None, date_to: date | None,
            unit: str | None) -> UnitReadinessOut:
    try:
        start, end = readiness.window(date_from, date_to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return readiness.build(db, tenant_uuid(user), start, end, unit)


_FROM = Query(None, description="Classes ending on or after this day (default: a year ago)")
_TO = Query(None, description="Classes starting on or before this day (default: 90 days ahead)")
_UNIT = Query(None, max_length=255, description="Only Students of this unit (case and spaces ignored)")


def _filename(report: UnitReadinessOut, ext: str) -> str:
    unit = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (report.unit or "all"))[:40]
    return f"readiness-{unit}-{report.date_from.isoformat()}-{report.date_to.isoformat()}.{ext}"


@router.get("", response_model=UnitReadinessOut, operation_id="getUnitReadiness",
            summary="The unit readiness page: classes, enrolment, attendance, results and qualifications")
def get_readiness(
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    unit: str | None = _UNIT,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> UnitReadinessOut:
    """The caller's own tenant only. Pass, fail or incomplete with the instructor's comment;
    never a score.  **readiness:read**"""
    return _report(db, user, date_from, date_to, unit)


@router.get(
    "/export.csv", response_class=Response, operation_id="exportUnitReadinessCsv",
    summary="The unit readiness page as CSV",
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
def export_readiness_csv(
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    unit: str | None = _UNIT,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> Response:
    """One row per Student per class; cells a spreadsheet would run as a formula are
    defused. Audited (``readiness_export``).  **readiness:read**"""
    report = _report(db, user, date_from, date_to, unit)
    out = io.StringIO()
    csv.writer(out).writerows(readiness.csv_rows(report))
    readiness.audit_export(db, user.id, tenant_uuid(user), "csv", report)
    db.commit()
    return Response(content=out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{_filename(report, "csv")}"',
                             "Cache-Control": "no-store"})


@router.get(
    "/export.pdf", response_class=Response, operation_id="exportUnitReadinessPdf",
    summary="The unit readiness page as PDF",
    responses={200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}},
)
def export_readiness_pdf(
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    unit: str | None = _UNIT,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> Response:
    """Rendered with the AAR's PDF machinery (fpdf2). Audited (``readiness_export``).
    **readiness:read**"""
    report = _report(db, user, date_from, date_to, unit)
    body = readiness_pdf.render(report)
    readiness.audit_export(db, user.id, tenant_uuid(user), "pdf", report)
    db.commit()
    return Response(content=body, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{_filename(report, "pdf")}"',
                             "Cache-Control": "no-store"})
