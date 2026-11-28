# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""The cost report: intervals priced and summed per Student, course, class or tenant."""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy.orm import Session

from ..classes.models import Class
from ..models import Course, Tenant, User
from .derive import LAB, UNATTRIBUTED, Interval, derive
from .rates import Priced, RateBook


class GroupBy(str, Enum):
    student = "student"
    course = "course"
    class_ = "class"
    tenant = "tenant"


@dataclass
class Row:
    key: str
    label: str
    tenant_id: uuid.UUID | None
    priced: Priced = field(default_factory=Priced)
    intervals: int = 0
    open: int = 0
    approximate: bool = False


def _key(iv: Interval, by: GroupBy) -> tuple[str, str | None]:
    """(row key, id to name it by); unattributed and self-serve get keys of their own."""
    if by is GroupBy.tenant:
        return str(iv.tenant_id), str(iv.tenant_id)
    if by is GroupBy.student:
        if iv.user_id is None:
            return f"unattributed:{iv.tenant_id}", None
        return str(iv.user_id), str(iv.user_id)
    if by is GroupBy.course:
        if iv.course_id is None:
            return f"no-course:{iv.tenant_id}", None
        return str(iv.course_id), str(iv.course_id)
    if iv.class_id is None:
        kind = "self-serve" if iv.source == LAB else "no-class"
        return f"{kind}:{iv.tenant_id}", None
    return str(iv.class_id), str(iv.class_id)


def _names(db: Session, by: GroupBy, ids: set[str]) -> dict[str, str]:
    uids = [uuid.UUID(i) for i in ids]
    if not uids:
        return {}
    model, col = {
        GroupBy.student: (User, User.display_name),
        GroupBy.course: (Course, Course.name),
        GroupBy.class_: (Class, Class.name),
        GroupBy.tenant: (Tenant, Tenant.name),
    }[by]
    return {str(i): n for i, n in db.query(model.id, col).filter(model.id.in_(uids)).all()}


def _fallback_label(key: str, by: GroupBy) -> str:
    if key.startswith("unattributed:"):
        return "Unattributed (no Student)"
    if key.startswith("no-course:"):
        return "No course"
    if key.startswith("self-serve:"):
        return "Self-serve labs"
    if key.startswith("no-class:"):
        return "No class"
    return "Deleted Student" if by is GroupBy.student else "(deleted)"


def price(book: RateBook, iv: Interval) -> Priced:
    return book.price(iv.tenant_id, iv.start, iv.end, vcpu=iv.vcpu, ram_gb=iv.ram_gb, disk_gb=iv.disk_gb, vms=iv.vms)


def build(
    db: Session,
    tenant_ids: set[uuid.UUID] | None,
    start: datetime,
    end: datetime,
    by: GroupBy,
    now: datetime | None = None,
) -> tuple[list[Row], Priced]:
    intervals = derive(db, tenant_ids, start, end, now)
    book = RateBook(db, tenant_ids)
    rows: dict[str, Row] = {}
    name_ids: set[str] = set()
    total = Priced()
    for iv in intervals:
        key, name_id = _key(iv, by)
        row = rows.setdefault(key, Row(key=key, label="", tenant_id=iv.tenant_id))
        p = price(book, iv)
        row.priced.add(p)
        total.add(p)
        row.intervals += 1
        row.open += int(iv.open)
        row.approximate = row.approximate or iv.approximate
        if name_id:
            name_ids.add(name_id)
    names = _names(db, by, name_ids)
    for row in rows.values():
        row.label = names.get(row.key) or _fallback_label(row.key, by)
    ordered = sorted(rows.values(), key=lambda r: (-r.priced.total, -r.priced.vcpu_hours, r.label))
    return ordered, total


def student_intervals(
    db: Session,
    tenant_ids: set[uuid.UUID] | None,
    user_id: uuid.UUID,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> list[tuple[Interval, Priced]]:
    book = RateBook(db, tenant_ids)
    found = [iv for iv in derive(db, tenant_ids, start, end, now) if iv.user_id == user_id]
    return [(iv, price(book, iv)) for iv in sorted(found, key=lambda i: i.start)]


CSV_COLUMNS = (
    "key",
    "label",
    "tenant_id",
    "vcpu_hours",
    "ram_gb_hours",
    "disk_gb_hours",
    "vm_hours",
    "cost_vcpu",
    "cost_ram",
    "cost_disk",
    "cost_vm",
    "cost_total",
    "currency",
    "unpriced_hours",
    "intervals",
    "open",
    "approximate",
)


def to_csv(rows: list[Row]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        p = r.priced
        w.writerow(
            [
                r.key,
                _safe(r.label),
                r.tenant_id or "",
                f"{p.vcpu_hours:.2f}",
                f"{p.ram_gb_hours:.2f}",
                f"{p.disk_gb_hours:.2f}",
                f"{p.vm_hours:.2f}",
                f"{p.cost_vcpu:.2f}",
                f"{p.cost_ram:.2f}",
                f"{p.cost_disk:.2f}",
                f"{p.cost_vm:.2f}",
                f"{p.total:.2f}",
                p.currency,
                f"{p.unpriced_hours:.2f}",
                r.intervals,
                r.open,
                "yes" if r.approximate else "no",
            ]
        )
    return buf.getvalue()


def _safe(text: str) -> str:
    """No spreadsheet formula injection from a name a person typed."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


__all__ = ["GroupBy", "Row", "UNATTRIBUTED", "build", "student_intervals", "to_csv", "CSV_COLUMNS"]
