# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""Usage and cost S1: per-Student cost from records that already exist, priced by rate card.

Allocated size x time held x the rate card in force. A lab is exact (provisioned to
destroyed); a class range is charged for its whole life (no power history yet) and a shared
one is split over the roll call; a range no Student owns is its own line, never dropped.
Admins only; a tenant admin sees their own tenant.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from _shared import real_tenant, real_user
from app.auth import get_current_user
from app.classes.models import Class, ClassRange, RollCallEntry
from app.course_releases.models import CourseRelease, CourseReleaseBlob
from app.lab_sessions.models import LabSession
from app.main import app as fastapi_app
from app.models import AuditLog, Course, Range, RangeState, Template, User, UserRole
from app.usage import RateCard
from app.usage.derive import CLASS_SEAT, CLASS_SHARED, LAB, UNATTRIBUTED, derive
from app.usage.rates import Card, RateBook

T0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
H = timedelta(hours=1)
TPL_YAML = (  # 2 VMs: 2+4 vCPU, 4+8 GB RAM, 60+100 GB disk
    "nodes:\n"
    "  - {id: ws, os: kali, specs: {cores: 2, memory_mb: 4096, disk_gb: 60}}\n"
    "  - {id: dc, os: windows, specs: {cores: 4, memory_mb: 8192, disk_gb: 100}}\n"
)


# ── pricing, no database ─────────────────────────────────────────────────────
def _card(at, vcpu=1.0, ram=0.0, disk=0.0, vm=0.0, cur="CAD"):
    return Card(effective_from=at, currency=cur, vcpu=vcpu, ram_gb=ram, disk_gb=disk, vm=vm)


def test_an_interval_crossing_a_price_change_is_split_there():
    book = RateBook.of({None: [_card(T0, vcpu=1.0), _card(T0 + 2 * H, vcpu=3.0)]})
    p = book.price(None, T0, T0 + 4 * H, vcpu=2, ram_gb=0, disk_gb=0, vms=0)
    assert p.vcpu_hours == pytest.approx(8)
    assert p.cost_vcpu == pytest.approx(2 * 2 * 1.0 + 2 * 2 * 3.0)
    assert p.currency == "CAD" and p.unpriced_hours == 0


def test_a_tenant_card_overrides_the_platform_default_from_its_start():
    t = uuid.uuid4()
    book = RateBook.of({None: [_card(T0, vcpu=1.0)], t: [_card(T0 + H, vcpu=5.0)]})
    p = book.price(t, T0, T0 + 3 * H, vcpu=1, ram_gb=0, disk_gb=0, vms=0)
    assert p.cost_vcpu == pytest.approx(1 * 1.0 + 2 * 5.0)
    assert book.price(uuid.uuid4(), T0, T0 + H, vcpu=1, ram_gb=0, disk_gb=0, vms=0).cost_vcpu == pytest.approx(1.0)


def test_time_before_any_card_is_counted_but_not_costed():
    book = RateBook.of({None: [_card(T0 + H, vcpu=1.0)]})
    p = book.price(None, T0, T0 + 2 * H, vcpu=1, ram_gb=0, disk_gb=0, vms=0)
    assert p.vcpu_hours == pytest.approx(2) and p.unpriced_hours == pytest.approx(1)
    assert p.cost_vcpu == pytest.approx(1.0)


def test_every_component_is_priced_and_disk_runs_while_compute_is_off():
    book = RateBook.of({None: [_card(T0, vcpu=0.01, ram=0.002, disk=0.0001, vm=0.05)]})
    p = book.price(None, T0, T0 + 10 * H, vcpu=6, ram_gb=12, disk_gb=160, vms=2)
    assert p.total == pytest.approx(10 * (6 * 0.01 + 12 * 0.002 + 160 * 0.0001 + 2 * 0.05))
    off = book.price(None, T0, T0 + 10 * H, vcpu=6, ram_gb=12, disk_gb=160, vms=2, compute=False)
    assert off.vcpu_hours == 0 and off.disk_gb_hours == pytest.approx(1600)


