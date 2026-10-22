"""Set new range passwords inside running guests (ADR 0013 §3; supersedes #198).

The API records each change as pending (app/range_credentials/service.py): the row's
``password`` is the one to set, ``previous_password`` the one the guests have now. This
module sets them in the guests and settles every row (compare-and-set on the version it
read, so a change made meanwhile is left alone): applied once a login with the new password
worked; otherwise put back to the previous password, which the guests still have, with an
audit row either way (``range_credential_change``). So a reveal never shows a password that
does not log in (adversarial review of #250):

* **after a snapshot restore** (a lab reset is one): every pending account, new
  Student-visible passwords and current passwords an admin set after the snapshot alike.
  The revert brought back the passwords the snapshot recorded, so the admin login is tried
  with those first (``db_ops.snapshot_passwords``), then the previous, then the current one;
* **an admin's rotation** of one account (``rotate_range_credentials`` with its id).

Per machine it logs in as an admin account of that machine: the built-in Administrator
(Windows) or the Linux build user, else another ``role: admin`` account (Linux), each
password tried once. Security Onion nodes and appliances are not changed here (their
logins are set by their own setup or configuration), and a backend that cannot change
passwords in a guest (``supports_credential_rotation``) changes nothing: the API refuses such
changes up front, and any that still reach here are put back.

``rotate_range_credentials`` is the Celery task (the API sends it for an admin rotation;
``_after_restore`` sends it when ``restore_snapshot`` succeeds, through Celery's
``task_success`` signal, so tasks.py does not grow, ADR 0003). Passwords are never logged.
"""

from __future__ import annotations

import json
import logging
import os

from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_failure, task_success, worker_init

from . import db_ops, range_accounts
from .base_tasks import ReliableTask
from .celery_app import app
from .fencing import run_async
from .provisioners.base import GuestLogin

logger = logging.getLogger("truenorth.worker.credential_rotation")

TASK = "worker.tasks.rotate_range_credentials"
RESTORE_TASK = "worker.tasks.restore_snapshot"
# Per run, for every VM together (guest operations after a revert wait for VMware Tools).
ROTATION_TIMEOUT = float(os.getenv("RANGE_ROTATION_TIMEOUT", "900"))


def family_of(vm: dict) -> str:
    """``windows`` | ``linux`` | ``appliance`` as the build recorded it (older outputs: the template name)."""
    if vm.get("family"):
        return str(vm["family"])
    return "windows" if "win" in str(vm.get("template") or vm.get("name") or "").lower() else "linux"


def _admin_logins(rows: list[dict], machine: str, family: str, snapshot: dict[str, str]) -> list[GuestLogin]:
    """The logins to try on ``machine``: its admin accounts in order of preference, each with
    the passwords the guest may have (snapshot-time, before a pending rotation, current)."""
    from .provisioners.vsphere_guest import build_user  # a name; no vendor SDK is needed for it

    admins = [r for r in rows if r["machine"] == machine and r["role"] == "admin"]
    if family == "windows":
        admins = [r for r in admins if r["username"].casefold() == "administrator"]
    else:
        preferred = build_user().casefold()
        admins.sort(key=lambda r: (r["username"].casefold() != preferred, r["username"]))
    out: list[GuestLogin] = []
    for r in admins:
        for password in (snapshot.get(r["id"]), r["previous"], r["password"]):
            login = GuestLogin(r["username"], password or "", family)
            if password and login not in out:
                out.append(login)
    return out


def plan(range_id: str, rows: list[dict], vms: list[dict], *, operation: str, ids: set[str] | None = None,
         snapshot: dict[str, str] | None = None) -> tuple[list[dict], list[str], list[dict]]:
    """What ``rotate_guest_passwords`` is asked to do, notes (no secrets), and the targeted
    rows that cannot be done here (their change is reverted). Only rows whose pending change
    is ``operation``'s (review of #250: a task never acts on another operation's change);
    ``ids`` narrows them to the accounts an admin rotates."""
    targets = [r for r in rows if r["generated"] and r["pending"] and r.get("operation") == operation
               and (ids is None or r["id"] in ids)]
    by_machine: dict[str, list[dict]] = {}
    for r in targets:
        by_machine.setdefault(r["machine"], []).append(r)
    work: list[dict] = []
    notes: list[str] = []
    undone: list[dict] = []
    for machine, changes in sorted(by_machine.items()):
        built = [v for v in vms if isinstance(v, dict) and v.get("vm_id")
                 and range_accounts.node_of(v, range_id) == machine]
        if not built:
            notes.append(f"machine {machine}: no built VM")
            undone += changes
            continue
        if any(family_of(vm) == "appliance" or vm.get("securityonion") for vm in built):
            notes.append(f"machine {machine}: its logins are set by its own setup, not changed in the guest")
            undone += changes
            continue
        family = family_of(built[0])
        logins = _admin_logins(rows, machine, family, snapshot or {})
        if not logins:
            notes.append(f"machine {machine}: no admin account to log in with")
            undone += changes
            continue
        for vm in built:
            work.append({"vm_id": str(vm["vm_id"]), "name": str(vm.get("name") or ""), "family": family,
                         "node_id": machine, "logins": logins, "rows": changes, "identity": vm.get("identity"),
                         "changes": [GuestLogin(r["username"], r["password"], family) for r in changes]})
    return work, notes, undone


