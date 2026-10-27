"""Nightly AD status sync: decide who to deactivate, and do it (ADR 0014 decision 6; #290).

**The rules.** TrueNorth reads AD and never writes to it: only an AD administrator disables
or removes an AD account, and AD disables accounts itself after 90 days unused. Each run
looks at every active TrueNorth account (one tenant for "run now", every tenant at night)
against Keycloak's view of the directory (``directory``):

- an AD account (Keycloak user with a ``federationLink``) that is disabled: ``ad_disabled``;
- an AD account that is in none of the ``TN-*`` groups any more: ``ad_no_tn_group``;
- a TrueNorth account made from the directory (``source`` ``ad``/``ad_sync``) whose Keycloak
  user is gone, by id and by address: ``ad_removed``;
- a local Keycloak account (no ``federationLink``) unused for 90 days: ``local_unused``,
  **except break-glass** (``last_seen.break_glass_usernames``), which is never disabled.
  With no break-glass account configured the 90-day rule does not run at all: disabling
  local accounts without knowing which one is the recovery account could lock everyone out.

Accounts Keycloak does not know and that were not made from the directory (CSV roster rows,
LTI Students, result-transfer subjects, an admin's unclaimed row) are not Keycloak sign-ins
and are left alone. An AD account still enabled and in a TN-* group is untouched.

**Deactivating** is the existing user-deactivation path (``users.is_active = false``, as
``DELETE /admin/users/{id}`` does), claimed with a guarded update (``is_active = true``) so
any number of replicas or overlapping runs deactivate an account once. Then, in the same
transaction, each of the account's places on the roll call of an open, unfinished class is
removed with the classes' own ``remove_from_roll_call`` (#298/#299): the seat is freed in
every approved session, they leave the teams of sessions not started, their own ranges go
at the class's next range tick, and they get a CANCEL of the sessions to come. An audit row
names the account and the reason; after the commit, each class's lead and assistants (the
tenant's admins when it has none) are emailed once per class per run. Nothing is deleted:
the account row, enrolments, results, audit history and roll-call entries all stay. An
administrator re-enables the account in Users; the next run deactivates it again only if the
directory still says so.

**Dry run** (``AD_STATUS_SYNC_DRY_RUN``, on by default for the first runs): the run records
who it would deactivate and changes nothing else.

**Held**: a run that would deactivate more than ``AD_STATUS_SYNC_MAX_FRACTION`` of the
accounts it looked at (and more than ``AD_STATUS_SYNC_MAX_FLOOR``) changes nothing and is
recorded ``held``: a broken group mapper or a rebuilt realm looks exactly like everyone
leaving. An administrator who has checked can "run now" with ``override_limit``.
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
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditLog, User
from . import last_seen
from .directory import DirectoryReader, DirectorySnapshot, DirectoryUnavailableError
from .models import AdStatusSyncResult, AdStatusSyncRun, UserLastSeen

logger = logging.getLogger("truenorth.api.ad_status_sync")

UNUSED_AFTER = timedelta(days=90)  # ADR 0014 decision 6
AD_SOURCES = ("ad", "ad_sync")  # routers/ad_sync.AD_SOURCES: made from a directory identity

REASONS = {
    "ad_disabled": "their AD account is disabled",
    "ad_removed": "their AD account no longer exists",
    "ad_no_tn_group": "their AD account is no longer in any TN-* group",
    "local_unused": "their local account was not used for 90 days",
}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    group_prefix: str
    max_fraction: float
    max_floor: int
    hour_utc: int


def settings() -> Settings:
    return Settings(
        dry_run=_flag("AD_STATUS_SYNC_DRY_RUN", True),
        group_prefix=os.getenv("AD_STATUS_SYNC_GROUP_PREFIX", "TN-") or "TN-",
        max_fraction=float(os.getenv("AD_STATUS_SYNC_MAX_FRACTION", "0.25")),
        max_floor=int(os.getenv("AD_STATUS_SYNC_MAX_FLOOR", "10")),
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


def _accounts(db: Session, tenant_id: uuid.UUID | None):
    """Active, undeleted accounts (of one tenant, or all), a page at a time (keyset on id)."""
    last = None
    while True:
        q = db.query(User).filter(User.is_active == True, User.deleted_at.is_(None))  # noqa: E712
        if tenant_id is not None:
            q = q.filter(User.tenant_id == tenant_id)
        if last is not None:
            q = q.filter(User.id > last)
        page = q.order_by(User.id).limit(500).all()
        yield from page
        if len(page) < 500:
            return
        last = page[-1].id


def plan(db: Session, snap: DirectorySnapshot, now: datetime, tenant_id: uuid.UUID | None) -> Plan:
    out = Plan()
    floor = counting_from(db)
    break_glass = last_seen.break_glass_usernames()
    for u in _accounts(db, tenant_id):
        out.count(u.tenant_id, "examined")
        ku = snap.users.get(u.keycloak_id)
        if ku is None and u.source in AD_SOURCES:
            # The same directory account under a new Keycloak id (a rebuilt realm): by address,
            # only when exactly one AD user has it.
            matches = [k for k in snap.by_email(u.email) if k.federated]
            ku = matches[0] if len(matches) == 1 else None
        if ku is None and any(k.username in break_glass for k in snap.by_email(u.email) if not k.federated):
            # The break-glass account under a new Keycloak id (bootstrap_admin marks it ``ad`` and
            # re-links it at the next 80-seed): never "removed".
            out.count(u.tenant_id, "break_glass_kept")
            continue
        if ku is None:
            if u.source in AD_SOURCES:
                out.decisions.append(Decision(u, "ad_removed"))
            else:
                out.count(u.tenant_id, "not_a_keycloak_account")
            continue
        if ku.federated:
            out.count(u.tenant_id, "ad_accounts")
            if not ku.enabled:
                out.decisions.append(Decision(u, "ad_disabled"))
            elif ku.id not in snap.in_tn_group:
                out.decisions.append(Decision(u, "ad_no_tn_group"))
            continue
        out.count(u.tenant_id, "local_accounts")
        if ku.id != u.keycloak_id:
            continue  # a local account is only ever matched by its own id
        if ku.username in break_glass:
            out.count(u.tenant_id, "break_glass_kept")
            continue
        if not break_glass:
            out.count(u.tenant_id, "local_not_checked_no_break_glass")
            continue
        # tenant-safe: the account's own last-use row, keyed by its id.
        seen = db.get(UserLastSeen, u.id)
        if now - last_use(u, seen.last_seen_at if seen else None, floor) >= UNUSED_AFTER:
            out.decisions.append(Decision(u, "local_unused"))
    for d in out.decisions:
        out.count(d.user.tenant_id, d.reason)
    return out


def over_limit(cfg: Settings, plan_: Plan) -> bool:
    examined = plan_.plain_counts()["total"].get("examined", 0)
    limit = max(cfg.max_floor, math.floor(cfg.max_fraction * examined))
    return len(plan_.decisions) > limit


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
            "account (or its TN-* group), then an administrator can re-enable it in Users and you can add them "
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
    """Run a claimed run to the end. Commits per account, so one failure undoes nothing else."""
    now = now or datetime.now(UTC)
    cfg = settings()
    out = Outcome(run=run)
    try:
        snap = reader.snapshot(cfg.group_prefix)
        if not snap.groups_found and any(u.federated for u in snap.users.values()):
            raise DirectoryUnavailableError(
                f"Keycloak has AD users but no group named {cfg.group_prefix}*: the AD group mapper is not "
                "bringing the TN-* groups, so every AD account would look removed from them"
            )
    except DirectoryUnavailableError as exc:
        run.status, run.error, run.finished_at = "failed", str(exc)[:1000], now
        _audit_run(db, run)
        db.commit()
        logger.error("AD status sync %s failed: %s", run.id, exc)
        return out

    p = plan(db, snap, now, run.tenant_id)
    counts = p.plain_counts()
    if over_limit(cfg, p) and not override_limit:
        run.status = "held"
        run.error = (f"would deactivate {len(p.decisions)} of {counts['total'].get('examined', 0)} accounts, over "
                     "the limit (AD_STATUS_SYNC_MAX_FRACTION): nothing was changed. Check the directory, then "
                     "run now with the limit overridden.")[:1000]
    if run.dry_run or run.status == "held":
        for d in p.decisions:
            _result(db, run, d.user, d.reason, False, 0, now)
        run.status = "completed" if run.status == "running" else run.status
    else:
        notices: dict = {}
        applied: dict[str, int] = defaultdict(int)
        for d in p.decisions:
            uid, tid = d.user.id, d.user.tenant_id
            try:
                won, invites, left = deactivate(db, run, d.user, d.reason, now)
                db.commit()
            except Exception:  # noqa: BLE001 -- the next account, and the next run, go on
                db.rollback()
                logger.exception("AD status sync %s: deactivating user %s failed", run.id, uid)
                mine = counts["by_tenant"].setdefault(str(tid), {})
                mine["errors"] = mine.get("errors", 0) + 1
                counts["total"]["errors"] = counts["total"].get("errors", 0) + 1
                continue
            if won:  # only now: a commit that failed sends no CANCEL and no notice
                applied[str(tid)] += 1
                out.invites += invites
                for cls, person in left:
                    notices.setdefault(cls.id, (cls, []))[1].append(person)
        for tid, n in applied.items():
            if n:
                counts["by_tenant"].setdefault(tid, {})["deactivated"] = n
        counts["total"]["deactivated"] = sum(applied.values())
        out.mails = notice_mails(db, notices)
        run.status = "completed"
    run.counts = counts
    run.finished_at = datetime.now(UTC)
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
