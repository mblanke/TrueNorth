"""Keep a range's credentials in step with its template, rotate them, and decide who reads them.

Rows (``RangeCredential``) are written:

* when a range is created, and when a build of it is accepted (``range_ops.accept``, the
  allocation step every build goes through: the ranges router, batch provisioning,
  exercises and the scheduler), and when a lab session builds its range. ``sync`` makes
  the rows match the template's ``accounts:`` plus the implicit staff logins
  (``accounts.implicit_staff_logins``: the built-in Administrator, the Linux build user,
  ``soadmin``), so no machine keeps its golden template's build password (ADR 0013 §3).
  Every password is generated per range, except a scenario account's typed-in one, which
  follows the template. An account the template no longer declares goes.
* when the template changes, for the ranges of it that are not built (``created``,
  ``destroyed``): the next build applies what the template now says. This runs after the
  response (``regenerate_for_template``), for every tenant's ranges of that template.

Rotation (ADR 0013 §3, supersedes #198). A new password is issued by ``_issue``:

* on a **rebuild** of a range built before (``on_build_accepted``): every Student-visible
  generated row; the build sets them. Staff rows stay as they are;
* on a **snapshot restore** (``on_restore``; a lab reset is one): the same rows, minus those
  on a Security Onion node (its SOC web login cannot be changed in the guest yet). The
  restore task then changes them in the guests (worker/credential_rotation.py), logging in
  with the passwords the snapshot recorded (``on_snapshot``) or the staff logins;
* by an **admin** (``rotate``, ``range:credentials_rotate``): one row, staff ones included.

A scenario account (``scenario: true``) is never rotated: its password is the lesson.
Every sync and rotation marks the rows it changed not ``applied``; the worker marks applied
what a build or rotation verified (worker/range_accounts.py, db_ops.mark_accounts_applied).

Reads (ADR 0012 §13, ADR 0013 §3), all tenant-scoped (another tenant's range is 404). The
list (``GET .../credentials``) carries no password; each password is revealed on its own
(``reveal``), and every reveal is audited (``range_credential_reveal``: who, which account).

==================================  ==============================================
admin, instructor, range_ops         every account (``range:credentials_read``)
Student                              ``student_visible`` accounts the last build or
                                     rotation set (``applied``), of a range they take
                                     part in: an exercise running or paused on it whose
                                     booked course they are rostered on, or their own
                                     live lab session's range. Otherwise 404.
observer and anyone else             403
==================================  ==============================================

The Student rule is the strict one of detection submission (app/exercise_participants.py,
ADR 0005), not the telemetry one: an exercise with no booked course seats no Student here.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import closing
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import safe_yaml
from ..auth import CurrentUser
from ..models import AuditLog, Exercise, ExerciseState, Range, RangeSnapshot, RangeState, Template, UserRole
from ..rbac import Permission, user_has_permission
from . import accounts as account_rules
from .models import RangeCredential, RangeCredentialSnapshot

logger = logging.getLogger("truenorth.range_credentials")

# A range in these states has no VMs a change would leave behind: its next build applies it.
UNBUILT = (RangeState.created, RangeState.destroyed)
# A range whose guests an admin rotation can reach now (guest operations need them running).
ROTATABLE = (RangeState.ready, RangeState.running)
ACTIVE_EXERCISE = (ExerciseState.running, ExerciseState.paused)

ALL = "all"
STUDENT = "student"


class InvalidAccountsError(ValueError):
    """The range's template declares accounts that do not validate."""


# ── Writes ──────────────────────────────────────────────────────────────
def template_of(db: Session, rng: Range) -> Template | None:
    # tenant-safe: the range's own template (FK); a public template may be another tenant's.
    return db.get(Template, rng.template_id) if rng.template_id else None


def foreign_template_allowed(tmpl: Template | None, rng: Range) -> bool:
    """Whether ``rng`` may take accounts from ``tmpl``: its own tenant's, or a global one."""
    return tmpl is None or tmpl.tenant_id is None or tmpl.tenant_id == rng.tenant_id


def _template_doc(tmpl: Template | None) -> dict:
    try:
        doc = safe_yaml.load(tmpl.yaml or "") if tmpl else {}
    except Exception:  # noqa: BLE001 — unreadable YAML builds nothing: no machine, no login
        return {}
    return doc if isinstance(doc, dict) else {}


