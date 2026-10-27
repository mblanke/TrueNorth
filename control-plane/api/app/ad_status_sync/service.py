"""Nightly AD status sync: decide who to deactivate, and do it (ADR 0014 decision 6; #290).

**The rules.** TrueNorth reads AD and never writes to it: only an AD administrator disables
or removes an AD account, and AD disables accounts itself after 90 days unused. Each run
looks at every active TrueNorth account (one tenant for "run now", every tenant at night)
against Keycloak's view of the directory (``directory``):

- an AD account (its Keycloak user has a ``federationLink``) that is disabled: ``ad_disabled``;
- an account known to be an AD account (``users.ad_object_guid``, set from the directory's
  claims) whose Keycloak user is gone, by id and by address: ``ad_removed``. ``source`` alone
  proves nothing: registration marks every account ``ad``, local ones included;
- a local Keycloak account (no ``federationLink``) unused for 90 days: ``local_unused``,
  **except break-glass** (``last_seen.break_glass_usernames``, and only a name that is a
  *local* Keycloak user), which is never disabled. With no break-glass account the 90-day
  rule does not run: disabling local accounts without knowing which one is the recovery
  account could lock everyone out.

TN-* group membership is not a rule: roll-call Students are admitted without any TN-* group
(classes/onboarding.py), and ADR 0014 section 6 names only a disabled or removed AD account.
Result-transfer subjects (``source = subject``) are excluded from every rule and from every
count of eligible accounts. Accounts Keycloak does not know and that are not known AD
accounts (CSV roster rows, LTI Students, an admin's unclaimed row) are left alone.

**Deactivating** is the existing user-deactivation path (``users.is_active = false``, as
``DELETE /admin/users/{id}`` does), claimed with a guarded update (``is_active = true``) so
any number of replicas or overlapping runs deactivate an account once. Then, in the same
transaction, each of the account's places on the roll call of an open, unfinished class is
removed with the classes' own ``remove_from_roll_call`` (#298/#299): the seat is freed in
every approved session, they leave the teams of sessions not started, their own ranges go
at the class's next range tick, and they get a CANCEL of the sessions to come. An audit row
names the account and the reason; after the commit, each class's lead and assistants (the
tenant's admins when it has none) are emailed once per class per run. Nothing is deleted.
An administrator re-enables the account in Users.

**Safety**, each checked per tenant:

- *Dry run* (``AD_STATUS_SYNC_DRY_RUN``, on by default): record, change nothing.
- *Confirmed*: an account is deactivated only when a good run covering its tenant (dry, held
  or applied) that finished ``SIGHTING_MIN``-``SIGHTING_MAX`` (12-36 h) before this one found
  the same account for the same reason: in practice, the previous night. A dry "run now" then
  a real one cannot confirm each other, a months-old finding confirms nothing, and a user
  missing from one page of Keycloak's answer does not lose their account. So turning dry run
  off acts on the next night, using the dry-run nights' findings.
- *Held*: per tenant and rule family. The AD rules (``ad_disabled``, ``ad_removed``) are
  judged against the tenant's AD accounts, the 90-day rule against its local accounts under
  that rule, so neither dilutes the other (nor do roster rows or LTI Students). A family
  that would deactivate more than max(``AD_STATUS_SYNC_MAX_FLOOR`` (3),
  ``AD_STATUS_SYNC_MAX_FRACTION`` (0.25) of its accounts) changes nothing this run; other
  families and tenants go on. "Run now" with ``override_limit`` releases the caller's tenant.
- *Last administrator*: never the tenant's last active admin (``is_last_admin`` locks the
  tenant's admin rows first, so two deactivations at once cannot both pass).
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditLog, User, UserRole
from . import last_seen
from .directory import DirectoryReader, DirectorySnapshot, DirectoryUnavailableError
from .models import AdStatusSyncResult, AdStatusSyncRun, UserLastSeen

logger = logging.getLogger("truenorth.api.ad_status_sync")

UNUSED_AFTER = timedelta(days=90)  # ADR 0014 decision 6
AD_SOURCES = ("ad", "ad_sync")  # routers/ad_sync.AD_SOURCES: made from a directory identity
SUBJECT_SOURCE = "subject"  # result_transfer/pseudonymous.SUBJECT_SOURCE: never judged here
# A finding is confirmed only by the same finding from a run that finished this long before.
SIGHTING_MIN = timedelta(hours=12)
SIGHTING_MAX = timedelta(hours=36)

REASONS = {
    "ad_disabled": "their AD account is disabled",
    "ad_removed": "their AD account no longer exists",
    "local_unused": "their local account was not used for 90 days",
}
# Rule families, each held against its own accounts only.
FAMILIES = {"ad": ("ad_disabled", "ad_removed"), "local": ("local_unused",)}
FAMILY_OF = {r: f for f, rs in FAMILIES.items() for r in rs}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    max_fraction: float
    max_floor: int
    hour_utc: int


def settings() -> Settings:
    return Settings(
        dry_run=_flag("AD_STATUS_SYNC_DRY_RUN", True),
        max_fraction=float(os.getenv("AD_STATUS_SYNC_MAX_FRACTION", "0.25")),
        max_floor=int(os.getenv("AD_STATUS_SYNC_MAX_FLOOR", "3")),
        hour_utc=int(os.getenv("AD_STATUS_SYNC_HOUR_UTC", "2")) % 24,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ── Deciding ───────────────────────────────────────────────────────────────


@dataclass
class Decision:
    user: User
    reason: str


@dataclass
class Plan:
    decisions: list[Decision] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))

    def count(self, tenant_id: uuid.UUID, key: str) -> None:
        self.counts[str(tenant_id)][key] += 1

    def plain_counts(self) -> dict:
        by_tenant = {t: dict(c) for t, c in self.counts.items()}
        total: dict[str, int] = defaultdict(int)
        for c in by_tenant.values():
            for k, v in c.items():
                total[k] += v
        return {"by_tenant": by_tenant, "total": dict(total)}


def counting_from(db: Session) -> datetime | None:
    """When TrueNorth started keeping ``user_last_seen``: the first sync run (any, dry or not)."""
    return _aware(db.query(func.min(AdStatusSyncRun.started_at)).scalar())


def last_use(user: User, seen: datetime | None, floor: datetime | None) -> datetime:
    """The latest of: last seen, created, and when the record started (never "unused" for the
    time before TrueNorth kept it)."""
    times = [t for t in (_aware(seen), _aware(user.created_at), floor) if t is not None]
    return max(times) if times else datetime.now(UTC)


PAGE = 500


def _account_pages(db: Session, tenant_id: uuid.UUID | None):
    """Active, undeleted accounts (of one tenant, or all), a page at a time (keyset on id)."""
    last = None
    while True:
        q = db.query(User).filter(User.is_active == True, User.deleted_at.is_(None))  # noqa: E712
        if tenant_id is not None:
            q = q.filter(User.tenant_id == tenant_id)
        if last is not None:
            q = q.filter(User.id > last)
        page = q.order_by(User.id).limit(PAGE).all()
        if page:
            yield page
        if len(page) < PAGE:
            return
        last = page[-1].id


def effective_break_glass(snap: DirectorySnapshot) -> set[str]:
    """The configured break-glass names that are LOCAL Keycloak users. A name that is only an AD
    account (the local administrator turned off) is no break-glass: the 90-day rule stays off."""
    local = {k.username for k in snap.users.values() if not k.federated}
    return last_seen.break_glass_usernames() & local


def configured_break_glass_missing(snap: DirectorySnapshot) -> bool:
    """A break-glass name is configured but none is a local Keycloak user: the 90-day rule is off,
    and the panel says so rather than switching it off silently."""
    return bool(last_seen.break_glass_usernames()) and not effective_break_glass(snap)


def plan(db: Session, snap: DirectorySnapshot, now: datetime, tenant_id: uuid.UUID | None) -> Plan:
    out = Plan()
    floor = counting_from(db)
    break_glass = effective_break_glass(snap)
    for page in _account_pages(db, tenant_id):
        # One query per page, not per account.
        seen = dict(
            db.query(UserLastSeen.user_id, UserLastSeen.last_seen_at)
            .filter(UserLastSeen.user_id.in_([u.id for u in page]))
            .all()
        )
        for u in page:
            _decide(out, u, snap, now, floor, break_glass, seen.get(u.id))
    return out


def _decide(out: Plan, u: User, snap: DirectorySnapshot, now: datetime, floor: datetime | None,
            break_glass: set[str], seen: datetime | None) -> None:
    t = u.tenant_id
    out.count(t, "examined")
    if u.source == SUBJECT_SOURCE:
        # A result-transfer subject (result_transfer/pseudonymous.py) is a pseudonymous record
        # holder, not a sign-in: no rule applies and it is not eligible.
        out.count(t, "result_transfer_subject")
        return

    def decide(reason: str) -> None:
        out.decisions.append(Decision(u, reason))
        out.count(t, reason)

    ku = snap.users.get(u.keycloak_id)
    if ku is None and u.ad_object_guid:
        # The same directory account under a new Keycloak id (a rebuilt realm): by address,
        # only when exactly one AD user has it.
        matches = [k for k in snap.by_email(u.email) if k.federated]
        ku = matches[0] if len(matches) == 1 else None
    if ku is None and any(k.username in break_glass for k in snap.by_email(u.email) if not k.federated):
        out.count(t, "break_glass_kept")  # break-glass under a new Keycloak id, until 80-seed re-links it
        return
    if ku is None:
        if u.ad_object_guid:  # a known AD account Keycloak no longer has
            out.count(t, "eligible_ad")
            decide("ad_removed")
        else:  # roster row, LTI Student, a local account after a realm rebuild: not ours to judge
            out.count(t, "not_a_keycloak_account")
        return
    if ku.federated:
        out.count(t, "ad_accounts")
        out.count(t, "eligible_ad")
        if not ku.enabled:
            decide("ad_disabled")
        return
    out.count(t, "local_accounts")
    if ku.id != u.keycloak_id:
        return  # a local account is only ever matched by its own id
    if ku.username in break_glass:
        out.count(t, "break_glass_kept")
        return
    if not break_glass:
        out.count(t, "local_not_checked_no_break_glass")
        return
    out.count(t, "eligible_local")
    if now - last_use(u, seen, floor) >= UNUSED_AFTER:
        decide("local_unused")


def held_families(cfg: Settings, p: Plan) -> dict[tuple[str, str], tuple[int, int]]:
    """(tenant, family) -> (would deactivate, eligible), for each rule family over its limit in
    a tenant. Each family is judged against its own accounts only: AD rules against AD accounts,
    the 90-day rule against local accounts, so neither dilutes the other."""
    out = {}
    for tid, c in p.counts.items():
        for family, reasons in FAMILIES.items():
            n = sum(c.get(r, 0) for r in reasons)
            eligible = c.get(f"eligible_{family}", 0)
            if n and n > max(cfg.max_floor, math.floor(cfg.max_fraction * eligible)):
                out[(tid, family)] = (n, eligible)
    return out


def held_message(family: str, would: int, eligible: int) -> str:
    """For one tenant and rule family, from that tenant's own counts only."""
    what = {"ad": "AD accounts", "local": "local accounts under the 90-day rule"}[family]
    return (f"would deactivate {would} of the {eligible} {what} here, over the limit "
            "(AD_STATUS_SYNC_MAX_FRACTION, AD_STATUS_SYNC_MAX_FLOOR): none of them was changed in this tenant. "
            "Check the directory, then run now with the limit overridden.")