# ── fixtures ─────────────────────────────────────────────────────────────────
@contextmanager
def acting(who):
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _student(db, tenant, name="ann") -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{name}-{uuid.uuid4().hex[:6]}@corp.test",
        display_name=name.title(),
        role=UserRole.student,
        tenant_id=tenant,
    )
    db.add(u)
    db.flush()
    return u


def _template(db, tenant, yaml=TPL_YAML) -> Template:
    t = Template(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml=yaml, tenant_id=tenant)
    db.add(t)
    db.flush()
    return t


def _range(db, tenant, tpl, state=RangeState.running, created=T0) -> Range:
    r = Range(
        id=uuid.uuid4(),
        name=f"r-{uuid.uuid4().hex[:6]}",
        template_id=tpl.id,
        tenant_id=tenant,
        state=state,
        provisioner_backend="mock",
        created_at=created,
    )
    db.add(r)
    db.flush()
    return r


def _release(db, tenant, course) -> CourseRelease:
    sha = uuid.uuid4().hex * 2
    db.add(CourseReleaseBlob(sha256=sha, size=1, data=b"x"))
    db.flush()
    rel = CourseRelease(
        id=uuid.uuid4(),
        tenant_id=tenant,
        course_id=course.id,
        catalogue_code="c",
        arc2_code="a",
        run_id="r",
        slug=f"s-{uuid.uuid4().hex[:6]}",
        title="T",
        version=1,
        release_digest=sha,
        learner_digest=sha,
        platform_digest=sha,
        instructor_digest=sha,
        blob_sha256=sha,
        meta="{}",
    )
    db.add(rel)
    db.flush()
    return rel


@pytest.fixture
def world(db_session):
    db = db_session
    a, b = real_tenant(db, "cost-a"), real_tenant(db, "cost-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id)
    db.add(course)
    db.flush()
    rel = _release(db, a.id, course)
    tpl = _template(db, a.id)
    ann, bob, cat = _student(db, a.id, "ann"), _student(db, a.id, "bob"), _student(db, a.id, "cat")
    return {
        "db": db,
        "a": a.id,
        "b": b.id,
        "course": course,
        "rel": rel,
        "tpl": tpl,
        "ann": ann,
        "bob": bob,
        "cat": cat,
    }


def _lab(w, user, start, end, state="destroyed", **kw) -> LabSession:
    db = w["db"]
    rng = _range(db, w["a"], w["tpl"], RangeState.destroyed if state == "destroyed" else RangeState.running)
    s = LabSession(
        id=uuid.uuid4(),
        tenant_id=w["a"],
        user_id=user.id,
        release_id=w["rel"].id,
        activity_id=f"mod_{uuid.uuid4().hex[:3]}",
        state=state,
        profile_id="p",
        profile_digest="0" * 64,
        backend="mock",
        range_id=rng.id,
        vcpu=4,
        ram_mb=8192,
        disk_gb=80,
        provisioning_at=start,
        ended_at=end,
        state_since=end or start,
        **kw,
    )
    db.add(s)
    db.flush()
    return s


def _class(w, start=T0) -> Class:
    db = w["db"]
    lead = real_user(db, UserRole.instructor, w["a"])
    cls = Class(
        id=uuid.uuid4(),
        tenant_id=w["a"],
        name="C201-26-04",
        course_id=w["course"].id,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 30),
        lead_instructor_id=uuid.UUID(lead.id),
        status="open",
    )
    db.add(cls)
    db.flush()
    return cls


def _entry(w, cls, user, joined, removed=None, status="signed_in") -> RollCallEntry:
    e = RollCallEntry(
        id=uuid.uuid4(),
        class_id=cls.id,
        tenant_id=w["a"],
        email=user.email if user else f"x-{uuid.uuid4().hex[:6]}@corp.test",
        user_id=user.id if user else None,
        status=status,
        joined_at=joined,
        removed_at=removed,
    )
    w["db"].add(e)
    w["db"].flush()
    return e


def _class_range(w, cls, kind, built, torn=None, entry=None) -> ClassRange:
    rng = _range(w["db"], w["a"], w["tpl"])
    cr = ClassRange(
        id=uuid.uuid4(),
        tenant_id=w["a"],
        class_id=cls.id,
        template_id=w["tpl"].id,
        kind=kind,
        entry_id=entry.id if entry else None,
        seat_key=str(entry.id) if entry else "shared",
        range_id=rng.id,
        builds=1,
        built_at=built,
        torn_down_at=torn,
    )
    w["db"].add(cr)
    w["db"].flush()
    return cr