def is_lab(db: Session, rng: Range) -> bool:
    """Whether a lab session builds this range (now, or before a rebuild retired it)."""
    from ..lab_sessions.service import lab_range_ids

    return rng.id in lab_range_ids(db, [rng.id])


def declared(db: Session, rng: Range) -> list[account_rules.AccountSpec]:
    """The range's accounts: what its template declares, plus the implicit staff logins of
    its machines (``accounts.implicit_staff_logins``). ``InvalidAccountsError`` if the
    template's accounts do not validate.

    Only the implicit logins from another tenant's template (a public one, reached through a
    booking or the designer): its declared passwords are that tenant's. Still never a
    site-wide default: this range gets its own generated logins (ADR 0013 §3).
    A lab session's implicit logins are handed to its Student (the lab is theirs)."""
    tmpl = template_of(db, rng)
    doc = _template_doc(tmpl)
    lab = is_lab(db, rng)
    if not foreign_template_allowed(tmpl, rng):
        return account_rules.implicit_staff_logins(doc, [], student_visible=lab)
    specs, errors = account_rules.parse_yaml(tmpl.yaml if tmpl else "", tenant_id=tmpl.tenant_id if tmpl else None)
    if errors:
        detail = "; ".join(f"{e['path']}: {e['message']}" for e in errors[:5])
        hint = (" Fix the template: mark an account whose weak password is the lesson `scenario: true`, or "
                "use `generate: true` (ADR 0013); admins see every affected template at "
                "GET /range-credentials/template-issues." if any("scenario" in e["message"] for e in errors) else "")
        raise InvalidAccountsError(f"template {tmpl.name!r} ({tmpl.id}) declares invalid accounts: {detail}.{hint}")
    return specs + account_rules.implicit_staff_logins(doc, specs, student_visible=lab)


def sync(db: Session, rng: Range, *, rotate_student: bool = False) -> int:
    """Make the range's rows match its template. Does not commit. Returns the row count.

    ``rotate_student``: a rebuild of a range built before; every Student-visible generated
    password is issued anew (staff ones are kept). Call only while nothing is built from
    the current rows (create, build acceptance, an unbuilt range's template changing): a
    password changed here is what the next build sets, so changing it under running VMs
    would make the record lie (a built range rotates through ``on_restore`` / ``rotate``)."""
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
                notes=spec.notes, applied=False, source=spec.source, scenario=spec.scenario,
            )
            if spec.generate:
                _issue(row)
            db.add(row)
            continue
        row.username, row.student_visible, row.role, row.notes, row.applied = (
            spec.username, spec.student_visible, spec.role, spec.notes, False)
        row.source, row.scenario, row.previous_password = spec.source, spec.scenario, None
        if spec.generate:
            if not row.generated:  # was a literal: make one up now
                _issue(row)
            elif rotate_student and row.student_visible:
                _issue(row, rotated=True)
        elif row.generated or row.password != spec.password:
            row.password, row.generated = spec.password, False
    for gone in rows.values():
        db.delete(gone)
    db.flush()
    return len(specs)


def _now() -> datetime:
    return datetime.now(UTC)


def _issue(row: RangeCredential, *, rotated: bool = False) -> None:
    """Give ``row`` a newly generated password. ``rotated``: it replaces one the row had."""
    row.password, row.generated = account_rules.generate_password(row.username), True
    if rotated:
        row.rotated_at = _now()


def rotatable(row: RangeCredential) -> bool:
    """A password TrueNorth may replace: generated, never a scenario account's."""
    return row.generated and not row.scenario


# Backends whose worker can change a password inside a running guest
# (provisioners/base.py ``supports_credential_rotation``).
GUEST_ROTATION_BACKENDS = frozenset({"vsphere_api"})


def _machine_kinds(db: Session, rng: Range) -> dict[str, str]:
    """Machine id -> ``windows`` | ``linux`` | ``appliance`` | ``securityonion``."""
    doc = _template_doc(template_of(db, rng))
    os_of = account_rules.machine_os(doc)
    return {m: (account_rules.SECURITY_ONION if account_rules.is_securityonion(os_of.get(m, "")) else fam)
            for m, fam in account_rules.machine_families(doc).items()}