def earlier_sightings(db: Session, run: AdStatusSyncRun, tenant_ids: set[str]) -> dict[str, set[tuple]]:
    """Per tenant, the (account, reason) pairs that confirm this run's findings: found by a good
    run (dry, held or applied) covering that tenant that finished between ``SIGHTING_MIN`` and
    ``SIGHTING_MAX`` before this one started. Not a moment ago (a dry "run now" then a real one
    must not confirm each other), not months ago, and for the same reason."""
    start = _aware(run.started_at)
    out: dict[str, set[tuple]] = {}
    for tid in tenant_ids:
        t = uuid.UUID(tid)
        rows = (
            db.query(AdStatusSyncResult.user_id, AdStatusSyncResult.reason)
            .join(AdStatusSyncRun, AdStatusSyncRun.id == AdStatusSyncResult.run_id)
            .filter(
                AdStatusSyncResult.tenant_id == t,
                AdStatusSyncRun.id != run.id,
                AdStatusSyncRun.status == "completed",
                AdStatusSyncRun.finished_at >= start - SIGHTING_MAX,
                AdStatusSyncRun.finished_at <= start - SIGHTING_MIN,
                or_(AdStatusSyncRun.tenant_id.is_(None), AdStatusSyncRun.tenant_id == t),
            )
            .all()
        )
        out[tid] = {(uid, reason) for uid, reason in rows}
    return out


