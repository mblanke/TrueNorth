# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""Usage intervals, derived from records that already exist (usage S1). Read-only.

Who held how much, from when to when:

=============  ==================================================  ===========================
Source         Interval                                            Student
=============  ==================================================  ===========================
lab            ``provisioning_at`` → destroyed (``state_since``     ``lab_sessions.user_id``
               once terminal), else ``ended_at``, else now
class_seat     ``class_ranges.built_at`` → ``torn_down_at`` / now   roll-call entry's user
class_shared   the same, split evenly over the class's roll call    each linked entry, for the
               at each moment                                       time it was on the roll call
unattributed   ``ranges.created_at`` → deleted / destroyed / now    none (bookings, exercises,
                                                                    ranges built by hand)
=============  ==================================================  ===========================

Sizes are allocated, not measured: a lab's own ``vcpu``/``ram_mb``/``disk_gb``; a class or
other range's template (``range_topology.template_demand``).

Power history (usage S2, ADR 0021): a class or other range's interval is cut by its
``range_state_events`` into what each state paid for (``ledger.charge``): running and the
like pay compute and disk, stopped pays disk only, created/destroyed pay nothing. Time
before a range's first event (history older than the ledger) is charged as before, all
on, and marked ``approximate``; so is time after a reconcile event. A range with no events
at all keeps the whole interval, approximate. A lab is never stopped and started: its
figures are exact from its own timestamps and are not cut.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..classes.models import Class, ClassRange, ClassRangeKind, RollCallEntry, RollCallStatus
from ..course_releases.models import CourseRelease
from ..lab_sessions import models as lab
from ..lab_sessions.models import LabSession
from ..models import Range, RangeState, Template
from ..range_topology import template_demand
from . import ledger
from .models import RangeStateEvent

LAB, CLASS_SEAT, CLASS_SHARED, UNATTRIBUTED = "lab", "class_seat", "class_shared", "unattributed"


@dataclass(frozen=True)
class Interval:
    tenant_id: uuid.UUID | None
    source: str
    source_id: uuid.UUID
    range_id: uuid.UUID | None
    user_id: uuid.UUID | None
    course_id: uuid.UUID | None
    class_id: uuid.UUID | None
    start: datetime
    end: datetime
    vcpu: float
    ram_gb: float
    disk_gb: float
    vms: float
    approximate: bool
    open: bool  # still holding resources at the time of the report
    compute: bool = True  # False: the VMs were off (stopped), disk only


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _clip(iv: Interval, start: datetime, end: datetime) -> Interval | None:
    a, b = max(iv.start, start), min(iv.end, end)
    return replace(iv, start=a, end=b) if b > a else None


class _Sizes:
    """Template sizes, parsed once per template per report."""

    def __init__(self, db: Session):
        self.db = db
        self._cache: dict[uuid.UUID, tuple[float, float, float, float, bool]] = {}

    def of(self, template_id: uuid.UUID | None) -> tuple[float, float, float, float, bool]:
        """(vcpu, ram_gb, disk_gb, vms, known)."""
        if template_id is None:
            return 0.0, 0.0, 0.0, 0.0, False
        if template_id not in self._cache:
            yaml = self.db.query(Template.yaml).filter(Template.id == template_id).scalar()
            d = template_demand(yaml)
            self._cache[template_id] = (
                (float(d.vcpu), d.ram_mb / 1024.0, float(d.disk_gb), float(d.vm_count), True)
                if d
                else (0.0, 0.0, 0.0, 0.0, False)
            )
        return self._cache[template_id]


def _tenant_filter(q, column, tenant_ids: set[uuid.UUID] | None):
    return q if tenant_ids is None else q.filter(column.in_(list(tenant_ids)))


def lab_intervals(db: Session, tenant_ids, start: datetime, end: datetime, now: datetime, sizes: _Sizes):
    q = _tenant_filter(db.query(LabSession), LabSession.tenant_id, tenant_ids)
    q = q.filter(LabSession.provisioning_at.isnot(None), LabSession.provisioning_at < end)
    rows = q.all()
    release_course = dict(
        db.query(CourseRelease.id, CourseRelease.course_id)
        .filter(CourseRelease.id.in_({r.release_id for r in rows} or {uuid.uuid4()}))
        .all()
    )
    range_template = dict(
        db.query(Range.id, Range.template_id)
        .filter(Range.id.in_({r.range_id for r in rows if r.range_id} or {uuid.uuid4()}))
        .all()
    )
    out: list[Interval] = []
    for s in rows:
        a = _aware(s.provisioning_at)
        if s.state in lab.TERMINAL:
            b, is_open = _aware(s.state_since) or _aware(s.ended_at) or _aware(s.updated_at), False
        elif s.state in lab.HOLDS_RESOURCES:
            b, is_open = now, True
        else:
            b, is_open = _aware(s.ended_at) or now, s.ended_at is None
        vms = sizes.of(range_template.get(s.range_id))[3]
        out.append(
            Interval(
                tenant_id=s.tenant_id,
                source=LAB,
                source_id=s.id,
                range_id=s.range_id,
                user_id=s.user_id,
                course_id=release_course.get(s.release_id),
                class_id=None,
                start=a,
                end=max(a, b),
                vcpu=float(s.vcpu),
                ram_gb=s.ram_mb / 1024.0,
                disk_gb=float(s.disk_gb),
                vms=vms,
                approximate=False,
                open=is_open,
            )
        )
    return out


