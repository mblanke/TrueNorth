"""Keep a range's credentials in step with its template, and decide who may read them.

Rows (``RangeCredential``) are written:

* when a range is created, and when a build of it is accepted (``range_ops.accept``, the
  allocation step every build goes through: the ranges router, batch provisioning,
  exercises and the scheduler). ``sync`` makes the rows match the template's
  ``accounts:``. A generated password is made once per range and kept across rebuilds; a
  literal one follows the template; an account the template no longer declares goes.
* when the template changes, for the ranges of it that are not built (``created``,
  ``destroyed``): the next build applies what the template now says. This runs after the
  response (``regenerate_for_template``), for every tenant's ranges of that template.

Every sync marks the rows not ``applied``; the worker marks applied what a build reports it
set (worker/range_accounts.py). A generated password is issued by ``_issue``, the one place
a per-account rotation would call.

Reads (ADR 0012 §13), all tenant-scoped (another tenant's range is 404):

==================================  ==============================================
admin, instructor, range_ops         every account (``range:credentials_read``)
Student                              ``student_visible`` accounts the last build set
                                     (``applied``), of a range they take part in: an
                                     exercise running or paused on it whose booked
                                     course they are rostered on. Otherwise 404. Lab
                                     sessions are not covered yet (their ranges hold no
                                     credentials; their VMs keep random passwords).
observer and anyone else             403
==================================  ==============================================

The Student rule is the strict one of detection submission (app/exercise_participants.py,
ADR 0005), not the telemetry one: an exercise with no booked course seats no Student here.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import closing

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, Exercise, ExerciseState, Range, RangeState, Template, UserRole
from ..rbac import Permission, user_has_permission
from . import accounts as account_rules
from .models import RangeCredential

logger = logging.getLogger("truenorth.range_credentials")

# A range in these states has no VMs a change would leave behind: its next build applies it.
UNBUILT = (RangeState.created, RangeState.destroyed)
ACTIVE_EXERCISE = (ExerciseState.running, ExerciseState.paused)

ALL = "all"
STUDENT = "student"


class InvalidAccountsError(ValueError):
    """The range's template declares accounts that do not validate."""


# ── Writes ──────────────────────────────────────────────────────────────
def template_of(db: Session, rng: Range) -> Template | None:
    # tenant-safe: the range's own template (FK); a public template may be another tenant's.
    return db.get(Template, rng.template_id) if rng.template_id else None


def declared(db: Session, rng: Range) -> list[account_rules.AccountSpec]:
    """The accounts the range's template declares. ``InvalidAccountsError`` if they do not validate."""
    tmpl = template_of(db, rng)
    specs, errors = account_rules.parse_yaml(tmpl.yaml if tmpl else "")
    if errors:
        detail = "; ".join(f"{e['path']}: {e['message']}" for e in errors[:5])
        raise InvalidAccountsError(f"the range's template declares invalid accounts: {detail}")
    return specs


def sync(db: Session, rng: Range) -> int:
    """Make the range's rows match its template. Does not commit. Returns the row count.

    Call only while nothing is built from the current rows (create, build acceptance,
    an unbuilt range's template changing): a literal password changed here is what the
    next build sets, so changing it under running VMs would make the record lie."""
    if rng.tenant_id is None:
        return 0
    specs = declared(db, rng)
    rows = {(r.machine, r.username.casefold()): r for r in _rows(db, rng)}
    for spec in specs:
        row = rows.pop(spec.key, None)
        if row is None:
            row = RangeCredential(
                tenant_id=rng.tenant_id, range_id=rng.id, machine=spec.machine, username=spec.username,
                password=spec.password, generated=False, student_visible=spec.student_visible, role=spec.role,
                notes=spec.notes, applied=False,
            )
            if spec.generate:
                _issue(row)
            db.add(row)
            continue
        row.username, row.student_visible, row.role, row.notes, row.applied = (
            spec.username, spec.student_visible, spec.role, spec.notes, False)
        if spec.generate:
            if not row.generated:  # was a literal: make one up now, once
                _issue(row)
        elif row.generated or row.password != spec.password:
            row.password, row.generated = spec.password, False
    for gone in rows.values():
        db.delete(gone)
    db.flush()
    return len(specs)