def is_last_admin(db: Session, u: User) -> bool:
    """``u`` is the only active admin of its tenant. Locks the tenant's active admin rows first
    (``FOR UPDATE``; PostgreSQL), so two transactions deactivating two admins at once cannot
    both see the other still active: the second waits, then counts again without the first."""
    if u.role != UserRole.admin:
        return False
    admins = (
        db.query(User.id)
        .filter(User.tenant_id == u.tenant_id, User.role == UserRole.admin, User.is_active == True,  # noqa: E712
                User.deleted_at.is_(None))
        .with_for_update()
        .all()
    )
    return not any(uid != u.id for (uid,) in admins)


# ── Applying ───────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    run: AdStatusSyncRun
    mails: list = field(default_factory=list)
    invites: list = field(default_factory=list)


def _result(db: Session, run: AdStatusSyncRun, u: User, reason: str, applied: bool, seats: int, now: datetime):
    db.add(AdStatusSyncResult(
        run_id=run.id, tenant_id=u.tenant_id, user_id=u.id, reason=reason, applied=applied, seats_freed=seats,
        created_at=now,
    ))


def _roll_call_places(db: Session, u: User, now: datetime):
    """The account's places on the roll call of open, unfinished classes of its own tenant."""
    from ..classes.models import Class, ClassStatus, RollCallEntry, RollCallStatus

    return (
        db.query(RollCallEntry, Class)
        .join(Class, Class.id == RollCallEntry.class_id)
        .filter(
            RollCallEntry.user_id == u.id,
            RollCallEntry.tenant_id == u.tenant_id,
            Class.tenant_id == u.tenant_id,
            Class.status == ClassStatus.open.value,
            Class.end_date >= (now - timedelta(days=1)).date(),
            RollCallEntry.status.notin_((RollCallStatus.removed.value, RollCallStatus.forfeited.value)),
        )
        .order_by(Class.id)
        .all()
    )