def guest_change_refusal(rng: Range, row: RangeCredential, kinds: dict[str, str]) -> str | None:
    """Why the worker cannot set ``row``'s password in the range's running guests, or None."""
    if (rng.provisioner_backend or "") not in GUEST_ROTATION_BACKENDS:
        return f"the {rng.provisioner_backend or 'unknown'} backend cannot change passwords inside a guest"
    kind = kinds.get(row.machine)
    if kind == account_rules.SECURITY_ONION:
        return "a Security Onion node's logins are set by its own setup (rebuild the range to change them)"
    if kind == "appliance" or kind is None:
        return "an appliance's logins are its own configuration (rebuild the range to change them)"
    return None


def _snapshot_state(row: RangeCredential) -> tuple:
    """What ``undo_rotation`` puts back (the sealed values as stored)."""
    return row.id, row.sealed_password, row.applied, row.rotated_at, row.sealed_previous


def _rotate_in_place(row: RangeCredential, guest_has: str) -> None:
    """A new password for a built range's account, set in the guests by the worker, which logs
    in with ``guest_has`` (kept as ``previous_password``) and puts it back if the change fails."""
    row.previous_password = guest_has
    _issue(row, rotated=True)
    row.applied = False


def on_snapshot(db: Session, rng: Range, snap: RangeSnapshot) -> None:
    """Record the password each account has **in the guests** as ``snap`` is taken (the one a
    pending change has not replaced yet): a restore brings those back, and the change after
    it logs in with them. Does not commit."""
    for row in _rows(db, rng):
        in_guest = row.previous_password if row.pending else row.password
        db.add(RangeCredentialSnapshot(tenant_id=rng.tenant_id, snapshot_id=snap.id, credential_id=row.id,
                                       password=in_guest))
    db.flush()


def template_issues(db: Session, tenant_id: uuid.UUID | None, *, include_global: bool) -> list[dict]:
    """Templates (of ``tenant_id``, and global ones when ``include_global``) whose accounts no
    longer validate because a typed-in password is not on a scenario account (ADR 0013 §3):
    ranges of them cannot be provisioned, scheduled bookings included, until they are fixed.
    Each with its problems (never a password) and how many upcoming bookings use it."""
    from ..scheduler.models import EventState, ScheduledEvent

    scope = [Template.tenant_id.is_(None) if tenant_id is None else Template.tenant_id == tenant_id]
    if include_global and tenant_id is not None:
        scope.append(Template.tenant_id.is_(None))
    from sqlalchemy import or_

    out = []
    for tmpl in db.query(Template).filter(or_(*scope)).order_by(Template.name).all():
        if "password" not in (tmpl.yaml or ""):
            continue
        _, errors = account_rules.parse_yaml(tmpl.yaml, tenant_id=tmpl.tenant_id)
        problems = [f"{e['path']}: {e['message']}" for e in errors if "scenario" in e["message"]]
        if not problems:
            continue
        upcoming = (
            db.query(ScheduledEvent.id)
            .join(Range, Range.id == ScheduledEvent.range_id)
            .filter(Range.template_id == tmpl.id, ScheduledEvent.state == EventState.scheduled,
                    ScheduledEvent.end_time > _now())
            .count()
        )
        out.append({"template_id": tmpl.id, "name": tmpl.name, "version": tmpl.version, "tenant_id": tmpl.tenant_id,
                    "problems": problems, "upcoming_bookings": upcoming})
    return out


def warn_template_issues_on_start() -> None:
    """At API start: one WARNING per template (every tenant) whose accounts ADR 0013 no
    longer accepts, so operators hear of it before a booking fails. Never raises."""
    try:
        with session_factory() as db:
            # tenant-safe: an operator-facing log of every tenant's templates, names and ids only.
            for tenant_id in [t for (t,) in db.query(Template.tenant_id).distinct()]:
                for issue in template_issues(db, tenant_id, include_global=False):
                    logger.warning("ACTION NEEDED (ADR 0013): template %r (%s, tenant %s, %d upcoming booking(s)) "
                                   "cannot be provisioned: %s", issue["name"], issue["template_id"], tenant_id,
                                   issue["upcoming_bookings"], "; ".join(issue["problems"]))
    except Exception as exc:  # noqa: BLE001 — a startup log line must not stop the API
        logger.warning("could not check templates for ADR 0013 account issues (%s)", type(exc).__name__)