def _issue(row: RangeCredential) -> None:
    """Give ``row`` a newly generated password. Once per range today; rotating one account
    (ADR 0012, M1 pending) is this plus ``applied = False`` and a rebuild or in-guest change."""
    row.password, row.generated = account_rules.generate_password(row.username), True


def forget(db: Session, range_id: uuid.UUID) -> None:
    """Drop a range's rows (the range record is being deleted). Does not commit."""
    # tenant-safe: callers pass a range they resolved in the caller's tenant.
    db.query(RangeCredential).filter(RangeCredential.range_id == range_id).delete(synchronize_session=False)


def _rows(db: Session, rng: Range) -> list[RangeCredential]:
    return (
        db.query(RangeCredential)
        .filter(RangeCredential.range_id == rng.id, RangeCredential.tenant_id == rng.tenant_id)
        .order_by(RangeCredential.machine, RangeCredential.username)
        .all()
    )


# ── Lifecycle hooks (the routers and range_ops call these; none commits) ────
def on_range_created(db: Session, rng: Range, actor: CurrentUser | None) -> None:
    """422 when the template's accounts do not validate: nothing could ever build it."""
    try:
        sync(db, rng)
    except InvalidAccountsError as exc:
        raise HTTPException(422, str(exc)) from None
    _document(db, rng, actor)


def on_range_updated(db: Session, rng: Range, actor: CurrentUser | None) -> None:
    _document(db, rng, actor)


def on_build_accepted(db: Session, rng: Range) -> None:
    """The allocation step: generated passwords are made here, once per range."""
    try:
        sync(db, rng)
    except InvalidAccountsError as exc:
        raise HTTPException(422, f"Cannot provision: {exc}") from None


def on_build_succeeded(db: Session, rng: Range) -> None:
    """The range is built: its page gains the addresses the worker recorded. Never raises."""
    _document(db, rng, None)


def regenerate_for_template(template_id: uuid.UUID) -> None:
    """After a template change (run after the response, ``BackgroundTasks``): re-sync every
    unbuilt range of it and regenerate every range's page, in every tenant (each range's own
    tenant, its own page). Batched, one commit per range; never raises."""
    try:
        _regenerate_for_template(template_id)
    except Exception as exc:  # noqa: BLE001 — after the response there is nobody to tell but the log
        logger.warning("template %s: its ranges were not refreshed (%s)", template_id, type(exc).__name__)


def _regenerate_for_template(template_id: uuid.UUID) -> None:
    with session_factory() as db:
        last: uuid.UUID | None = None
        while True:
            q = db.query(Range).filter(Range.template_id == template_id, Range.deleted_at.is_(None))
            if last is not None:
                q = q.filter(Range.id > last)
            batch = q.order_by(Range.id).limit(BATCH).all()
            if not batch:
                return
            for rng in batch:
                last = rng.id
                try:
                    if rng.state in UNBUILT:
                        sync(db, rng)
                    _document(db, rng, None)
                    db.commit()
                except InvalidAccountsError as exc:  # a template stored before validation existed
                    db.rollback()
                    logger.warning("range %s keeps its credentials: %s", rng.id, exc)
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning("range %s: credentials/page not refreshed (%s)", rng.id, type(exc).__name__)


BATCH = 200


def _default_factory():
    """A session of its own (the request's is closed by the time background work runs)."""
    from ..db import SessionLocal

    return closing(SessionLocal())


# A context manager yielding a session; tests point it at theirs.
session_factory = _default_factory


def on_range_deleted(db: Session, rng: Range) -> None:
    from . import wiki_doc

    forget(db, rng.id)
    wiki_doc.retire(db, rng)