def deactivate(db: Session, run: AdStatusSyncRun, u: User, reason: str, now: datetime) -> tuple[bool, list, list]:
    """Deactivate ``u`` (guarded), free their seats, audit. Does not commit. Returns (won,
    CANCEL invites, the classes whose roll call they left with the person, for the emails)."""
    from ..classes import service as classes
    from ..scheduler import service as scheduler

    won = (
        db.query(User)
        .filter(User.id == u.id, User.is_active == True, User.deleted_at.is_(None))  # noqa: E712
        .update({User.is_active: False}, synchronize_session=False)
    ) == 1
    if not won:  # another replica or run deactivated it, or an admin did meanwhile
        _result(db, run, u, reason, False, 0, now)
        return False, [], []
    invites: list = []
    left: list = []
    seats = 0
    classes_left = []
    for entry, cls in _roll_call_places(db, u, now):
        try:
            with db.begin_nested():
                freed = classes.remove_from_roll_call(db, cls, entry, None)
        except HTTPException:
            continue  # its status changed at the same moment (a forfeit, a lead's removal): already off
        seats += 1 if freed else 0
        invites += scheduler.attendee_invites(db, freed, [entry.email], "CANCEL")
        classes_left.append(cls.name)
        left.append((cls, (u.display_name or u.email, u.email, reason)))
    detail = f"deactivated by the AD status sync (run {run.id}): {REASONS[reason]}"
    if classes_left:
        detail += f"; taken off the roll call of {', '.join(classes_left)}"
    db.add(AuditLog(
        user_id=run.triggered_by, tenant_id=u.tenant_id, action="ad_status_deactivate", resource_type="user",
        resource_id=str(u.id), detail=detail[:2000],
    ))
    _result(db, run, u, reason, True, seats, now)
    return True, invites, left


