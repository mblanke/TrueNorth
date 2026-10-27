"""Identity 13: nightly AD status sync and the 90-day rule for local accounts (ADR 0014 §6; #290).

The rules under test (app/ad_status_sync):

- TrueNorth only reads the directory, through Keycloak's Admin API: GET requests and the
  client-credentials token, never a write (asserted on the HTTP transport).
- An AD account that is disabled, gone, or in no TN-* group any more: the TrueNorth account
  is deactivated (``is_active`` false). One still enabled and in a TN-* group is untouched.
- Deactivating frees their roll-call seats (``remove_from_roll_call``: seat freed, CANCEL),
  emails the class's instructors, writes an audit row, and deletes nothing.
- A local Keycloak account unused for 90 days is deactivated; break-glass never is.
- A dry run records and changes nothing. A run over the limit is held.
- Any number of replicas: one nightly run per night; each account deactivated once.
- Administrators only, their own tenant only.

Time travel: ``service.run_nightly(db, reader, now=...)``, as the class clocks are tested.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from _shared import act_as, real_tenant, real_user
from app import auth
from app.ad_status_sync import router as sync_router
from app.ad_status_sync import service
from app.ad_status_sync.directory import (
    DirectoryReader,
    DirectorySnapshot,
    DirectoryUnavailableError,
    DirectoryUser,
    KeycloakDirectoryReader,
)
from app.ad_status_sync.models import AdStatusSyncResult, AdStatusSyncRun, UserLastSeen
from app.auth import TokenPayload
from app.classes import sessions as class_sessions
from app.classes.models import RollCallEntry, RollCallSettings
from app.main import app as fastapi_app
from app.models import AuditLog, Course, Enrollment, User, UserRole
from app.scheduler.capacity import Resources
from app.scheduler.models import ScheduledEvent

DAY = (datetime.now(UTC) + timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)
NINE = DAY + timedelta(hours=9)
NIGHT = datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0)
BREAK_GLASS = "breakglass@tn.local"


# ── Fakes ──────────────────────────────────────────────────────────────────


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        method, _ics = (metadata or {}).get("calendar", (None, ""))
        self.sent.append({"to": to, "subject": subject, "body": body, "method": method})
        return True

    def to(self, method=None) -> set[str]:
        return {m["to"] for m in self.sent if m["method"] == method}


class FakeDirectory(DirectoryReader):
    """Keycloak's view: users by Keycloak id; who is in a TN-* group."""

    def __init__(self):
        self.users: dict[str, DirectoryUser] = {}
        self.in_group: set[str] = set()
        self.fail = False
        self.calls = 0

    def ad(self, u: User, *, enabled=True, in_group=True, kc_id=None) -> None:
        kid = kc_id or u.keycloak_id
        self.users[kid] = DirectoryUser(id=kid, username=u.email.split("@")[0], email=u.email, enabled=enabled,
                                        federated=True)
        if in_group:
            self.in_group.add(kid)
        else:
            self.in_group.discard(kid)

    def local(self, u: User, username: str | None = None) -> None:
        self.users[u.keycloak_id] = DirectoryUser(id=u.keycloak_id, username=(username or u.email).lower(),
                                                  email=u.email, enabled=True, federated=False)

    def snapshot(self, group_prefix: str) -> DirectorySnapshot:
        self.calls += 1
        if self.fail:
            raise DirectoryUnavailableError("Keycloak could not be read: ConnectError")
        return DirectorySnapshot(users=dict(self.users), in_tn_group=set(self.in_group),
                                 groups_found=["TN-Students", "TN-Instructors"])


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


@pytest.fixture
def kc(monkeypatch):
    d = FakeDirectory()
    fastapi_app.dependency_overrides[sync_router.directory_reader] = lambda: d
    monkeypatch.setenv("AD_STATUS_SYNC_DRY_RUN", "false")
    monkeypatch.setenv("AD_STATUS_SYNC_BREAK_GLASS", BREAK_GLASS)
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FLOOR", "10")
    yield d
    fastapi_app.dependency_overrides.pop(sync_router.directory_reader, None)