def _hours(ivs, **match):
    return sum(
        (iv.end - iv.start).total_seconds() / 3600 for iv in ivs if all(getattr(iv, k) == v for k, v in match.items())
    )


# ── derivation ───────────────────────────────────────────────────────────────
def test_a_destroyed_lab_runs_from_provisioning_to_destroyed_and_is_exact(world):
    w = world
    _lab(w, w["ann"], T0, T0 + 3 * H)
    ivs = derive(w["db"], {w["a"]}, T0 - H, T0 + 10 * H, now=T0 + 20 * H)
    labs = [iv for iv in ivs if iv.source == LAB]
    assert len(labs) == 1 and labs[0].user_id == w["ann"].id and labs[0].course_id == w["course"].id
    assert labs[0].end - labs[0].start == 3 * H and labs[0].vcpu == 4 and labs[0].ram_gb == 8
    assert labs[0].vms == 2 and not labs[0].approximate and not labs[0].open
    # its range is the lab's: never counted a second time as unattributed
    assert not [iv for iv in ivs if iv.source == UNATTRIBUTED]


def test_a_live_lab_runs_to_now_and_is_clipped_to_the_period(world):
    w = world
    _lab(w, w["ann"], T0, None, state="active")
    ivs = derive(w["db"], {w["a"]}, T0 + H, T0 + 100 * H, now=T0 + 5 * H)
    (lab,) = [iv for iv in ivs if iv.source == LAB]
    assert lab.start == T0 + H and lab.end == T0 + 5 * H and lab.open


def test_a_lab_that_never_got_machines_costs_nothing(world):
    w = world
    s = _lab(w, w["ann"], None, None, state="failed")
    assert s.provisioning_at is None
    assert not [iv for iv in derive(w["db"], {w["a"]}, T0, T0 + H, now=T0 + H) if iv.source == LAB]


def test_a_seat_range_goes_to_its_student_and_an_unlinked_seat_is_unattributed(world):
    w = world
    cls = _class(w)
    e_ann = _entry(w, cls, w["ann"], T0)
    e_none = _entry(w, cls, None, T0, status="invited")
    _class_range(w, cls, "seat", T0, T0 + 4 * H, e_ann)
    _class_range(w, cls, "seat", T0, T0 + 4 * H, e_none)
    ivs = derive(w["db"], {w["a"]}, T0, T0 + 10 * H, now=T0 + 10 * H)
    assert _hours(ivs, source=CLASS_SEAT, user_id=w["ann"].id) == pytest.approx(4)
    seat = next(iv for iv in ivs if iv.source == CLASS_SEAT)
    assert seat.vcpu == 6 and seat.ram_gb == 12 and seat.disk_gb == 160 and seat.approximate
    assert seat.class_id == cls.id and seat.course_id == w["course"].id
    assert _hours(ivs, source=UNATTRIBUTED, class_id=cls.id) == pytest.approx(4)


def test_a_shared_range_is_split_over_the_roll_call_and_the_parts_add_up(world):
    w = world
    cls = _class(w)
    _entry(w, cls, w["ann"], T0)
    _entry(w, cls, w["bob"], T0 + 2 * H)  # joins after 2 h
    _entry(w, cls, w["cat"], T0, removed=T0 + 6 * H, status="removed")  # leaves at 6 h
    _entry(w, cls, _student(w["db"], w["a"], "dan"), T0, status="pending")  # pending: never on the roll call
    _class_range(w, cls, "shared", T0, T0 + 8 * H)
    ivs = [iv for iv in derive(w["db"], {w["a"]}, T0, T0 + 10 * H, now=T0 + 10 * H) if iv.class_id == cls.id]
    vcpu_h = {
        u: sum(iv.vcpu * (iv.end - iv.start).total_seconds() / 3600 for iv in ivs if iv.user_id == u)
        for u in (w["ann"].id, w["bob"].id, w["cat"].id)
    }
    # 0-2 h: ann, cat (3 vCPU each); 2-6 h: three (2 each); 6-8 h: ann, bob (3 each)
    assert vcpu_h[w["ann"].id] == pytest.approx(2 * 3 + 4 * 2 + 2 * 3)
    assert vcpu_h[w["bob"].id] == pytest.approx(4 * 2 + 2 * 3)
    assert vcpu_h[w["cat"].id] == pytest.approx(2 * 3 + 4 * 2)
    assert sum(vcpu_h.values()) == pytest.approx(6 * 8)  # the whole range, nothing lost
    assert all(iv.source == CLASS_SHARED for iv in ivs)