def notice_mails(db: Session, notices: dict) -> list:
    """One email per instructor per class: who was deactivated, why, and that nothing was deleted."""
    from ..classes import forfeit
    from ..classes import service as classes

    out = []
    for cls, people in notices.values():
        lines = "\n".join(f"  - {name} <{email}>: {REASONS[reason]}" for name, email, reason in people)
        body = (
            f"The nightly directory check deactivated {len(people)} TrueNorth account(s) on the roll call of "
            f"{cls.name}:\n\n{lines}\n\n"
            "They were taken off the roll call, their seat in the class's sessions was freed and their calendar "
            "invites withdrawn. Nothing was deleted: their results, enrolments and records are kept.\n\n"
            "TrueNorth never changes Active Directory. If this is wrong, ask your AD administrator to restore the "
            "account, then an administrator can re-enable it in Users and you can add them "
            "back to the roll call.\n"
        )
        subject = f"{classes._subject_safe(cls.name)}: {len(people)} account(s) deactivated by the directory check"
        to = forfeit.instructors(db, cls) or forfeit.tenant_admins(db, cls)
        out += [classes.Mail(to=p.email, subject=subject, body=body) for p in to if p.email]
    return out


# ── A run ──────────────────────────────────────────────────────────────────


def claim(db: Session, slot: str, trigger: str, *, tenant_id: uuid.UUID | None, actor_id: uuid.UUID | None,
          dry_run: bool, now: datetime) -> AdStatusSyncRun | None:
    """Insert the run's row; None when that slot is taken (another replica runs it). Commits."""
    run = AdStatusSyncRun(id=uuid.uuid4(), slot=slot, trigger=trigger, tenant_id=tenant_id, triggered_by=actor_id,
                          dry_run=dry_run, status="running", started_at=now, counts={})
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError:
        return None
    db.commit()
    return run