def _person(db, tenant_id, email, role=UserRole.student, source="ad", created=None) -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=email, display_name=email.split("@")[0],
             role=role, tenant_id=tenant_id, source=source)
    if created is not None:
        u.created_at = created
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def world(db_session, client, monkeypatch, outbox, kc):
    """Tenant a: a class with an approved session; ann (AD) and bob (AD) on its roll call,
    signed in, each holding a 10-vCPU seat. Tenant b: its own admin and Student."""
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(100 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "1000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    monkeypatch.setattr(class_sessions, "seat_demand", lambda db, cls, evt: Resources(10, 0, 0))
    db = db_session
    a, b = real_tenant(db, "adsync-a"), real_tenant(db, "adsync-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    db.add(course)
    db.flush()
    db.add(RollCallSettings(tenant_id=a.id, email_domains=["corp.test"]))
    w = {
        "a": a, "b": b, "course": course,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "b_admin": real_user(db, UserRole.admin, b.id),
        "ann": _person(db, a.id, "ann@corp.test"),
        "bob": _person(db, a.id, "bob@corp.test"),
        "b_kim": _person(db, b.id, "kim@corp.test"),
    }
    # Staff and Students all exist in the directory, enabled, in a TN-* group.
    for key in ("lead", "assistant", "admin", "observer", "b_admin"):
        w[key + "_row"] = db.get(User, uuid.UUID(w[key].id))
        kc.ad(w[key + "_row"])
    for key in ("ann", "bob", "b_kim"):
        kc.ad(w[key])
    act_as(w["lead"])
    r = client.post("/classes", json={
        "name": "C201-26-03", "course_id": str(course.id), "start_date": str(DAY.date()),
        "end_date": str((DAY + timedelta(days=4)).date()), "assistant_ids": [w["assistant"].id],
        "observer_ids": [w["observer"].id],
    })
    assert r.status_code == 201, r.text
    w["class"] = r.json()
    r = client.post(f"/classes/{w['class']['id']}/roll-call", json={"emails": ["ann@corp.test", "bob@corp.test"]})
    assert r.status_code == 200 and len(r.json()["added"]) == 2, r.text
    r = client.post("/schedule/events", json={
        "name": "Lab", "start_time": NINE.isoformat(), "end_time": (NINE + timedelta(hours=3)).isoformat(),
        "class_id": w["class"]["id"], "vcpu_total": 40,
    })
    assert r.status_code == 201, r.text
    w["event"] = r.json()
    act_as(w["admin"])
    r = client.post(f"/schedule/events/{w['event']['id']}/approve")
    assert r.status_code == 200, r.text
    outbox.sent.clear()
    return w


def _entry(db, email) -> RollCallEntry:
    row = db.query(RollCallEntry).filter(RollCallEntry.email == email).one()
    db.refresh(row)
    return row


def _event(db, w) -> ScheduledEvent:
    row = db.get(ScheduledEvent, uuid.UUID(w["event"]["id"]))
    db.refresh(row)
    return row


def _active(db, u: User) -> bool:
    db.refresh(u)
    return u.is_active


def _run_now(client, w, who="admin", **body) -> dict:
    act_as(w[who])
    r = client.post("/ad-status-sync/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── The AD rules ───────────────────────────────────────────────────────────


def test_an_ad_disabled_account_is_deactivated(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    run = _run_now(client, world)
    assert run["status"] == "completed" and run["dry_run"] is False
    assert not _active(db_session, world["ann"])
    assert [(r["email"], r["reason"], r["applied"]) for r in run["results"]] == [
        ("ann@corp.test", "ad_disabled", True)]
    assert run["counts"]["deactivated"] == 1 and run["counts"]["ad_disabled"] == 1


def test_an_account_removed_from_every_tn_group_is_deactivated(client, db_session, world, kc):
    kc.ad(world["bob"], in_group=False)
    run = _run_now(client, world)
    assert not _active(db_session, world["bob"])
    assert [(r["email"], r["reason"]) for r in run["results"]] == [("bob@corp.test", "ad_no_tn_group")]


def test_an_ad_account_gone_from_the_directory_is_deactivated(client, db_session, world, kc):
    del kc.users[world["ann"].keycloak_id]
    run = _run_now(client, world)
    assert not _active(db_session, world["ann"])
    assert [r["reason"] for r in run["results"]] == ["ad_removed"]


def test_a_rebuilt_realm_matches_the_same_ad_account_by_address(client, db_session, world, kc):
    """A new Keycloak id for the same directory account is not "gone"."""
    del kc.users[world["ann"].keycloak_id]
    kc.ad(world["ann"], kc_id="kc-rebuilt-ann")
    run = _run_now(client, world)
    assert _active(db_session, world["ann"]) and run["results"] == []


def test_accounts_still_active_in_ad_are_untouched(client, db_session, world, kc, outbox):
    run = _run_now(client, world)
    assert run["status"] == "completed" and run["results"] == []
    for key in ("ann", "bob", "b_kim", "lead_row", "admin_row"):
        assert _active(db_session, world[key])
    assert _entry(db_session, "ann@corp.test").status == "signed_in"
    assert outbox.sent == []


def test_roster_rows_and_lti_students_are_not_keycloak_accounts(client, db_session, world, kc):
    """Not in Keycloak and not made from the directory: left alone."""
    roster = _person(db_session, world["a"].id, "rosie@corp.test", source="csv_import")
    lti = _person(db_session, world["a"].id, "lti@corp.test", source="lti")
    run = _run_now(client, world)
    assert _active(db_session, roster) and _active(db_session, lti)
    assert run["counts"]["not_a_keycloak_account"] == 2 and run["results"] == []


# ── What deactivating does ─────────────────────────────────────────────────


def test_seats_are_freed_and_the_instructors_told(client, db_session, world, kc, outbox):
    seq = _event(db_session, world).sequence
    kc.ad(world["ann"], enabled=False)
    run = _run_now(client, world)
    assert run["results"][0]["seats_freed"] == 1
    assert _entry(db_session, "ann@corp.test").status == "removed"
    assert _entry(db_session, "bob@corp.test").status == "signed_in"
    ev = _event(db_session, world)
    assert ev.vcpu_total == 30 and ev.sequence == seq + 1  # one 10-vCPU seat freed
    assert "ann@corp.test" in outbox.to("CANCEL")
    told = [m for m in outbox.sent if m["method"] is None]
    assert {m["to"] for m in told} == {world["lead"].email, world["assistant"].email}
    assert all("ann@corp.test" in m["body"] and "Nothing was deleted" in m["body"] for m in told)
    assert all("disabled" in m["body"] for m in told)


def test_records_are_kept(client, db_session, world, kc):
    ann = world["ann"]
    assert db_session.query(Enrollment).filter(Enrollment.user_id == ann.id).count() == 1
    db_session.add(AuditLog(user_id=ann.id, tenant_id=world["a"].id, action="exercise_submit", resource_type="exercise",
                            resource_id="x", detail="her earlier work"))
    db_session.flush()
    kc.ad(ann, enabled=False)
    _run_now(client, world)
    db_session.refresh(ann)
    assert ann.deleted_at is None and not ann.is_active  # the row stays, only inactive
    assert db_session.query(Enrollment).filter(Enrollment.user_id == ann.id).count() == 1
    assert _entry(db_session, "ann@corp.test").removed_at is not None  # the entry stays, marked removed
    assert db_session.query(AuditLog).filter(AuditLog.user_id == ann.id, AuditLog.action == "exercise_submit").count() == 1
    assert db_session.query(AdStatusSyncResult).filter(AdStatusSyncResult.user_id == ann.id).one().applied


def test_audit_rows(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    run = _run_now(client, world)
    rows = db_session.query(AuditLog).filter(AuditLog.action == "ad_status_deactivate").all()
    assert [(r.resource_id, r.tenant_id, str(r.user_id)) for r in rows] == [
        (str(world["ann"].id), world["a"].id, world["admin"].id)]
    assert "AD account is disabled" in rows[0].detail and "C201-26-03" in rows[0].detail
    runs = db_session.query(AuditLog).filter(AuditLog.action == "ad_status_sync_run").all()
    assert [(r.resource_id, str(r.user_id), r.tenant_id) for r in runs] == [
        (run["id"], world["admin"].id, world["a"].id)]


# ── Local accounts and break-glass ─────────────────────────────────────────


def _local(db, w, kc, email, *, created_days_ago, seen_days_ago=None, username=None) -> User:
    u = _person(db, w["a"].id, email, role=UserRole.admin, source="local",
                created=datetime.now(UTC) - timedelta(days=created_days_ago))
    kc.local(u, username)
    if seen_days_ago is not None:
        db.add(UserLastSeen(user_id=u.id, last_seen_at=datetime.now(UTC) - timedelta(days=seen_days_ago)))
    db.flush()
    return u


def _first_run_long_ago(db, days=400):
    db.add(AdStatusSyncRun(slot="nightly:history", trigger="nightly", dry_run=True, status="completed",
                           started_at=datetime.now(UTC) - timedelta(days=days), counts={}))
    db.flush()


def test_a_local_account_unused_for_90_days_is_disabled(client, db_session, world, kc):
    _first_run_long_ago(db_session)
    stale = _local(db_session, world, kc, "old@tn.local", created_days_ago=300, seen_days_ago=91)
    fresh = _local(db_session, world, kc, "new@tn.local", created_days_ago=300, seen_days_ago=89)
    never = _local(db_session, world, kc, "never@tn.local", created_days_ago=120)
    run = _run_now(client, world)
    assert not _active(db_session, stale) and not _active(db_session, never)
    assert _active(db_session, fresh)
    assert sorted((r["email"], r["reason"]) for r in run["results"]) == [
        ("never@tn.local", "local_unused"), ("old@tn.local", "local_unused")]


def test_time_before_the_first_run_does_not_count(client, db_session, world, kc):
    """No last-use record yet: counted from the first sync run, so nobody goes on the first night."""
    old = _local(db_session, world, kc, "old@tn.local", created_days_ago=400)
    run = _run_now(client, world)
    assert _active(db_session, old) and run["results"] == []


def test_break_glass_is_never_disabled(client, db_session, world, kc):
    _first_run_long_ago(db_session)
    bg = _local(db_session, world, kc, BREAK_GLASS, created_days_ago=1000, seen_days_ago=900, username=BREAK_GLASS)
    run = _run_now(client, world)
    assert _active(db_session, bg)
    assert run["counts"]["break_glass_kept"] == 1 and run["results"] == []
    # Nor by a nightly run, nor when it is also in no group (local accounts are not AD's to judge).
    service.run_nightly(db_session, kc, now=NIGHT)
    assert _active(db_session, bg)


def test_break_glass_under_a_new_keycloak_id_is_not_removed(client, db_session, world, kc):
    """bootstrap_admin marks the local administrator ``ad``; a rebuilt realm gives it a new id
    until 80-seed re-links it. It is still break-glass, never "AD account gone"."""
    bg = _person(db_session, world["a"].id, BREAK_GLASS, role=UserRole.admin, source="ad")
    kc.users["kc-new-id"] = DirectoryUser(id="kc-new-id", username=BREAK_GLASS, email=BREAK_GLASS, enabled=True,
                                          federated=False)
    run = _run_now(client, world)
    assert _active(db_session, bg) and run["results"] == [] and run["counts"]["break_glass_kept"] == 1


def test_without_a_break_glass_account_local_accounts_are_not_checked(client, db_session, world, kc, monkeypatch):
    monkeypatch.setenv("AD_STATUS_SYNC_BREAK_GLASS", "")
    monkeypatch.delenv("TN_BOOTSTRAP_ADMIN_UPN", raising=False)
    _first_run_long_ago(db_session)
    stale = _local(db_session, world, kc, "old@tn.local", created_days_ago=300, seen_days_ago=200)
    run = _run_now(client, world)
    assert _active(db_session, stale)
    assert run["counts"]["local_not_checked_no_break_glass"] == 1


def test_a_directory_account_named_like_break_glass_follows_ad(client, db_session, world, kc):
    """Break-glass is the LOCAL account of that name; an AD account is judged by AD."""
    u = _person(db_session, world["a"].id, BREAK_GLASS)
    kc.ad(u, enabled=False)
    _run_now(client, world)
    assert not _active(db_session, u)


def test_re_enabling_restarts_the_90_days(client, db_session, world, kc):
    _first_run_long_ago(db_session)
    stale = _local(db_session, world, kc, "old@tn.local", created_days_ago=300, seen_days_ago=120)
    _run_now(client, world)
    assert not _active(db_session, stale)
    act_as(world["admin"])
    r = client.patch(f"/users/{stale.id}", params={"is_active": True})
    assert r.status_code == 200, r.text
    run = _run_now(client, world)
    assert _active(db_session, stale) and run["results"] == []


# ── Last use and the break-glass alert ─────────────────────────────────────


def test_signing_in_records_last_use_and_announces_break_glass(db_session, world, monkeypatch):
    from app.ad_status_sync import last_seen

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(last_seen, "_written", {})
    bg = _person(db_session, world["a"].id, BREAK_GLASS, role=UserRole.student, source="local")
    ann = world["ann"]

    def sign_in(u, **claims):
        ident = TokenPayload(sub=u.keycloak_id, email=u.email, preferred_username=claims.pop("username", u.email),
                             **claims)
        return asyncio.run(auth.get_signed_in_user(None, ident, db_session))

    from app import metrics

    before = metrics.break_glass_sign_ins_total._values[()]
    sign_in(ann, ad_object_guid=str(uuid.uuid4()))
    sign_in(bg)
    sign_in(bg)  # within the hour: no second write
    for u in (ann, bg):
        assert db_session.get(UserLastSeen, u.id) is not None
    alerts = db_session.query(AuditLog).filter(AuditLog.action == "break_glass_sign_in").all()
    assert [a.resource_id for a in alerts] == [str(bg.id)]
    assert metrics.break_glass_sign_ins_total._values[()] == before + 1  # Prometheus: BreakGlassAccountUsed


# ── Dry run, held runs, failures ───────────────────────────────────────────


def test_a_dry_run_changes_nothing(client, db_session, world, kc, outbox, monkeypatch):
    monkeypatch.setenv("AD_STATUS_SYNC_DRY_RUN", "true")
    kc.ad(world["ann"], enabled=False)
    seq = _event(db_session, world).sequence
    run = _run_now(client, world)
    assert run["dry_run"] is True and run["status"] == "completed"
    assert [(r["email"], r["applied"]) for r in run["results"]] == [("ann@corp.test", False)]
    assert _active(db_session, world["ann"])
    assert _entry(db_session, "ann@corp.test").status == "signed_in"
    assert _event(db_session, world).sequence == seq
    assert outbox.sent == []
    assert db_session.query(AuditLog).filter(AuditLog.action == "ad_status_deactivate").count() == 0


def test_run_now_may_ask_for_a_dry_run_whatever_the_setting(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    run = _run_now(client, world, dry_run=True)
    assert run["dry_run"] is True and _active(db_session, world["ann"])


def test_a_run_over_the_limit_is_held(client, db_session, world, kc, monkeypatch):
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FLOOR", "1")
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FRACTION", "0.1")
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["bob"], enabled=False)
    run = _run_now(client, world)
    assert run["status"] == "held" and "nothing was changed" in run["error"]
    assert _active(db_session, world["ann"]) and _active(db_session, world["bob"])
    assert all(not r["applied"] for r in run["results"])
    run = _run_now(client, world, override_limit=True)
    assert run["status"] == "completed"
    assert not _active(db_session, world["ann"]) and not _active(db_session, world["bob"])


def test_keycloak_unreachable_fails_the_run_and_changes_nothing(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    kc.fail = True
    run = _run_now(client, world)
    assert run["status"] == "failed" and "Keycloak" in run["error"]
    assert _active(db_session, world["ann"])


def test_no_tn_groups_in_keycloak_fails_the_run(client, db_session, world, kc, monkeypatch):
    """A group mapper that brings nothing must not read as everyone leaving the TN-* groups."""
    monkeypatch.setattr(FakeDirectory, "snapshot", lambda self, prefix: DirectorySnapshot(users=dict(self.users)))
    run = _run_now(client, world)
    assert run["status"] == "failed" and "no group named TN-*" in run["error"]
    assert _active(db_session, world["ann"])


# ── Replicas ───────────────────────────────────────────────────────────────


def test_one_nightly_run_per_night_whatever_the_replicas(db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    assert service.run_nightly(db_session, kc, now=NIGHT.replace(hour=1)) is None  # before the hour
    first = service.run_nightly(db_session, kc, now=NIGHT)
    assert first is not None and first.run.status == "completed"
    assert service.run_nightly(db_session, kc, now=NIGHT + timedelta(minutes=5)) is None
    assert kc.calls == 1
    # A replica that read "not run yet" just before: the slot is unique, its insert loses.
    lost = service.claim(db_session, f"nightly:{NIGHT.date()}", "nightly", tenant_id=None, actor_id=None,
                         dry_run=False, now=NIGHT)
    assert lost is None
    assert db_session.query(AdStatusSyncRun).filter(AdStatusSyncRun.slot == f"nightly:{NIGHT.date()}").count() == 1


def test_a_replica_with_a_stale_view_deactivates_nothing_twice(db_session, world, kc, outbox):
    kc.ad(world["ann"], enabled=False)
    snap = kc.snapshot("TN-")
    stale_plan = service.plan(db_session, snap, NIGHT, None)  # replica B read before A applied
    service.run_nightly(db_session, kc, now=NIGHT)  # replica A
    seq = _event(db_session, world).sequence
    b = service.claim(db_session, "manual:b", "manual", tenant_id=None, actor_id=None, dry_run=False, now=NIGHT)
    for d in stale_plan.decisions:
        won, invites, left = service.deactivate(db_session, b, d.user, d.reason, NIGHT)
        assert (won, invites, left) == (False, [], [])
    db_session.flush()
    assert _event(db_session, world).sequence == seq
    assert db_session.query(AuditLog).filter(AuditLog.action == "ad_status_deactivate").count() == 1
    assert db_session.query(AdStatusSyncResult).filter(AdStatusSyncResult.applied == True).count() == 1  # noqa: E712


def test_a_nightly_run_covers_every_tenant(db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["b_kim"], enabled=False)
    out = service.run_nightly(db_session, kc, now=NIGHT)
    assert not _active(db_session, world["ann"]) and not _active(db_session, world["b_kim"])
    assert out.run.counts["by_tenant"][str(world["b"].id)]["deactivated"] == 1


# ── RBAC and tenancy ───────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["lead", "observer"])
def test_only_admins(client, world, who):
    act_as(world[who])
    assert client.get("/ad-status-sync").status_code == 403
    assert client.post("/ad-status-sync/runs", json={}).status_code == 403


def test_run_now_is_the_callers_tenant_only(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["b_kim"], enabled=False)
    run = _run_now(client, world, who="b_admin")
    assert [r["email"] for r in run["results"]] == ["kim@corp.test"]
    assert _active(db_session, world["ann"])  # tenant a's account untouched
    # Tenant a's admin does not see tenant b's manual run.
    act_as(world["admin"])
    assert client.get(f"/ad-status-sync/runs/{run['id']}").status_code == 404
    assert run["id"] not in {r["id"] for r in client.get("/ad-status-sync").json()["runs"]}


def test_a_nightly_run_shows_each_tenant_only_its_own_accounts(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["b_kim"], enabled=False)
    service.run_nightly(db_session, kc, now=NIGHT)
    act_as(world["b_admin"])
    body = client.get("/ad-status-sync").json()
    assert [r["email"] for r in body["latest"]["results"]] == ["kim@corp.test"]
    assert body["latest"]["counts"]["deactivated"] == 1
    assert body["latest"]["counts"]["examined"] == 2  # b_admin and kim, not tenant a's accounts
    assert body["break_glass_usernames"] == [BREAK_GLASS]


# ── The Keycloak reader: GET only ──────────────────────────────────────────


def test_the_keycloak_reader_never_writes():
    seen: list[httpx.Request] = []
    users = [
        {"id": f"u{i}", "username": f"user{i}", "email": f"u{i}@corp.test", "enabled": i != 3,
         "federationLink": "ldap-1" if i < 600 else None}
        for i in range(601)  # two pages of users
    ]
    groups = [{"id": "g1", "name": "TN-Students"}, {"id": "g2", "name": "TN-Instructors"},
              {"id": "g3", "name": "Other-TN-Thing"}]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "t"})
        first, mx = int(req.url.params.get("first", 0)), int(req.url.params.get("max", 100))
        if path.endswith("/users"):
            return httpx.Response(200, json=users[first:first + mx])
        if path.endswith("/groups"):
            return httpx.Response(200, json=groups if first == 0 else [])
        if path.endswith("/g1/members"):
            return httpx.Response(200, json=[{"id": "u1"}, {"id": "u2"}] if first == 0 else [])
        if path.endswith("/g2/members"):
            return httpx.Response(200, json=[{"id": "u5"}] if first == 0 else [])
        if path.endswith("/g3/members"):
            return httpx.Response(200, json=[{"id": "u9"}])
        return httpx.Response(404)

    reader = KeycloakDirectoryReader(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cfg={"base": "http://kc/auth", "realm": "truenorth", "client_id": "api", "client_secret": "s"},
    )
    snap = reader.snapshot("TN-")
    assert len(snap.users) == 601 and snap.users["u3"].enabled is False
    assert snap.users["u1"].federated and not snap.users["u600"].federated
    assert snap.in_tn_group == {"u1", "u2", "u5"} and sorted(snap.groups_found) == ["TN-Instructors", "TN-Students"]
    writes = [(r.method, r.url.path) for r in seen if r.method != "GET"]
    assert writes == [("POST", "/auth/realms/truenorth/protocol/openid-connect/token")]
    assert all("/admin/realms/truenorth/" in r.url.path for r in seen if r.method == "GET")


def test_the_keycloak_reader_stops_on_any_error():
    def handler(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(503)

    reader = KeycloakDirectoryReader(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cfg={"base": "http://kc/auth", "realm": "truenorth", "client_id": "api", "client_secret": "s"},
    )
    with pytest.raises(DirectoryUnavailableError):
        reader.snapshot("TN-")


def test_a_run_through_the_api_sends_no_keycloak_write(client, db_session, world, kc):
    """The whole run, end to end, against a recording Keycloak: GET only (plus the token)."""
    seen: list[httpx.Request] = []
    by_id = {u.id: u for u in kc.users.values()}

    def handler(req):
        seen.append(req)
        p = req.url.path
        first = int(req.url.params.get("first", 0))
        if p.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t"})
        if p.endswith("/users"):
            rows = [{"id": u.id, "username": u.username, "email": u.email, "enabled": u.id != world["ann"].keycloak_id,
                     "federationLink": "ldap"} for u in by_id.values()]
            return httpx.Response(200, json=rows[first:first + 500])
        if p.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "g", "name": "TN-Students"}] if first == 0 else [])
        if p.endswith("/members"):
            return httpx.Response(200, json=[{"id": i} for i in kc.in_group] if first == 0 else [])
        return httpx.Response(404)

    reader = KeycloakDirectoryReader(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cfg={"base": "http://kc/auth", "realm": "truenorth", "client_id": "api", "client_secret": "s"},
    )
    fastapi_app.dependency_overrides[sync_router.directory_reader] = lambda: reader
    run = _run_now(client, world)
    assert [r["email"] for r in run["results"]] == ["ann@corp.test"]
    assert {r.method for r in seen} == {"GET", "POST"}
    assert [r.url.path for r in seen if r.method == "POST"] == ["/auth/realms/truenorth/protocol/openid-connect/token"]