def forget_snapshot(db: Session, snap: RangeSnapshot) -> None:
    """Delete the passwords ``snap`` recorded (it is being deleted). Does not commit."""
    # tenant-safe: the caller's own snapshot (resolved through its range).
    db.query(RangeCredentialSnapshot).filter(RangeCredentialSnapshot.snapshot_id == snap.id,
                                             RangeCredentialSnapshot.tenant_id == snap.tenant_id).delete(
        synchronize_session=False)


def snapshot_passwords(db: Session, snap: RangeSnapshot) -> dict[uuid.UUID, str]:
    # tenant-safe: the caller's own snapshot (resolved through its range).
    return {r.credential_id: r.password for r in
            db.query(RangeCredentialSnapshot).filter(RangeCredentialSnapshot.snapshot_id == snap.id,
                                                     RangeCredentialSnapshot.tenant_id == snap.tenant_id)}


def on_restore(db: Session, rng: Range, snap: RangeSnapshot, actor: CurrentUser | uuid.UUID | None) -> list[tuple]:
    """A snapshot restore (a lab reset is one) was accepted. The revert brings back the
    passwords the guests had when ``snap`` was taken, so every account the guests may have
    (applied, or with a change pending) becomes a pending change the restore task carries out
    after the revert (worker/credential_rotation.py), logging in with the snapshot's password:

    * a Student-visible generated account gets a **new** password (ADR 0013 §3);
    * any other generated account whose current password differs from the snapshot's (an
      admin rotated it after the snapshot) gets its **current** password set again, so a
      reveal never shows a password the guest no longer has (adversarial review of #250).

    Machines the worker cannot change (Security Onion, appliances, another backend) are left
    as they are. One audit row lists what is rotated and re-applied. Does not commit.
    Returns what ``undo_rotation`` needs should the restore not be sent (in memory only)."""
    kinds = _machine_kinds(db, rng)
    recorded = snapshot_passwords(db, snap)
    undo, rotated, reapplied = [], [], []
    for row in _rows(db, rng):
        if not rotatable(row) or not (row.applied or row.pending) or guest_change_refusal(rng, row, kinds):
            continue
        current_in_guest = row.previous_password if row.pending else row.password
        guest_has = recorded.get(row.id, current_in_guest)  # an older snapshot recorded nothing
        if row.student_visible:
            undo.append(_snapshot_state(row))
            _rotate_in_place(row, guest_has)
            rotated.append(f"{row.machine}/{row.username}")
        elif guest_has != row.password:
            undo.append(_snapshot_state(row))
            row.previous_password, row.applied = guest_has, False  # the current password, set again
            reapplied.append(f"{row.machine}/{row.username}")
    db.flush()
    if rotated or reapplied:
        _audit(db, actor, rng, "range_credential_rotate", {
            "range_id": str(rng.id), "reason": "snapshot restore", "operation_id": f"restore:{snap.id}",
            "rotated": rotated, "reapplied": reapplied, "in_guest": True})
    return undo


def undo_rotation(db: Session, undo: list[tuple]) -> None:
    """Put back what ``on_restore`` changed (the restore task was never sent). Does not commit."""
    for row_id, sealed, applied, rotated_at, sealed_previous in undo:
        # tenant-safe: ids on_restore just read from one range of the caller's tenant.
        row = db.get(RangeCredential, row_id)
        if row is not None:
            row.sealed_password, row.applied, row.rotated_at, row.sealed_previous = (
                sealed, applied, rotated_at, sealed_previous)
    db.flush()