def _document(db: Session, rng: Range, actor: CurrentUser | None) -> None:
    """Regenerate the range's wiki page in a savepoint. Never raises: documentation must not
    fail the range change or operation outcome it follows (the next change regenerates it)."""
    from . import wiki_doc

    if rng.tenant_id is None:
        return
    try:
        with db.begin_nested():
            wiki_doc.upsert(db, rng, _rows(db, rng), actor_id(actor))
    except Exception as exc:  # noqa: BLE001
        # The type only: a database error's text carries the statement and its parameters.
        logger.warning("could not regenerate the wiki page of range %s (%s)", rng.id, type(exc).__name__)


# ── Reads ───────────────────────────────────────────────────────────────
SYSTEM_ACTOR = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


def actor_id(user: CurrentUser | None) -> uuid.UUID:
    """The user's id, or a fixed system id (the scheduler, the reconcile path)."""
    try:
        return uuid.UUID(str(user.id)) if user is not None else SYSTEM_ACTOR
    except ValueError:
        return SYSTEM_ACTOR


def access_scope(user: CurrentUser) -> str:
    """``all`` or ``student``; 403 for any other role (observers: ADR 0012 §13)."""
    if user_has_permission(user, Permission.RANGE_CREDENTIALS_READ):
        return ALL
    if user.role == UserRole.student:
        return STUDENT
    raise HTTPException(403, "Your role does not see range credentials")


def takes_part(db: Session, user: CurrentUser, rng: Range) -> bool:
    """Whether a Student takes part in ``rng`` (module docstring). ``rng`` is in their tenant."""
    from ..enrollment import rostered_course_ids
    from ..exercise_participants import roster_courses
    try:
        uid = uuid.UUID(str(user.id))
    except ValueError:
        return False
    exercises = (
        db.query(Exercise)
        .filter(
            Exercise.range_id == rng.id,
            Exercise.tenant_id == rng.tenant_id,
            Exercise.state.in_(ACTIVE_EXERCISE),
            Exercise.deleted_at.is_(None),
        )
        .all()
    )
    if not exercises:
        return False
    mine = set(rostered_course_ids(db, uid))
    return bool(mine) and any(roster_courses(db, ex) & mine for ex in exercises)


def readable_range(db: Session, range_id: uuid.UUID, user: CurrentUser, scope: str) -> Range:
    """The caller's range, or 404: another tenant's, a deleted one, a Student's lab to staff
    without infrastructure rights (as routers/ranges.py), one a Student does not take part in."""
    rng = (
        db.query(Range)
        .filter(Range.id == range_id, Range.tenant_id == uuid.UUID(str(user.tenant_id)), Range.deleted_at.is_(None))
        .first()
    )
    if rng is None:
        raise HTTPException(404, "Range not found")
    if scope == ALL:
        if not user_has_permission(user, Permission.INFRA_READ):
            from ..lab_sessions.service import lab_range_ids

            if rng.id in lab_range_ids(db, [rng.id]):
                raise HTTPException(404, "Range not found")
        return rng
    if not takes_part(db, user, rng):
        raise HTTPException(404, "Range not found")
    return rng


def visible_accounts(db: Session, rng: Range, scope: str) -> list[RangeCredential]:
    rows = _rows(db, rng)
    return rows if scope == ALL else [r for r in rows if r.student_visible and r.applied]


def audit_read(db: Session, user: CurrentUser, rng: Range, scope: str, rows: list[RangeCredential]) -> None:
    """One audit row per read: who, which range, how many accounts. Never a password."""
    machines = sorted({r.machine for r in rows})
    db.add(AuditLog(
        user_id=_audit_user(user),
        tenant_id=rng.tenant_id,
        action="range_credentials_read",
        resource_type="range",
        resource_id=str(rng.id),
        detail=f"scope={scope}; accounts={len(rows)}; machines={','.join(machines)[:900]}",
    ))


def _audit_user(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except ValueError:
        return None