def test_shared_time_with_nobody_on_the_roll_call_stays_unattributed(world):
    w = world
    cls = _class(w)
    _entry(w, cls, w["ann"], T0 + 3 * H)
    _class_range(w, cls, "shared", T0, T0 + 4 * H)
    ivs = [iv for iv in derive(w["db"], {w["a"]}, T0, T0 + 10 * H, now=T0 + 10 * H) if iv.class_id == cls.id]
    assert _hours(ivs, source=UNATTRIBUTED) == pytest.approx(3)
    assert _hours(ivs, user_id=w["ann"].id) == pytest.approx(1)


def test_a_range_no_student_owns_is_its_own_line(world):
    w = world
    _range(w["db"], w["a"], w["tpl"], RangeState.running, created=T0)
    _range(w["db"], w["a"], w["tpl"], RangeState.created, created=T0)  # never built: nothing
    ivs = derive(w["db"], {w["a"]}, T0, T0 + 5 * H, now=T0 + 5 * H)
    (iv,) = [i for i in ivs if i.source == UNATTRIBUTED]
    assert iv.user_id is None and iv.approximate and iv.open and iv.end - iv.start == 5 * H


def test_another_tenants_usage_is_not_derived(world):
    w = world
    _range(w["db"], w["b"], _template(w["db"], w["b"]), RangeState.running, created=T0)
    assert derive(w["db"], {w["a"]}, T0, T0 + H, now=T0 + H) == []


# ── API ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def priced(world, monkeypatch):
    w = world
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(w["a"]))
    w["db"].add(
        RateCard(
            id=uuid.uuid4(),
            tenant_id=None,
            effective_from=T0 - 24 * H,
            currency="CAD",
            per_vcpu_hour=Decimal("0.01"),
            per_gb_ram_hour=Decimal("0.005"),
            per_gb_disk_hour=Decimal("0.0001"),
            per_vm_hour=Decimal("0"),
        )
    )
    _lab(w, w["ann"], T0, T0 + 10 * H)
    w["db"].commit()
    return w


def _q(**extra):
    return {"from": (T0 - H).isoformat(), "to": (T0 + 24 * H).isoformat(), **extra}


def test_an_admin_sees_cost_per_student(client, priced):
    w = priced
    with acting(real_user(w["db"], UserRole.admin, w["a"])):
        resp = client.get("/usage/cost", params=_q())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (row,) = [r for r in body["rows"] if r["key"] == str(w["ann"].id)]
    assert row["label"] == "Ann" and row["vcpu_hours"] == pytest.approx(40)
    # 10 h x (4 vCPU x 0.01 + 8 GB x 0.005 + 80 GB x 0.0001)
    assert row["cost_total"] == pytest.approx(round(10 * (0.04 + 0.04 + 0.008), 2))
    assert row["currency"] == "CAD" and not row["approximate"]
    assert body["totals"]["cost_total"] == pytest.approx(row["cost_total"])


def test_the_report_rolls_up_per_course_and_exports_csv(client, priced):
    w = priced
    with acting(real_user(w["db"], UserRole.admin, w["a"])):
        course = client.get("/usage/cost", params=_q(group_by="course")).json()
        csv = client.get("/usage/cost.csv", params=_q())
    assert course["rows"][0]["label"] == "SOC Analyst"
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0].startswith("key,label,tenant_id,vcpu_hours")
    assert "Ann" in csv.text


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.observer, UserRole.student, UserRole.range_ops])
def test_only_admins_see_what_a_student_costs(client, priced, role):
    w = priced
    with acting(real_user(w["db"], role, w["a"])):
        assert client.get("/usage/cost", params=_q()).status_code == 403
        assert client.get(f"/usage/cost/students/{w['ann'].id}", params=_q()).status_code == 403
        assert client.get("/usage/rate-cards").status_code == 403


