"""Identity 13: nightly AD status sync and the 90-day rule for local accounts (ADR 0014 §6; #290).

The rules under test (app/ad_status_sync):

- TrueNorth only reads the directory, through Keycloak's Admin API: GET requests and the
  client-credentials token, never a write (asserted on the HTTP transport).
- An AD account that is disabled, or a known AD account that is gone: the TrueNorth account
  is deactivated (``is_active`` false). One still enabled is untouched, TN-* groups or not
  (roll-call Students have none).
- Deactivating frees their roll-call seats (``remove_from_roll_call``: seat freed, CANCEL),
  emails the class's instructors, writes an audit row, and deletes nothing.
- A local Keycloak account unused for 90 days is deactivated; break-glass never is.
- A dry run records and changes nothing. Per tenant: an account is deactivated only when a
  run 12-36 h earlier (the night before) found it for the same reason; a rule family over its
  limit (AD rules against AD accounts, the 90-day rule against local ones) is held while
  others go on; never the last admin. Result-transfer subjects are never judged.
- Any number of replicas: one nightly run per night; each account deactivated once.
- Administrators only, their own tenant only.

Time travel: ``service.run_nightly(db, reader, now=...)``, as the class clocks are tested.
``_apply`` runs the night before (``run_nightly`` 24 h ago), then "run now", as a deactivation
needs a finding from the night before.
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
PREV_NIGHT = NIGHT - timedelta(days=1)
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
    """Keycloak's view: users by Keycloak id."""

    def __init__(self):
        self.users: dict[str, DirectoryUser] = {}
        self.fail = False
        self.calls = 0

    def ad(self, u: User, *, enabled=True, kc_id=None, username=None) -> None:
        kid = kc_id or u.keycloak_id
        self.users[kid] = DirectoryUser(id=kid, username=(username or u.email.split("@")[0]).lower(), email=u.email,
                                        enabled=enabled, federated=True)

    def local(self, u: User, username: str | None = None) -> None:
        self.users[u.keycloak_id] = DirectoryUser(id=u.keycloak_id, username=(username or u.email).lower(),
                                                  email=u.email, enabled=True, federated=False)

    def snapshot(self) -> DirectorySnapshot:
        self.calls += 1
        if self.fail:
            raise DirectoryUnavailableError("Keycloak could not be read: ConnectError")
        return DirectorySnapshot(users=dict(self.users))


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


@pytest.fixture
def kc(monkeypatch):
    d = FakeDirectory()
    # The local recovery account beside AD (tn_keycloak_local_admin): it makes BREAK_GLASS a
    # break-glass account at all (a name that is not a local Keycloak user is none).
    d.users["kc-breakglass"] = DirectoryUser(id="kc-breakglass", username=BREAK_GLASS, email=BREAK_GLASS,
                                             enabled=True, federated=False)
    fastapi_app.dependency_overrides[sync_router.directory_reader] = lambda: d
    monkeypatch.setenv("AD_STATUS_SYNC_DRY_RUN", "false")
    monkeypatch.setenv("AD_STATUS_SYNC_BREAK_GLASS", BREAK_GLASS)
    monkeypatch.setenv("AD_STATUS_SYNC_HOUR_UTC", "0")  # time travel: any hour is the nightly hour
    monkeypatch.delenv("AD_STATUS_SYNC_MAX_FLOOR", raising=False)
    monkeypatch.delenv("AD_STATUS_SYNC_MAX_FRACTION", raising=False)
    yield d
    fastapi_app.dependency_overrides.pop(sync_router.directory_reader, None)


def _person(db, tenant_id, email, role=UserRole.student, source="ad", created=None, guid=None) -> User:
    """``guid``: whether the directory's claims gave it an AD objectGUID (default: source ad)."""
    if guid is None:
        guid = source == "ad"
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=email, display_name=email.split("@")[0],
             role=role, tenant_id=tenant_id, source=source, ad_object_guid=str(uuid.uuid4()) if guid else None)
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
        "db": db, "kc": kc, "a": a, "b": b, "course": course,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "b_admin": real_user(db, UserRole.admin, b.id),
        "ann": _person(db, a.id, "ann@corp.test"),
        "bob": _person(db, a.id, "bob@corp.test"),
        "b_kim": _person(db, b.id, "kim@corp.test"),
    }
    # Staff and Students all exist in the directory, enabled.
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


