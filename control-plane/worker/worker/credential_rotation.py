"""Set new range passwords inside running guests (ADR 0013 §3; supersedes #198).

The API issues the new passwords (app/range_credentials/service.py) and marks the rows not
``applied``; this module sets them in the guests and marks applied what a login verified:

* **after a snapshot restore** (a lab reset is one): every Student-visible account the API
  rotated when it accepted the restore (``pending``: rotated and not yet applied). The
  revert brought back the passwords the guests had when the snapshot was taken, so the
  admin login is tried with those first (``db_ops.snapshot_passwords``), then with the
  password the guest had before the rotation (``previous``), then the current one;
* **an admin's rotation** of one account (``rotate_range_credentials`` with its id).

Per machine it logs in as an admin account of that machine: the built-in Administrator
(Windows) or the Linux build user, else another ``role: admin`` account (Linux), each
password tried once. Security Onion nodes and appliances are not changed here (their
logins are set by their own setup or configuration): their rows keep waiting for a rebuild.
A backend that cannot change passwords in a guest (``supports_credential_rotation``) leaves
the rows not applied: Students then see none of them until the next build, never a stale one.

``rotate_range_credentials`` is the Celery task (the API sends it for an admin rotation;
``_after_restore`` sends it when ``restore_snapshot`` succeeds, through Celery's
``task_success`` signal, so tasks.py does not grow, ADR 0003). Passwords are never logged.
"""

from __future__ import annotations

import json
import logging
import os

from celery.signals import task_success

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


def plan(range_id: str, rows: list[dict], vms: list[dict], *, ids: set[str] | None = None,
         snapshot: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """What ``rotate_guest_passwords`` is asked to do, and notes (no secrets): ``ids`` the
    accounts an admin rotates, else every pending one (after a restore)."""
    targets = [r for r in rows if r["generated"] and ((r["id"] in ids) if ids is not None else r["pending"])]
    by_machine: dict[str, list[dict]] = {}
    for r in targets:
        by_machine.setdefault(r["machine"], []).append(r)
    work: list[dict] = []
    notes: list[str] = []
    for machine, changes in sorted(by_machine.items()):
        built = [v for v in vms if isinstance(v, dict) and v.get("vm_id")
                 and range_accounts.node_of(v, range_id) == machine]
        if not built:
            notes.append(f"machine {machine}: no built VM; its new passwords apply at the next build")
            continue
        for vm in built:
            family = family_of(vm)
            if family == "appliance" or vm.get("securityonion"):
                notes.append(f"VM {vm.get('name')}: passwords are not changed in this guest (its logins are set by "
                             "its own setup); they apply at the next build")
                continue
            logins = _admin_logins(rows, machine, family, snapshot or {})
            if not logins:
                notes.append(f"VM {vm.get('name')}: no admin account to log in with; passwords apply at the next build")
                continue
            work.append({"vm_id": str(vm["vm_id"]), "name": str(vm.get("name") or ""), "family": family,
                         "node_id": machine, "logins": logins,
                         "changes": [GuestLogin(r["username"], r["password"], family) for r in changes]})
    return work, notes


def run(range_id: str, *, ids: list[str] | None = None, snapshot_id: str | None = None,
        operation_id: str = "") -> dict:
    """Plan and apply one rotation; mark applied what was verified. Returns a summary
    (usernames and counts only)."""
    from .base_tasks import _get_backend
    from .tasks import _db_session

    with _db_session() as db:
        rows = db_ops.account_rows(db, range_id)
        snapshot = db_ops.snapshot_passwords(db, range_id, snapshot_id) if snapshot_id else {}
        found = db_ops.range_output_and_backend(db, range_id)
    if not found or not found[0]:
        return {"status": "skipped", "detail": "the range has no build recorded"}
    output = json.loads(found[0])
    work, notes = plan(range_id, rows, output.get("vms") or [], ids=set(ids) if ids else None, snapshot=snapshot)
    for note in notes:
        logger.warning("[rotate %s] range %s: %s", operation_id, range_id, note)
    if not work:
        return {"status": "nothing_to_do", "notes": notes}
    provisioner = _get_backend(found[1], range_id, output=output)
    if not getattr(provisioner, "supports_credential_rotation", False):
        logger.warning("[rotate %s] range %s: the %s backend cannot change passwords in a guest; the accounts stay "
                       "not applied until the next build", operation_id, range_id, found[1])
        return {"status": "unsupported", "notes": notes}
    results = run_async(provisioner.rotate_guest_passwords(range_id, work, ROTATION_TIMEOUT))
    built = []
    for item in work:
        res = results.get(item["name"]) or {}
        if res.get("error"):
            logger.warning("[rotate %s] range %s VM %s: %s", operation_id, range_id, item["name"], res["error"])
        built.append({"name": item["name"], "node_id": item["node_id"],
                      "accounts_applied": list(res.get("applied") or [])})
    range_accounts.record_applied(_db_session, range_id, built)
    applied = sum(len(b["accounts_applied"]) for b in built)
    logger.info("[rotate %s] range %s: %d password(s) set and verified", operation_id, range_id, applied)
    return {"status": "ok", "applied": applied, "notes": notes}


@app.task(base=ReliableTask, bind=True, name=TASK)
def rotate_range_credentials(self, range_id: str, credential_ids: list, operation_id: str = "",
                             snapshot_id: str | None = None):
    """Set rotated passwords in the range's running guests (module docstring). Empty
    ``credential_ids``: every pending account (after ``snapshot_id`` was restored)."""
    return run(range_id, ids=[str(i) for i in credential_ids or []] or None, snapshot_id=snapshot_id,
               operation_id=operation_id)


@task_success.connect
def _after_restore(sender=None, result=None, **_kwargs) -> None:
    """A snapshot restore succeeded: send the rotation of the passwords the API renewed for it."""
    if getattr(sender, "name", "") != RESTORE_TASK or not isinstance(result, dict) or result.get("status") != "restored":
        return
    try:
        range_id, snapshot_id = (list(getattr(sender.request, "args", None) or []) + [None, None])[:2]
        if range_id and snapshot_id:
            app.send_task(TASK, args=[str(range_id), [], f"restore:{snapshot_id}", str(snapshot_id)],
                          ignore_result=True)
    except Exception:  # noqa: BLE001 — the restore stands; the rows stay not applied (Students see none)
        logger.warning("could not send the password rotation after a restore of range %s", result.get("range_id"))