def test_a_tenant_admin_sees_only_their_own_tenant(client, priced):
    w = priced
    with acting(real_user(w["db"], UserRole.admin, w["b"])):
        body = client.get("/usage/cost", params=_q()).json()
        assert all(r["tenant_id"] in (None, str(w["b"])) for r in body["rows"])
        assert not [r for r in body["rows"] if r["key"] == str(w["ann"].id)]
        assert client.get("/usage/cost", params=_q(tenant_id=str(w["a"]))).status_code == 403
        assert client.get(f"/usage/cost/students/{w['ann'].id}", params=_q()).status_code == 404


def test_the_platform_admin_sees_every_tenant_and_one_on_request(client, priced):
    w = priced
    _range(w["db"], w["b"], _template(w["db"], w["b"]), RangeState.running, created=T0)
    w["db"].commit()
    with acting(real_user(w["db"], UserRole.admin, w["a"])):
        tenants = {r["tenant_id"] for r in client.get("/usage/cost", params=_q()).json()["rows"]}
        only_b = client.get("/usage/cost", params=_q(tenant_id=str(w["b"]))).json()["rows"]
    assert {str(w["a"]), str(w["b"])} <= tenants
    assert only_b and all(r["tenant_id"] == str(w["b"]) for r in only_b)


def test_a_students_drill_down_lists_each_interval(client, priced):
    w = priced
    with acting(real_user(w["db"], UserRole.admin, w["a"])):
        body = client.get(f"/usage/cost/students/{w['ann'].id}", params=_q()).json()
    (iv,) = body["intervals"]
    assert iv["source"] == "lab" and iv["vcpu_hours"] == pytest.approx(40)
    assert body["totals"]["cost_total"] == iv["cost_total"]


def test_a_rate_card_is_added_audited_and_platform_wide_needs_the_platform_admin(client, priced):
    w = priced
    card = {"effective_from": "2026-12-01T00:00:00-05:00", "per_vcpu_hour": "0.02", "currency": "CAD"}
    with acting(real_user(w["db"], UserRole.admin, w["b"])):
        assert client.post("/usage/rate-cards", json={**card, "platform_wide": True}).status_code == 403
        resp = client.post("/usage/rate-cards", json=card)
        assert resp.status_code == 201, resp.text
        assert resp.json()["tenant_id"] == str(w["b"])
        assert resp.json()["effective_from"].startswith("2026-12-01T05:00:00")
        listed = client.get("/usage/rate-cards").json()
    assert {c["tenant_id"] for c in listed} == {None, str(w["b"])}
    assert w["db"].query(AuditLog).filter(AuditLog.resource_type == "rate_card").count() == 1
    with acting(real_user(w["db"], UserRole.admin, w["a"])):
        assert client.post("/usage/rate-cards", json={**card, "platform_wide": True}).status_code == 201


def test_a_rate_card_refuses_a_time_with_no_zone_and_a_negative_rate(client, priced):
    w = priced
    with acting(real_user(w["db"], UserRole.admin, w["a"])):
        assert client.post("/usage/rate-cards", json={"effective_from": "2026-12-01T00:00:00"}).status_code == 422
        bad = {"effective_from": "2026-12-01T00:00:00Z", "per_vcpu_hour": "-1"}
        assert client.post("/usage/rate-cards", json=bad).status_code == 422


def test_from_must_be_before_to(client, priced):
    with acting(real_user(priced["db"], UserRole.admin, priced["a"])):
        resp = client.get("/usage/cost", params={"from": T0.isoformat(), "to": (T0 - H).isoformat()})
    assert resp.status_code == 422


# ── tenancy and access: each filter has a test that fails without it ─────────
def _other_tenant(w) -> dict:
    """The same builders, pointed at tenant B (they read the tenant from ``w["a"]``)."""
    db = w["db"]
    course = Course(id=uuid.uuid4(), name="Other Course", tenant_id=w["b"])
    db.add(course)
    db.flush()
    return {**w, "a": w["b"], "course": course, "rel": _release(db, w["b"], course), "tpl": _template(db, w["b"])}