def _settle(range_id: str, rows: list[dict], *, applied: bool, operation_id: str, why: str = "",
            revert_to: dict[str, str] | None = None) -> None:
    """Settle each row's change of ``operation_id`` (compare-and-set on its sealed version),
    and audit it. ``revert_to``: account id -> the seal to put back when not applied (a
    snapshot's, once its revert ran); else the previous password."""
    from .tasks import _db_session

    if not rows:
        return
    with _db_session() as db:
        settled = [r for r in rows
                   if db_ops.settle_credential_change(db, range_id, r["id"], r["sealed"], applied=applied,
                                                      operation=operation_id,
                                                      revert_to=(revert_to or {}).get(r["id"]))]
        if settled:
            db_ops.audit_credential_change(db, range_id, settled[0]["tenant_id"], {
                "operation_id": operation_id, "outcome": "applied" if applied else "reverted",
                "accounts": [f"{r['machine']}/{r['username']}" for r in settled], **({"why": why} if why else {})})
        db.commit()
    stale = len(rows) - len(settled)
    if stale:
        logger.info("[rotate %s] range %s: %d account(s) changed again meanwhile; left to that change",
                    operation_id, range_id, stale)


def run(range_id: str, *, ids: list[str] | None = None, snapshot_id: str | None = None,
        operation_id: str = "") -> dict:
    """Plan and apply one change in the guests. Each account is settled: applied when a login
    with its new password worked, otherwise put back to the password the guests still have
    (a timeout, an error, an unsupported backend or machine included), so a reveal never
    shows a password that does not log in. Returns a summary (usernames and counts only)."""
    from .base_tasks import _get_backend
    from .tasks import _db_session

    with _db_session() as db:
        rows = db_ops.account_rows(db, range_id)
        recorded = db_ops.snapshot_passwords(db, range_id, snapshot_id) if snapshot_id else {}
        found = db_ops.range_output_and_backend(db, range_id)
    output = json.loads(found[0]) if found and found[0] else {}
    # After a revert the guests have what the snapshot recorded: log in with it, and put it
    # back (its seal, as is) for a change that does not take.
    snapshot = {cid: plain for cid, (plain, _) in recorded.items()}
    revert_to = {cid: sealed for cid, (_, sealed) in recorded.items()}
    work, notes, undone = plan(range_id, rows, output.get("vms") or [], operation=operation_id,
                               ids=set(ids) if ids else None, snapshot=snapshot)
    for note in notes:
        logger.warning("[rotate %s] range %s: %s", operation_id, range_id, note)
    _settle(range_id, undone, applied=False, operation_id=operation_id, why="; ".join(notes)[:500],
            revert_to=revert_to)
    if not work:
        return {"status": "nothing_to_do" if not undone else "reverted", "notes": notes}
    targets = {r["id"]: r for w in work for r in w["rows"]}
    try:
        provisioner = _get_backend(found[1], range_id, output=output)
        if not getattr(provisioner, "supports_credential_rotation", False):
            raise RuntimeError(f"the {found[1]} backend cannot change passwords in a guest")
        results = run_async(provisioner.rotate_guest_passwords(
            range_id, [{k: v for k, v in w.items() if k != "rows"} for w in work], ROTATION_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — every target goes back to what the guests have
        why = type(exc).__name__ if not isinstance(exc, RuntimeError) else str(exc)[:200]
        logger.warning("[rotate %s] range %s: not changed in the guests (%s)", operation_id, range_id, why)
        _settle(range_id, list(targets.values()), applied=False, operation_id=operation_id, why=why,
                revert_to=revert_to)
        if isinstance(exc, SoftTimeLimitExceeded):
            raise
        return {"status": "reverted", "notes": [*notes, why]}
    verified: dict[str, set[str]] = {}  # machine -> usernames verified on EVERY VM of it
    for w in work:
        res = results.get(w["name"]) or {}
        if res.get("error"):
            logger.warning("[rotate %s] range %s VM %s: %s", operation_id, range_id, w["name"], res["error"])
        names = set(res.get("applied") or [])
        verified[w["node_id"]] = verified[w["node_id"]] & names if w["node_id"] in verified else names
    ok = [r for r in targets.values() if r["username"] in verified.get(r["machine"], set())]
    failed = [r for r in targets.values() if r not in ok]
    _settle(range_id, ok, applied=True, operation_id=operation_id)
    _settle(range_id, failed, applied=False, operation_id=operation_id, why="not verified by a login in the guest",
            revert_to=revert_to)
    logger.info("[rotate %s] range %s: %d set and verified, %d put back", operation_id, range_id, len(ok),
                len(failed))
    return {"status": "ok" if not failed else "partial", "applied": len(ok), "reverted": len(failed),
            "notes": notes}


@app.task(base=ReliableTask, bind=True, name=TASK)
def rotate_range_credentials(self, range_id: str, credential_ids: list, operation_id: str = "",
                             snapshot_id: str | None = None):
    """Set rotated passwords in the range's running guests (module docstring). Empty
    ``credential_ids``: every pending account (after ``snapshot_id`` was restored)."""
    return run(range_id, ids=[str(i) for i in credential_ids or []] or None, snapshot_id=snapshot_id,
               operation_id=operation_id)


@worker_init.connect
def _refuse_to_start_without_template_passwords(**_kwargs) -> None:
    """ADR 0013 §3, adversarial review of #250: a vSphere worker in ``enforce`` mode that does
    not know the template passwords stops at start, loudly, rather than marking every VM's
    template-login check inconclusive (provisioners/vsphere_credcheck.startup_problem)."""
    from .provisioners.vsphere_credcheck import startup_problem

    if problem := startup_problem():
        logger.critical(problem)
        raise SystemExit(problem)  # not an Exception: Celery's signal dispatch would swallow that


def roll_back_operation(range_id: str, operation_id: str, why: str) -> int:
    """Put every pending change of ``operation_id`` back to the password the guests still
    have (its previous one): a restore that failed before changing anything, or was skipped,
    never ran its revert. Audited. Returns how many were put back. Never raises."""
    from .tasks import _db_session

    try:
        with _db_session() as db:
            rows = [r for r in db_ops.account_rows(db, range_id) if r["pending"] and r.get("operation") == operation_id]
        _settle(range_id, rows, applied=False, operation_id=operation_id, why=why)
        return len(rows)
    except Exception:  # noqa: BLE001 — an admin can still force-settle them (POST .../settle)
        logger.warning("[rotate %s] range %s: could not put the pending changes back", operation_id, range_id)
        return 0


def _restore_args(sender) -> tuple[str | None, str | None]:
    range_id, snapshot_id = (list(getattr(getattr(sender, "request", None), "args", None) or []) + [None, None])[:2]
    return (str(range_id) if range_id else None), (str(snapshot_id) if snapshot_id else None)


@task_success.connect
def _after_restore(sender=None, result=None, **_kwargs) -> None:
    """A snapshot restore ended. Restored: send the rotation of the passwords the API renewed
    for it. Skipped (the range moved on, nothing reverted): put those changes back."""
    if getattr(sender, "name", "") != RESTORE_TASK or not isinstance(result, dict):
        return
    range_id, snapshot_id = _restore_args(sender)
    if not (range_id and snapshot_id):
        return
    if result.get("status") == "skipped":
        roll_back_operation(range_id, f"restore:{snapshot_id}", "the restore was skipped: nothing was reverted")
        return
    if result.get("status") != "restored":
        return
    try:
        app.send_task(TASK, args=[range_id, [], f"restore:{snapshot_id}", snapshot_id], ignore_result=True)
    except Exception:  # noqa: BLE001 — the rows stay pending (Students see none); an admin can settle them
        logger.warning("could not send the password rotation after a restore of range %s", range_id)


@task_failure.connect
def _after_failed_restore(sender=None, **_kwargs) -> None:
    """A snapshot restore failed for good (after its retries): its pending password changes
    go back to what the guests had before (review of #250). A restore that reverted some VMs
    and not others leaves the range ``failed``; its passwords go back to the pre-restore ones
    too, and the range needs a rebuild either way."""
    if getattr(sender, "name", "") != RESTORE_TASK:
        return
    range_id, snapshot_id = _restore_args(sender)
    if range_id and snapshot_id:
        roll_back_operation(range_id, f"restore:{snapshot_id}", "the restore failed")