def _attendance(
    entries: list[RollCallEntry], default_start: datetime
) -> list[tuple[uuid.UUID, datetime, datetime | None]]:
    """(user, on the roll call from, until) for each linked entry that was ever on it."""
    out = []
    for e in entries:
        if e.user_id is None or e.status == RollCallStatus.pending.value:
            continue
        since = _aware(e.joined_at) or _aware(e.created_at) or default_start
        # ``forfeited_at`` is kept after a reinstatement, so it ends the time only while the
        # entry is still forfeited; a reinstatement moves ``joined_at`` on instead.
        until = _aware(e.removed_at)
        if e.status == RollCallStatus.forfeited.value:
            until = _aware(e.forfeited_at) or until
        out.append((e.user_id, since, until))
    return out


def _split(base: Interval, attendance) -> list[Interval]:
    """A shared range's interval, split evenly over who was on the roll call at each moment.
    Time with nobody on it stays unattributed; the parts always add up to the whole."""
    points = {base.start, base.end}
    for _, a, b in attendance:
        for t in (a, b):
            if t is not None and base.start < t < base.end:
                points.add(t)
    ordered = sorted(points)
    out: list[Interval] = []
    for a, b in zip(ordered, ordered[1:], strict=False):
        present = [u for u, since, until in attendance if since <= a and (until is None or until > a)]
        if not present:
            out.append(replace(base, start=a, end=b, source=UNATTRIBUTED, user_id=None))
            continue
        n = float(len(present))
        for u in present:
            out.append(
                replace(
                    base,
                    start=a,
                    end=b,
                    user_id=u,
                    vcpu=base.vcpu / n,
                    ram_gb=base.ram_gb / n,
                    disk_gb=base.disk_gb / n,
                    vms=base.vms / n,
                )
            )
    return out


def class_intervals(db: Session, tenant_ids, start: datetime, end: datetime, now: datetime, sizes: _Sizes):
    q = _tenant_filter(db.query(ClassRange), ClassRange.tenant_id, tenant_ids)
    rows = q.filter(ClassRange.built_at.isnot(None), ClassRange.built_at < end).all()
    if not rows:
        return []
    class_ids = {r.class_id for r in rows}
    course_of = dict(db.query(Class.id, Class.course_id).filter(Class.id.in_(class_ids)).all())
    entries: dict[uuid.UUID, list[RollCallEntry]] = {}
    for e in db.query(RollCallEntry).filter(RollCallEntry.class_id.in_(class_ids)).all():
        entries.setdefault(e.class_id, []).append(e)
    by_id = {e.id: e for es in entries.values() for e in es}
    events = _events(db, {r.range_id for r in rows if r.range_id})
    out: list[Interval] = []
    for r in rows:
        a = _aware(r.built_at)
        b = _aware(r.torn_down_at) or now
        vcpu, ram_gb, disk_gb, vms, _known = sizes.of(r.template_id)
        base = Interval(
            tenant_id=r.tenant_id,
            source=CLASS_SEAT,
            source_id=r.id,
            range_id=r.range_id,
            user_id=None,
            course_id=course_of.get(r.class_id),
            class_id=r.class_id,
            start=a,
            end=max(a, b),
            vcpu=vcpu,
            ram_gb=ram_gb,
            disk_gb=disk_gb,
            vms=vms,
            approximate=True,
            open=r.torn_down_at is None,
        )
        parts = _refine(base, events.get(r.range_id))
        if r.kind == ClassRangeKind.shared.value:
            attendance = _attendance(entries.get(r.class_id, []), a)
            for part in parts:
                out.extend(_split(replace(part, source=CLASS_SHARED), attendance))
        else:
            entry = by_id.get(r.entry_id) if r.entry_id else None
            user = entry.user_id if entry else None
            out.extend(replace(p, user_id=user, source=CLASS_SEAT if user else UNATTRIBUTED) for p in parts)
    return out