def rotate(db: Session, rng: Range, row: RangeCredential, user: CurrentUser, reason: str,
           operation_id: str) -> bool:
    """An admin's rotation of one account. Returns whether the worker must now set it in the
    guests (a built, running range); an unbuilt range's next build sets it. 409 for a
    scenario or typed-in password, an account whose last change is still being applied, a
    machine or backend the worker cannot change in the guest, or a range in a state no
    rotation can reach. Audited (with ``operation_id``, the id the worker task logs), without
    the password. Does not commit."""
    if not rotatable(row):
        raise HTTPException(409, "This account's password was typed into the range template (a scenario "
                                 "account, authored by design); it is not rotated")
    if row.pending:
        raise HTTPException(409, "A change of this account's password is still being applied in the guests; "
                                 "rotate it again once it shows as set (or failed)")
    if rng.state in UNBUILT:
        _issue(row, rotated=True)
        row.applied = False
        in_guest = False
    elif rng.state in ROTATABLE:
        from ..range_ops import service as ops

        if why := guest_change_refusal(rng, row, _machine_kinds(db, rng)):
            raise HTTPException(409, f"Cannot rotate this account in the running range: {why}")
        ops.refuse_while_in_flight(db, rng, "rotate a password")
        restoring = db.query(RangeSnapshot.id).filter(RangeSnapshot.range_id == rng.id,
                                                      RangeSnapshot.snapshot_state == "restoring").first()
        if restoring:
            raise HTTPException(409, "Cannot rotate a password: a restore of this range is in progress")
        _rotate_in_place(row, row.password)
        in_guest = True
    else:
        raise HTTPException(409, f"Cannot rotate a password of a range in state {rng.state.value}: rotate it "
                                 "when the range is ready or running, or before it is built")
    db.flush()
    _audit(db, user, rng, "range_credential_rotate", {
        "range_id": str(rng.id), "machine": row.machine, "username": row.username, "reason": reason,
        "operation_id": operation_id, "in_guest": in_guest})
    return in_guest


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
    """The allocation step, before the new operation is recorded: a rebuild of a range built
    (or attempted) before gives every Student-visible account a new password; staff ones
    are kept."""
    from ..range_ops.models import RangeOperation

    rebuilt = db.query(RangeOperation.id).filter(RangeOperation.range_id == rng.id,
                                                 RangeOperation.action == "provision").first() is not None
    try:
        sync(db, rng, rotate_student=rebuilt)
    except InvalidAccountsError as exc:
        raise HTTPException(422, f"Cannot provision: {exc}") from None


def on_lab_range(db: Session, rng: Range) -> None:
    """A lab session's range was just created (app/lab_sessions, which builds it without
    ``range_ops.accept``): its rows, generated per lab. A lab rebuild makes a new range,
    so its passwords are new too. Raises nothing a lab start could not survive: a lab
    profile declares no accounts, only the implicit logins, which always validate."""
    sync(db, rng)


def on_build_succeeded(db: Session, rng: Range) -> None:
    """The range is built: its page gains the addresses the worker recorded. Never raises."""
    _document(db, rng, None)


def regenerate_for_template(template_id: uuid.UUID) -> None:
    """After a template change (run after the response, ``BackgroundTasks``): re-sync every
    unbuilt range of it and regenerate their pages, in the template's own tenant only (a
    global template's ranges in every tenant only while it declares no accounts). Another
    tenant's range never takes this template's accounts (``declared``), so there is nothing
    of it to refresh. Batched, one commit per range; never raises."""
    try:
        _regenerate_for_template(template_id)
    except Exception as exc:  # noqa: BLE001 — after the response there is nobody to tell but the log
        logger.warning("template %s: its ranges were not refreshed (%s)", template_id, type(exc).__name__)


def _regenerate_for_template(template_id: uuid.UUID) -> None:
    with session_factory() as db:
        # tenant-safe: the template by id, to read its own tenant (the scope below).
        tmpl = db.get(Template, template_id)
        if tmpl is None:
            return
        scope = [Range.template_id == template_id, Range.deleted_at.is_(None)]
        if tmpl.tenant_id is not None:
            scope.append(Range.tenant_id == tmpl.tenant_id)
        elif account_rules.parse_yaml(tmpl.yaml, tenant_id=None)[0]:
            logger.warning("global template %s declares accounts; its ranges refresh at their next build",
                           template_id)
            return
        last: uuid.UUID | None = None
        while True:
            q = db.query(Range).filter(*scope)
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
    return participation(db, user, rng) is not None