def _apply(client, w, who="admin", reader=None, **body) -> dict:
    """Last night's run finds; "run now" (finding the same, for the same reason) deactivates."""
    service.run_nightly(w["db"], reader or w["kc"], now=datetime.now(UTC) - timedelta(hours=24))
    return _run_now(client, w, who, **body)


# ── The AD rules ───────────────────────────────────────────────────────────


def test_an_ad_disabled_account_is_deactivated(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    run = _apply(client, world)
    assert run["status"] == "completed" and run["dry_run"] is False
    assert not _active(db_session, world["ann"])
    assert [(r["email"], r["reason"], r["applied"]) for r in run["results"]] == [
        ("ann@corp.test", "ad_disabled", True)]
    assert run["counts"]["deactivated"] == 1 and run["counts"]["ad_disabled"] == 1


def test_a_roll_call_student_in_no_tn_group_is_not_deactivated(client, db_session, world, kc):
    """B1: roll-call admission gives no TN-* group (classes/onboarding.py). Enabled in AD is
    enough; group membership is not even read (the reader asks for no /groups)."""
    ann = world["ann"]
    assert _entry(db_session, "ann@corp.test").status == "signed_in"
    kc.ad(ann, enabled=True)  # federated, enabled, member of nothing
    run = _apply(client, world)
    assert _active(db_session, ann) and run["results"] == []
    assert _entry(db_session, "ann@corp.test").status == "signed_in"
    assert "ad_no_tn_group" not in service.REASONS


def test_a_known_ad_account_gone_from_the_directory_is_deactivated(client, db_session, world, kc):
    del kc.users[world["ann"].keycloak_id]
    run = _apply(client, world)
    assert not _active(db_session, world["ann"])
    assert [r["reason"] for r in run["results"]] == ["ad_removed"]


def test_a_rebuilt_realm_matches_the_same_ad_account_by_address(client, db_session, world, kc):
    """A new Keycloak id for the same directory account is not "gone"."""
    del kc.users[world["ann"].keycloak_id]
    kc.ad(world["ann"], kc_id="kc-rebuilt-ann")
    run = _apply(client, world)
    assert _active(db_session, world["ann"]) and run["results"] == []


def test_a_rebuilt_realm_on_a_no_ad_install_removes_nobody(client, db_session, world, kc):
    """Registration marks every account ``source = ad`` (routers/registration.py), local ones
    included. Without an AD objectGUID, a lost Keycloak id is not "AD account gone"."""
    local = _person(db_session, world["a"].id, "local@tn.local", source="ad", guid=False)
    kc.users["kc-rebuilt"] = DirectoryUser(id="kc-rebuilt", username="local@tn.local", email="local@tn.local",
                                           enabled=True, federated=False)
    run = _apply(client, world)
    assert _active(db_session, local) and run["results"] == []
    assert run["counts"]["not_a_keycloak_account"] == 1


def test_accounts_still_active_in_ad_are_untouched(client, db_session, world, kc, outbox):
    run = _apply(client, world)
    assert run["status"] == "completed" and run["results"] == []
    for key in ("ann", "bob", "b_kim", "lead_row", "admin_row"):
        assert _active(db_session, world[key])
    assert _entry(db_session, "ann@corp.test").status == "signed_in"
    assert outbox.sent == []


def test_roster_rows_and_lti_students_are_not_keycloak_accounts(client, db_session, world, kc):
    """Not in Keycloak and not made from the directory: left alone."""
    roster = _person(db_session, world["a"].id, "rosie@corp.test", source="csv_import")
    lti = _person(db_session, world["a"].id, "lti@corp.test", source="lti")
    run = _apply(client, world)
    assert _active(db_session, roster) and _active(db_session, lti)
    assert run["counts"]["not_a_keycloak_account"] == 2 and run["results"] == []


# ── What deactivating does ─────────────────────────────────────────────────


def test_seats_are_freed_and_the_instructors_told(client, db_session, world, kc, outbox):
    seq = _event(db_session, world).sequence
    kc.ad(world["ann"], enabled=False)
    run = _apply(client, world)
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
    _apply(client, world)
    db_session.refresh(ann)
    assert ann.deleted_at is None and not ann.is_active  # the row stays, only inactive
    assert db_session.query(Enrollment).filter(Enrollment.user_id == ann.id).count() == 1
    assert _entry(db_session, "ann@corp.test").removed_at is not None  # the entry stays, marked removed
    assert db_session.query(AuditLog).filter(AuditLog.user_id == ann.id, AuditLog.action == "exercise_submit").count() == 1
    assert db_session.query(AdStatusSyncResult).filter(AdStatusSyncResult.user_id == ann.id,
                                                       AdStatusSyncResult.applied == True).count() == 1  # noqa: E712


def test_audit_rows(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    run = _apply(client, world)
    rows = db_session.query(AuditLog).filter(AuditLog.action == "ad_status_deactivate").all()
    assert [(r.resource_id, r.tenant_id, str(r.user_id)) for r in rows] == [
        (str(world["ann"].id), world["a"].id, world["admin"].id)]
    assert "AD account is disabled" in rows[0].detail and "C201-26-03" in rows[0].detail
    runs = db_session.query(AuditLog).filter(AuditLog.action == "ad_status_sync_run").all()
    # One per run: last night's (the system, every tenant) and the admin's "run now" in their tenant.
    manual = [(r.resource_id, str(r.user_id), r.tenant_id) for r in runs if r.user_id is not None]
    nightly = [(r.user_id, r.tenant_id) for r in runs if r.user_id is None]
    assert manual == [(run["id"], world["admin"].id, world["a"].id)] and nightly == [(None, None)]


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
    run = _apply(client, world)
    assert not _active(db_session, stale) and not _active(db_session, never)
    assert _active(db_session, fresh)
    assert sorted((r["email"], r["reason"]) for r in run["results"]) == [
        ("never@tn.local", "local_unused"), ("old@tn.local", "local_unused")]


def test_time_before_the_first_run_does_not_count(client, db_session, world, kc):
    """No last-use record yet: counted from the first sync run, so nobody goes on the first night."""
    old = _local(db_session, world, kc, "old@tn.local", created_days_ago=400)
    run = _apply(client, world)
    assert _active(db_session, old) and run["results"] == []


def test_break_glass_is_never_disabled(client, db_session, world, kc):
    _first_run_long_ago(db_session)
    bg = _local(db_session, world, kc, BREAK_GLASS, created_days_ago=1000, seen_days_ago=900, username=BREAK_GLASS)
    run = _apply(client, world)
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
    run = _apply(client, world)
    assert _active(db_session, bg) and run["results"] == [] and run["counts"]["break_glass_kept"] == 1


def test_without_a_break_glass_account_local_accounts_are_not_checked(client, db_session, world, kc, monkeypatch):
    monkeypatch.setenv("AD_STATUS_SYNC_BREAK_GLASS", "")
    monkeypatch.delenv("TN_BOOTSTRAP_ADMIN_UPN", raising=False)
    _first_run_long_ago(db_session)
    stale = _local(db_session, world, kc, "old@tn.local", created_days_ago=300, seen_days_ago=200)
    run = _apply(client, world)
    assert _active(db_session, stale)
    assert run["counts"]["local_not_checked_no_break_glass"] == 1


def test_a_directory_account_named_like_break_glass_follows_ad(client, db_session, world, kc):
    """Break-glass is the LOCAL account of that name; an AD account is judged by AD."""
    u = _person(db_session, world["a"].id, BREAK_GLASS)
    kc.ad(u, enabled=False)
    _apply(client, world)
    assert not _active(db_session, u)


def test_a_break_glass_name_that_is_only_an_ad_account_turns_the_90_day_rule_off(client, db_session, world, kc):
    """The local administrator turned off (tn_keycloak_local_admin false) leaves the variable
    naming an AD account: there is no break-glass, so local accounts are not checked."""
    _first_run_long_ago(db_session)
    del kc.users["kc-breakglass"]  # no local account of that name any more
    ad_admin = _person(db_session, world["a"].id, BREAK_GLASS, role=UserRole.admin)
    kc.ad(ad_admin, username=BREAK_GLASS)
    stale = _local(db_session, world, kc, "old@tn.local", created_days_ago=300, seen_days_ago=200)
    run = _apply(client, world)
    assert _active(db_session, stale) and _active(db_session, ad_admin)
    assert run["counts"]["local_not_checked_no_break_glass"] == 1


def test_re_enabling_restarts_the_90_days(client, db_session, world, kc):
    _first_run_long_ago(db_session)
    stale = _local(db_session, world, kc, "old@tn.local", created_days_ago=300, seen_days_ago=120)
    _apply(client, world)
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
    run = _apply(client, world)
    assert run["dry_run"] is True and run["status"] == "completed"
    assert [(r["email"], r["applied"]) for r in run["results"]] == [("ann@corp.test", False)]
    assert _active(db_session, world["ann"])
    assert _entry(db_session, "ann@corp.test").status == "signed_in"
    assert _event(db_session, world).sequence == seq
    assert outbox.sent == []
    assert db_session.query(AuditLog).filter(AuditLog.action == "ad_status_deactivate").count() == 0


@pytest.mark.parametrize("override", [False, True])
def test_while_dry_run_is_set_the_api_refuses_a_real_run(client, db_session, world, kc, monkeypatch, override):
    """SHOULD 1: AD_STATUS_SYNC_DRY_RUN is the switch; no request turns it off, override or not."""
    monkeypatch.setenv("AD_STATUS_SYNC_DRY_RUN", "true")
    kc.ad(world["ann"], enabled=False)
    act_as(world["admin"])
    for _ in range(2):
        r = client.post("/ad-status-sync/runs", json={"dry_run": False, "override_limit": override})
        assert r.status_code == 409 and "AD_STATUS_SYNC_DRY_RUN" in r.text
    assert _active(db_session, world["ann"])
    assert db_session.query(AdStatusSyncRun).count() == 0


def test_run_now_may_ask_for_a_dry_run_whatever_the_setting(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    run = _apply(client, world, dry_run=True)
    assert run["dry_run"] is True and _active(db_session, world["ann"])


def test_found_once_is_not_enough(client, db_session, world, kc):
    """A finding waits for the same finding from the night before. Two runs a moment apart
    confirm nothing; last night's finding does."""
    kc.ad(world["ann"], enabled=False)
    first = _run_now(client, world)
    assert [(r["email"], r["applied"]) for r in first["results"]] == [("ann@corp.test", False)]
    assert first["counts"]["awaiting_confirmation"] == 1
    second = _run_now(client, world)  # a moment later: not a confirmation
    assert _active(db_session, world["ann"]) and second["counts"]["awaiting_confirmation"] == 1
    service.run_nightly(db_session, kc, now=datetime.now(UTC) - timedelta(hours=24))
    third = _run_now(client, world)
    assert not _active(db_session, world["ann"]) and third["counts"]["deactivated"] == 1


def test_a_dry_run_now_then_a_real_one_do_not_confirm_each_other(client, db_session, world, kc):
    """Round 2, blocker 2: two clicks must not be enough."""
    kc.ad(world["ann"], enabled=False)
    _run_now(client, world, dry_run=True)
    _run_now(client, world)
    assert _active(db_session, world["ann"])


def test_a_months_old_finding_for_another_reason_confirms_nothing(db_session, world, kc):
    """Round 2, blocker 2: found disabled 120 days ago, re-enabled, now missing once: a single
    sighting of the new condition, not a confirmed one."""
    kc.ad(world["ann"], enabled=False)
    service.run_nightly(db_session, kc, now=NIGHT - timedelta(days=120))
    kc.ad(world["ann"], enabled=True)
    del kc.users[world["ann"].keycloak_id]
    out = service.run_nightly(db_session, kc, now=NIGHT)
    assert _active(db_session, world["ann"])
    assert out.run.counts["by_tenant"][str(world["a"].id)]["awaiting_confirmation"] == 1


def test_a_finding_from_the_night_before_with_another_reason_confirms_nothing(db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    service.run_nightly(db_session, kc, now=PREV_NIGHT)  # ad_disabled
    del kc.users[world["ann"].keycloak_id]
    service.run_nightly(db_session, kc, now=NIGHT)  # ad_removed: a different finding
    assert _active(db_session, world["ann"])


@pytest.mark.parametrize("hours, confirms", [(11, False), (13, True), (35, True), (37, False)])
def test_only_a_finding_12_to_36_hours_old_confirms(db_session, world, kc, hours, confirms):
    kc.ad(world["ann"], enabled=False)
    service.run_nightly(db_session, kc, now=NIGHT - timedelta(hours=hours))
    service.run_now(db_session, kc, tenant_id=world["a"].id, actor_id=uuid.UUID(world["admin"].id), dry_run=False,
                    override_limit=False, now=NIGHT)
    assert _active(db_session, world["ann"]) is not confirms


def test_a_user_missing_from_one_answer_keeps_their_account(db_session, world, kc):
    """A page Keycloak skipped one night: gone, back the next night, gone again: never two
    nights in a row, never deactivated."""
    ann = kc.users.pop(world["ann"].keycloak_id)
    service.run_nightly(db_session, kc, now=NIGHT - timedelta(days=2))
    kc.users[ann.id] = ann
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
    del kc.users[ann.id]
    out = service.run_nightly(db_session, kc, now=NIGHT)
    assert _active(db_session, world["ann"])
    assert out.run.counts["by_tenant"][str(world["a"].id)]["awaiting_confirmation"] == 1


def test_local_accounts_do_not_dilute_the_ad_hold(db_session, world, kc):
    """Round 2, blocker 1: a federation outage reads every AD account of a tenant disabled. Its
    30 active local accounts must not make 10 of 40 look like a quarter."""
    c = real_tenant(db_session, "adv-dilute")
    staff = [_person(db_session, c.id, f"admin{i}@corp.test", role=UserRole.admin) for i in range(2)]
    staff += [_person(db_session, c.id, f"inst{i}@corp.test", role=UserRole.instructor) for i in range(8)]
    for u in staff:
        kc.ad(u, enabled=False)
    for i in range(30):
        s = _person(db_session, c.id, f"local{i}@tn.local", source="local", guid=False)
        kc.local(s)
        db_session.add(UserLastSeen(user_id=s.id, last_seen_at=NIGHT - timedelta(days=1)))
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
    out = service.run_nightly(db_session, kc, now=NIGHT)
    counts = out.run.counts["by_tenant"][str(c.id)]
    assert counts["held_ad"] == 1 and counts["held_ad_eligible"] == 10 and counts["eligible_local"] == 30
    assert all(_active(db_session, u) for u in staff)


def test_result_transfer_subjects_are_never_judged(db_session, world, kc):
    """Round 2, should-fix 4: subjects (source subject) count for no rule and pad nothing."""
    from app.result_transfer.pseudonymous import SUBJECT_SOURCE

    assert service.SUBJECT_SOURCE == SUBJECT_SOURCE
    c = real_tenant(db_session, "adv-subjects")
    staff = [_person(db_session, c.id, f"inst{i}@corp.test", role=UserRole.instructor) for i in range(10)]
    for u in staff:
        kc.ad(u, enabled=False)
    subjects = []
    for i in range(30):
        s = _person(db_session, c.id, f"subject-{i}@subjects.invalid", source=SUBJECT_SOURCE, guid=False,
                    created=NIGHT - timedelta(days=400))
        kc.local(s)  # even with a Keycloak user, and long unused
        subjects.append(s)
    _first_run_long_ago(db_session)
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
    out = service.run_nightly(db_session, kc, now=NIGHT)
    counts = out.run.counts["by_tenant"][str(c.id)]
    assert counts["result_transfer_subject"] == 30 and "eligible_local" not in counts
    assert counts["held_ad"] == 1
    assert all(_active(db_session, s) for s in subjects + staff)


def test_many_small_tenants_are_each_held(db_session, world, kc):
    """Ten four-account tenants that all read disabled: each is held (4 > 3), none emptied."""
    victims = []
    for t in range(10):
        c = real_tenant(db_session, f"adv-tiny{t}")
        kc.ad(_person(db_session, c.id, f"a{t}@corp.test", role=UserRole.admin), enabled=False)
        for i in range(3):
            s = _person(db_session, c.id, f"s{t}-{i}@corp.test")
            kc.ad(s, enabled=False)
            victims.append(s)
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
    service.run_nightly(db_session, kc, now=NIGHT)
    assert all(_active(db_session, s) for s in victims)


def test_the_panel_warns_when_break_glass_is_not_found(client, db_session, world, kc):
    """Round 2, should-fix 5: a configured name that is no local Keycloak user turns the 90-day
    rule off, and the panel says so."""
    act_as(world["admin"])
    _run_now(client, world)
    assert client.get("/ad-status-sync").json()["break_glass_warning"] is None
    del kc.users["kc-breakglass"]
    _run_now(client, world)
    warning = client.get("/ad-status-sync").json()["break_glass_warning"]
    assert "not found" in warning and BREAK_GLASS in warning and "90-day rule" in warning


def test_deleting_the_last_admin_is_refused(client, db_session, world):
    """Round 2, should-fix 3: the manual path has the same last-admin guard."""
    act_as(world["admin"])
    r = client.delete(f"/users/{world['admin'].id}")
    assert r.status_code == 409 and "last active administrator" in r.text
    assert _active(db_session, world["admin_row"])
    second = real_user(db_session, UserRole.admin, world["a"].id)
    r = client.delete(f"/users/{second.id}")
    assert r.status_code == 204, r.text


@pytest.mark.parametrize("change", [{"is_active": False}, {"role": "instructor"}, {"role": "student"}])
def test_patching_away_the_last_admin_is_refused(client, db_session, world, change):
    """PATCH /users has the same locked last-admin guard: neither disabling nor demoting the
    tenant's last active admin; with a second admin, both are allowed."""
    act_as(world["admin"])
    row = world["admin_row"]
    r = client.patch(f"/users/{row.id}", params=change)
    assert r.status_code == 409 and "last active administrator" in r.text, r.text
    db_session.refresh(row)
    assert row.is_active and row.role == UserRole.admin
    r = client.patch(f"/users/{row.id}", params={"display_name": "Still the admin", "role": "admin"})
    assert r.status_code == 200, r.text  # other edits, and keeping the role, are fine
    real_user(db_session, UserRole.admin, world["a"].id)
    r = client.patch(f"/users/{row.id}", params=change)
    assert r.status_code == 200, r.text


def test_a_tenant_over_its_limit_is_held(client, db_session, world, kc, monkeypatch):
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FLOOR", "1")
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FRACTION", "0.1")
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["bob"], enabled=False)
    run = _apply(client, world)
    assert run["status"] == "held" and "none of them was changed in this tenant" in run["error"]
    assert _active(db_session, world["ann"]) and _active(db_session, world["bob"])
    assert all(not r["applied"] for r in run["results"])
    run = _run_now(client, world, override_limit=True)
    assert run["status"] == "completed"
    assert not _active(db_session, world["ann"]) and not _active(db_session, world["bob"])


def test_lti_and_roster_padding_does_not_dilute_the_limit(client, db_session, world, kc, monkeypatch):
    """B2: the denominator is accounts a rule could deactivate. Fifty LTI Students (and roster
    rows) must not turn 2 of 6 directory accounts into 2 of 66."""
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FLOOR", "1")
    for i in range(50):
        _person(db_session, world["a"].id, f"lti{i}@lms.test", source="lti")
    for i in range(10):
        _person(db_session, world["a"].id, f"roster{i}@corp.test", source="csv_import")
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["bob"], enabled=False)
    run = _apply(client, world)
    assert run["status"] == "held"
    assert run["counts"]["held_ad_eligible"] == 6 and run["counts"]["examined"] == 66
    assert _active(db_session, world["ann"]) and _active(db_session, world["bob"])


def test_one_tenant_held_while_another_applies(client, db_session, world, kc, monkeypatch):
    """B2 + SHOULD 7: held per tenant; each tenant's message shows its own numbers only."""
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FLOOR", "1")
    monkeypatch.setenv("AD_STATUS_SYNC_MAX_FRACTION", "0.1")
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["bob"], enabled=False)
    kc.ad(world["b_kim"], enabled=False)
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
    service.run_nightly(db_session, kc, now=NIGHT)
    assert _active(db_session, world["ann"]) and _active(db_session, world["bob"])  # tenant a: held
    assert not _active(db_session, world["b_kim"])  # tenant b: 1 of 2, applied
    act_as(world["admin"])
    a_view = client.get("/ad-status-sync").json()["latest"]
    assert a_view["status"] == "held" and "would deactivate 2 of the 6 AD accounts" in a_view["error"]
    act_as(world["b_admin"])
    b_view = client.get("/ad-status-sync").json()["latest"]
    assert b_view["status"] == "completed" and b_view["error"] is None
    assert b_view["counts"]["deactivated"] == 1 and "held_ad" not in b_view["counts"]


def test_a_small_tenant_cannot_lose_its_last_admin(client, db_session, world, kc):
    """B2: never the tenant's last active admin, whatever the directory says."""
    c = real_tenant(db_session, "adsync-c")
    c_admin = _person(db_session, c.id, "chief@corp.test", role=UserRole.admin)
    c_student = _person(db_session, c.id, "cara@corp.test")
    kc.ad(c_admin, enabled=False)
    kc.ad(c_student, enabled=False)
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
    out = service.run_nightly(db_session, kc, now=NIGHT)
    assert _active(db_session, c_admin) and not _active(db_session, c_student)
    assert out.run.counts["by_tenant"][str(c.id)]["kept_last_admin"] == 1


def test_a_small_tenant_cannot_be_emptied(client, db_session, world, kc):
    """B2: the default floor is 3. A five-account tenant whose four Students all look disabled
    is held, not emptied (a floor of 10 would have let all four go)."""
    c = real_tenant(db_session, "adsync-small")
    kc.ad(_person(db_session, c.id, "boss@corp.test", role=UserRole.admin))
    students = [_person(db_session, c.id, f"s{i}@corp.test") for i in range(4)]
    for s in students:
        kc.ad(s, enabled=False)
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
    out = service.run_nightly(db_session, kc, now=NIGHT)
    assert all(_active(db_session, s) for s in students)
    assert out.run.counts["by_tenant"][str(c.id)]["held_ad"] == 1


def test_keycloak_unreachable_fails_the_run_and_changes_nothing(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    _run_now(client, world)
    kc.fail = True
    run = _run_now(client, world)
    assert run["status"] == "failed" and "Keycloak" in run["error"]
    assert _active(db_session, world["ann"])


# ── Replicas ───────────────────────────────────────────────────────────────


def test_one_nightly_run_per_night_whatever_the_replicas(db_session, world, kc, monkeypatch):
    monkeypatch.setenv("AD_STATUS_SYNC_HOUR_UTC", "2")
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
    service.run_nightly(db_session, kc, now=PREV_NIGHT)  # the first sighting
    stale_plan = service.plan(db_session, kc.snapshot(), NIGHT, None)  # replica B read before A applied
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
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
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
    run = _apply(client, world, who="b_admin")
    assert [r["email"] for r in run["results"]] == ["kim@corp.test"]
    assert not _active(db_session, world["b_kim"])
    assert _active(db_session, world["ann"])  # tenant a's account untouched
    # Tenant a's admin does not see tenant b's manual run.
    act_as(world["admin"])
    assert client.get(f"/ad-status-sync/runs/{run['id']}").status_code == 404
    assert run["id"] not in {r["id"] for r in client.get("/ad-status-sync").json()["runs"]}


def test_a_nightly_run_shows_each_tenant_only_its_own_accounts(client, db_session, world, kc):
    kc.ad(world["ann"], enabled=False)
    kc.ad(world["b_kim"], enabled=False)
    service.run_nightly(db_session, kc, now=PREV_NIGHT)
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

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "t"})
        first, mx = int(req.url.params.get("first", 0)), int(req.url.params.get("max", 100))
        if path.endswith("/users"):
            return httpx.Response(200, json=users[first:first + mx])
        return httpx.Response(404)

    reader = KeycloakDirectoryReader(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cfg={"base": "http://kc/auth", "realm": "truenorth", "client_id": "api", "client_secret": "s"},
    )
    snap = reader.snapshot()
    assert len(snap.users) == 601 and snap.users["u3"].enabled is False
    assert snap.users["u1"].federated and not snap.users["u600"].federated
    assert [u.id for u in snap.by_email("U5@corp.test")] == ["u5"]  # one index, any case
    writes = [(r.method, r.url.path) for r in seen if r.method != "GET"]
    assert writes == [("POST", "/auth/realms/truenorth/protocol/openid-connect/token")]
    assert all(r.url.path == "/auth/admin/realms/truenorth/users" for r in seen if r.method == "GET")  # no /groups


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
        reader.snapshot()


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
        return httpx.Response(404)

    reader = KeycloakDirectoryReader(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cfg={"base": "http://kc/auth", "realm": "truenorth", "client_id": "api", "client_secret": "s"},
    )
    fastapi_app.dependency_overrides[sync_router.directory_reader] = lambda: reader
    run = _apply(client, world, reader=reader)
    assert [r["email"] for r in run["results"]] == ["ann@corp.test"]
    assert {r.method for r in seen} == {"GET", "POST"}
    assert {r.url.path for r in seen if r.method == "POST"} == {"/auth/realms/truenorth/protocol/openid-connect/token"}