def _usage_in_b(w) -> User:
    """A lab, a class seat and a shared class range, all in tenant B. Returns B's Student."""
    wb = _other_tenant(w)
    bea = _student(w["db"], w["b"], "bea")
    _lab(wb, bea, T0, T0 + 2 * H)
    cls = _class(wb)
    entry = _entry(wb, cls, bea, T0)
    _class_range(wb, cls, "seat", T0, T0 + 3 * H, entry=entry)
    _class_range(wb, cls, "shared", T0, T0 + 3 * H)
    return bea


def test_another_tenants_labs_and_class_ranges_are_not_derived(world):
    w = world
    bea = _usage_in_b(w)
    assert derive(w["db"], {w["a"]}, T0, T0 + 5 * H, now=T0 + 5 * H) == []
    theirs = derive(w["db"], {w["b"]}, T0, T0 + 5 * H, now=T0 + 5 * H)
    assert {LAB, CLASS_SEAT, CLASS_SHARED} <= {iv.source for iv in theirs}
    assert {iv.user_id for iv in theirs if iv.source in (LAB, CLASS_SEAT)} == {bea.id}


def test_a_tenant_admin_never_reads_another_tenants_class_ranges(client, priced):
    w = priced
    _usage_in_b(w)
    w["db"].commit()
    with acting(real_user(w["db"], UserRole.admin, w["b"])):
        mine = client.get("/usage/cost", params=_q()).json()["rows"]
    assert mine and all(r["tenant_id"] in (None, str(w["b"])) for r in mine)
    # tenant A's admin is the platform admin here; a third tenant's admin is not
    c = real_tenant(w["db"], "cost-c")
    with acting(real_user(w["db"], UserRole.admin, c.id)):
        assert client.get("/usage/cost", params=_q()).json()["rows"] == []
        assert client.get("/usage/cost", params=_q(group_by="class")).json()["rows"] == []


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.observer, UserRole.student, UserRole.range_ops])
def test_only_admins_export_the_csv_or_add_a_rate_card(client, priced, role):
    w = priced
    with acting(real_user(w["db"], role, w["a"])):
        assert client.get("/usage/cost.csv", params=_q()).status_code == 403
        card = {"effective_from": "2026-12-01T00:00:00Z", "per_vcpu_hour": "0.02"}
        assert client.post("/usage/rate-cards", json=card).status_code == 403


def test_the_csv_holds_only_the_admins_own_tenant(client, priced):
    w = priced
    _usage_in_b(w)
    w["db"].commit()
    with acting(real_user(w["db"], UserRole.admin, w["b"])):
        csv = client.get("/usage/cost.csv", params=_q())
        assert client.get("/usage/cost.csv", params=_q(tenant_id=str(w["a"]))).status_code == 403
    assert csv.status_code == 200
    assert "Bea" in csv.text and "Ann" not in csv.text and str(w["a"]) not in csv.text


def test_a_tenant_admin_does_not_list_another_tenants_rate_cards(client, priced):
    w = priced
    for tenant in (w["a"], w["b"]):
        w["db"].add(RateCard(id=uuid.uuid4(), tenant_id=tenant, effective_from=T0, currency="CAD"))
    w["db"].commit()
    with acting(real_user(w["db"], UserRole.admin, w["b"])):
        listed = client.get("/usage/rate-cards").json()
    assert {c["tenant_id"] for c in listed} == {None, str(w["b"])}


def test_a_name_that_starts_like_a_formula_is_quoted_in_the_csv(client, priced):
    w = priced
    w["ann"].display_name = "=HYPERLINK(1)"
    w["db"].commit()
    with acting(real_user(w["db"], UserRole.admin, w["a"])):
        text = client.get("/usage/cost.csv", params=_q()).text
    assert ",'=HYPERLINK(1)," in text and ",=HYPERLINK(1)," not in text


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_every_formula_lead_character_is_quoted(lead):
    from app.usage.report import _safe

    assert _safe(lead + "x") == "'" + lead + "x" and _safe("x" + lead) == "x" + lead