def participation(db: Session, user: CurrentUser, rng: Range) -> dict[str, str] | None:
    """How a Student takes part in ``rng``: ``{"lab_session_id": ...}`` (their own live lab
    session's range) or ``{"exercise_id": ...}`` (module docstring); None when they do not."""
    from ..enrollment import rostered_course_ids
    from ..exercise_participants import roster_courses
    from ..lab_sessions.models import LIVE, LabSession
    try:
        uid = uuid.UUID(str(user.id))
    except ValueError:
        return None
    lab = (
        db.query(LabSession.id)
        .filter(LabSession.range_id == rng.id, LabSession.tenant_id == rng.tenant_id, LabSession.user_id == uid,
                LabSession.state.in_(LIVE))
        .first()
    )
    if lab is not None:
        return {"lab_session_id": str(lab[0])}
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
        return None
    mine = set(rostered_course_ids(db, uid))
    if not mine:
        return None
    ex = next((ex for ex in exercises if roster_courses(db, ex) & mine), None)
    return {"exercise_id": str(ex.id)} if ex is not None else None


def readable_range(db: Session, range_id: uuid.UUID, user: CurrentUser, scope: str) -> Range:
    """The caller's range, or 404: another tenant's, a deleted one, a Student's lab to staff
    without infrastructure rights (as routers/ranges.py), one a Student does not take part in.
    A Student's own lab session's range is theirs (``participation``)."""
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


def setup_pending_machines(rng: Range) -> set[str]:
    """Node ids whose deploy-time setup (a product role image or Security Onion) is still
    ``running`` in the range's output. Staff see such a machine's accounts as "being set up"
    until the reconcile task verifies them (worker/setup_reconcile.py); a Student sees nothing
    for it yet. Never raises on odd output."""
    out: set[str] = set()
    try:
        output = json.loads(rng.provisioner_output) if rng.provisioner_output else {}
        for vm in output.get("vms") or []:
            running = ((vm.get("role_setup") or {}).get("status") == "running"
                       or (vm.get("securityonion") or {}).get("status") == "running")
            if running and vm.get("node_id"):
                out.add(str(vm["node_id"]))
    except (ValueError, TypeError, AttributeError):
        return out
    return out


def setup_status(row: RangeCredential, pending: set[str]) -> str:
    """How a staff reader sees a row: ``applied`` (verified), ``being_set_up`` (its machine's
    setup is still running) or ``not_set`` (TrueNorth did not set it, or not built yet)."""
    if row.applied:
        return "applied"
    return "being_set_up" if row.machine in pending else "not_set"


def account(db: Session, rng: Range, cred_id: uuid.UUID, scope: str) -> RangeCredential:
    """One account of ``rng`` the caller may see, or 404 (another range's, or one a
    Student is not handed: not ``student_visible``, or not set by the last build)."""
    row = (
        db.query(RangeCredential)
        .filter(RangeCredential.id == cred_id, RangeCredential.range_id == rng.id,
                RangeCredential.tenant_id == rng.tenant_id)
        .first()
    )
    if row is None or (scope != ALL and not (row.student_visible and row.applied)):
        raise HTTPException(404, "Account not found")
    return row


def reveal(db: Session, rng: Range, row: RangeCredential, scope: str, actor: CurrentUser | uuid.UUID | None,
           via: str, context: dict[str, str] | None = None) -> str:
    """The password that logs in now, with one audit row: who, which account, how (``via``)
    and, for a Student, through which exercise or lab session. Never the password. While a
    change is pending the guests still have the previous password, so that one is shown (a
    reveal never shows a password the guest does not have). Does not commit."""
    _audit(db, actor, rng, "range_credential_reveal", {
        "range_id": str(rng.id), "machine": row.machine, "username": row.username, "scope": scope, "via": via,
        **({"pending_change": True} if row.pending else {}), **(context or {})})
    return (row.previous_password if row.pending else row.password) or ""


def _audit(db: Session, actor: CurrentUser | uuid.UUID | None, rng: Range, action: str, detail: dict) -> None:
    db.add(AuditLog(
        user_id=_audit_user(actor),
        tenant_id=rng.tenant_id,
        action=action,
        resource_type="range",
        resource_id=str(rng.id),
        detail=json.dumps(detail, sort_keys=True)[:2000],
    ))


def _audit_user(actor: CurrentUser | uuid.UUID | None) -> uuid.UUID | None:
    if actor is None or isinstance(actor, uuid.UUID):
        return actor
    try:
        return uuid.UUID(str(actor.id))
    except ValueError:
        return None