def _attributed_ranges(db: Session, tenant_ids) -> set[uuid.UUID]:
    """Every range a lab session or a class owns, now or before a rebuild replaced it."""
    ids: set[uuid.UUID] = set()
    q = _tenant_filter(db.query(LabSession.range_id, LabSession.retired_ranges), LabSession.tenant_id, tenant_ids)
    for range_id, retired in q.all():
        if range_id:
            ids.add(range_id)
        for rid in json.loads(retired or "[]"):
            try:
                ids.add(uuid.UUID(str(rid)))
            except ValueError:
                continue
    q = _tenant_filter(db.query(ClassRange.range_id), ClassRange.tenant_id, tenant_ids)
    ids.update(rid for (rid,) in q.all() if rid)
    return ids


def other_range_intervals(db: Session, tenant_ids, start: datetime, end: datetime, now: datetime, sizes: _Sizes):
    """Ranges no Student owns: kept as a line of their own, never dropped."""
    owned = _attributed_ranges(db, tenant_ids)
    q = _tenant_filter(db.query(Range), Range.tenant_id, tenant_ids)
    q = q.filter(Range.state != RangeState.created, Range.created_at < end)
    rows = [r for r in q.all() if r.id not in owned]
    events = _events(db, {r.id for r in rows})
    out: list[Interval] = []
    for r in rows:
        a = _aware(r.created_at)
        if r.deleted_at is not None:
            b, is_open = _aware(r.deleted_at), False
        elif r.state == RangeState.destroyed:
            b, is_open = _aware(r.updated_at), False
        else:
            b, is_open = now, True
        vcpu, ram_gb, disk_gb, vms, _known = sizes.of(r.template_id)
        base = Interval(
            tenant_id=r.tenant_id,
            source=UNATTRIBUTED,
            source_id=r.id,
            range_id=r.id,
            user_id=None,
            course_id=None,
            class_id=None,
            start=a,
            end=max(a, b),
            vcpu=vcpu,
            ram_gb=ram_gb,
            disk_gb=disk_gb,
            vms=vms,
            approximate=True,
            open=is_open,
        )
        out.extend(_refine(base, events.get(r.id)))
    return out


@dataclass(frozen=True)
class _Event:
    at: datetime
    from_state: str | None
    to_state: str
    source: str


def _events(db: Session, range_ids: set[uuid.UUID]) -> dict[uuid.UUID, list[_Event]]:
    out: dict[uuid.UUID, list[_Event]] = {}
    ids = list(range_ids)
    for i in range(0, len(ids), 500):
        rows = (
            db.query(RangeStateEvent)
            .filter(RangeStateEvent.range_id.in_(ids[i : i + 500]))
            .order_by(RangeStateEvent.range_id, RangeStateEvent.at)
            .all()
        )
        for e in rows:
            out.setdefault(e.range_id, []).append(_Event(_aware(e.at), e.from_state, e.to_state, e.source))
    return out


def _refine(base: Interval, evs: list[_Event] | None) -> list[Interval]:
    """``base`` cut by its range's state events into what each part paid for. Parts that
    paid nothing are dropped; the rest keep ``base``'s Student, sizes and labels.

    Approximate: time before the first event (history older than the ledger), and the time
    that ends at a reconcile event: the change it caught happened somewhere in it."""
    if not evs:
        return [base]
    first = evs[0]
    pre = ledger.charge(first.from_state) if first.from_state else ledger.COMPUTE
    segments = [(base.start, first.at, pre, True)]
    state = first.from_state
    settled = pre if first.from_state not in ledger.IN_PROGRESS else ledger.NOTHING
    for i, e in enumerate(evs):
        nxt = evs[i + 1] if i + 1 < len(evs) else None
        paid = ledger.charge(e.to_state, e.from_state or state, settled)
        if e.to_state not in ledger.IN_PROGRESS and e.to_state != "failed":
            settled = paid
        approx = nxt is not None and nxt.source == ledger.SOURCE_RECONCILE
        segments.append((e.at, nxt.at if nxt else base.end, paid, approx))
        state = e.to_state
    out: list[Interval] = []
    for a, b, what, approx in segments:
        a, b = max(a, base.start), min(b, base.end)
        if b <= a or what == ledger.NOTHING:
            continue
        out.append(
            replace(
                base,
                start=a,
                end=b,
                compute=what == ledger.COMPUTE,
                approximate=approx,
                open=base.open and b == base.end,
            )
        )
    return out


def derive(
    db: Session, tenant_ids: set[uuid.UUID] | None, start: datetime, end: datetime, now: datetime | None = None
) -> list[Interval]:
    """Every usage interval of ``tenant_ids`` (None: every tenant), clipped to ``[start, end)``."""
    now = _aware(now) or datetime.now(UTC)
    start, end = _aware(start), min(_aware(end), now)
    sizes = _Sizes(db)
    found = (
        lab_intervals(db, tenant_ids, start, end, now, sizes)
        + class_intervals(db, tenant_ids, start, end, now, sizes)
        + other_range_intervals(db, tenant_ids, start, end, now, sizes)
    )
    return [c for iv in found if (c := _clip(iv, start, end)) is not None]