def execute(db: Session, run: AdStatusSyncRun, reader: DirectoryReader, *, override_limit: bool = False,
            now: datetime | None = None) -> Outcome:
    """Run a claimed run to the end. Commits per account, so one failure undoes nothing else.

    ``override_limit`` releases a held tenant; only "run now" passes it, for its own tenant."""
    now = now or datetime.now(UTC)
    wall = datetime.now(UTC)  # finished_at is ``now`` plus the time the run took
    cfg = settings()
    out = Outcome(run=run)
    try:
        snap = reader.snapshot()
    except DirectoryUnavailableError as exc:
        run.status, run.error, run.finished_at = "failed", str(exc)[:1000], now
        _audit_run(db, run)
        db.commit()
        logger.error("AD status sync %s failed: %s", run.id, exc)
        return out

    p = plan(db, snap, now, run.tenant_id)
    counts = p.plain_counts()
    held = {} if override_limit else held_families(cfg, p)
    for (tid, family), (would, eligible) in held.items():
        counts["by_tenant"][tid].update({f"held_{family}": 1, f"held_{family}_would_deactivate": would,
                                         f"held_{family}_eligible": eligible})
    if configured_break_glass_missing(snap):
        counts["break_glass_not_found"] = 1
    confirmed = {} if run.dry_run else earlier_sightings(db, run, {str(d.user.tenant_id) for d in p.decisions})
    notices: dict = {}
    applied: dict[str, int] = defaultdict(int)

    def bump(tid: str, key: str) -> None:
        mine = counts["by_tenant"].setdefault(tid, {})
        mine[key] = mine.get(key, 0) + 1
        counts["total"][key] = counts["total"].get(key, 0) + 1

    for d in p.decisions:
        uid, tid = d.user.id, str(d.user.tenant_id)
        if run.dry_run or (tid, FAMILY_OF[d.reason]) in held:
            _result(db, run, d.user, d.reason, False, 0, now)
            continue
        if (uid, d.reason) not in confirmed.get(tid, set()):
            _result(db, run, d.user, d.reason, False, 0, now)  # not confirmed; tomorrow night may
            bump(tid, "awaiting_confirmation")
            continue
        if is_last_admin(db, d.user):
            _result(db, run, d.user, d.reason, False, 0, now)
            bump(tid, "kept_last_admin")
            continue
        try:
            won, invites, left = deactivate(db, run, d.user, d.reason, now)
            db.commit()
        except Exception:  # noqa: BLE001 -- the next account, and the next run, go on
            db.rollback()
            logger.exception("AD status sync %s: deactivating user %s failed", run.id, uid)
            bump(tid, "errors")
            continue
        if won:  # only now: a commit that failed sends no CANCEL and no notice
            applied[tid] += 1
            out.invites += invites
            for cls, person in left:
                notices.setdefault(cls.id, (cls, []))[1].append(person)
    for tid, n in applied.items():
        counts["by_tenant"].setdefault(tid, {})["deactivated"] = n
    counts["total"]["deactivated"] = sum(applied.values())
    out.mails = notice_mails(db, notices)
    run.status = "completed"
    run.counts = counts
    run.finished_at = now + (datetime.now(UTC) - wall)
    _audit_run(db, run)
    db.commit()
    logger.info("AD status sync %s %s (dry run %s): %s", run.id, run.status, run.dry_run, counts["total"])
    return out


def _audit_run(db: Session, run: AdStatusSyncRun) -> None:
    """One row per run, in the tenant it covered (none for the nightly run of every tenant)."""
    total = (run.counts or {}).get("total", {})
    db.add(AuditLog(
        user_id=run.triggered_by, tenant_id=run.tenant_id, action="ad_status_sync_run", resource_type="ad_status_sync",
        resource_id=str(run.id),
        detail=(f"{run.trigger} run, {'dry run' if run.dry_run else 'applied'}: {run.status}; "
                + ", ".join(f"{k} {v}" for k, v in sorted(total.items()))
                + (f"; {run.error}" if run.error else ""))[:2000],
    ))


def run_nightly(db: Session, reader: DirectoryReader, now: datetime | None = None) -> Outcome | None:
    """The night's run, once across all replicas; None when it is not yet time or already claimed."""
    now = now or datetime.now(UTC)
    cfg = settings()
    if now.hour < cfg.hour_utc:
        return None
    slot = f"nightly:{now.date().isoformat()}"
    if db.query(AdStatusSyncRun.id).filter(AdStatusSyncRun.slot == slot).first() is not None:
        return None
    run = claim(db, slot, "nightly", tenant_id=None, actor_id=None, dry_run=cfg.dry_run, now=now)
    if run is None:
        return None
    return execute(db, run, reader, now=now)


def run_now(db: Session, reader: DirectoryReader, *, tenant_id: uuid.UUID, actor_id: uuid.UUID, dry_run: bool,
            override_limit: bool, now: datetime | None = None) -> Outcome:
    """An administrator's run of their own tenant."""
    now = now or datetime.now(UTC)
    run = claim(db, f"manual:{uuid.uuid4()}", "manual", tenant_id=tenant_id, actor_id=actor_id, dry_run=dry_run,
                now=now)
    assert run is not None  # a fresh uuid slot
    return execute(db, run, reader, override_limit=override_limit, now=now)
