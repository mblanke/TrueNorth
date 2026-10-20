"""Finish long in-guest setups a build left ``running`` (ADR 0012).

A product role image (Exchange, SharePoint SE, MECM) and Security Onion each take 20 min to
2 h in the guest, far longer than a provision may run before the Celery soft time limit
(worker/fencing.py). So the build starts the setup, records it ``running`` with the status
file to read and when it started, and finishes within its budget. This periodic task then
reads each ``running`` setup's status in its guest and records the real outcome
(``ok`` / ``failed`` / ``timeout``), replacing the "being set up" warning. Accounts a setup
sets (the SOC web login, the range admin) are marked ``applied`` only once verified here.

Secrets are never stored in the range output: the domain Administrator password is re-read
from the range's credentials each run (worker/range_accounts.py), used only for the
guest-operations auth, and the Security Onion install login comes from the environment or the
range's admin account. The write back is conditional on the range still being ``ready`` and
done under its row lock, so it never overwrites a range an operator has moved on. A no-op
when no range has a setup running. Kept out of tasks.py (ADR 0003).
"""

from __future__ import annotations

import json
import logging
import os

from .base_tasks import ReliableTask
from .celery_app import app
from .fencing import run_async

logger = logging.getLogger("truenorth.worker.setup_reconcile")


def _running(output: dict) -> tuple[list[dict], list[dict]]:
    """(product VMs, Security Onion VMs) in the output whose setup is still ``running``."""
    vms = output.get("vms") or []
    products = [v for v in vms if (v.get("role_setup") or {}).get("status") == "running"
                and (v["role_setup"].get("reconcile"))]
    sos = [v for v in vms if (v.get("securityonion") or {}).get("status") == "running"
           and (v["securityonion"].get("reconcile"))]
    return products, sos


def _domain_passwords(declared: dict, products: list[dict], default) -> dict:
    """DC node id -> its Administrator password (the range's declared/generated account, else
    the template default). The reconcile logs in to each product VM as that domain admin."""
    out: dict[str, str] = {}
    for v in products:
        node = str((v["role_setup"]["reconcile"]).get("dc_node") or "")
        if not node or node in out:
            continue
        admin = next((a for a in declared.get(node, []) if a.username.casefold() == "administrator"), None)
        secret = admin.password if admin is not None else default
        if secret is not None:
            out[node] = secret.reveal()
    return out


def _so_logins(declared: dict, sos: list[dict], env: dict) -> dict:
    """Security Onion node id -> (install user, password): the template build login from the
    environment, else the machine's declared admin account."""
    out: dict[str, tuple[str, str]] = {}
    for v in sos:
        info = v["securityonion"]["reconcile"]
        node = str(info.get("node") or "")
        if info.get("install_from_env") and env.get("VSPHERE_SO_INSTALL_USER") and env.get("VSPHERE_SO_INSTALL_PASSWORD"):
            out[node] = (env["VSPHERE_SO_INSTALL_USER"], env["VSPHERE_SO_INSTALL_PASSWORD"])
            continue
        admin = next((a for a in declared.get(node, []) if a.admin), None)
        if admin is not None:
            out[node] = (admin.username, admin.password.reveal())
    return out


def _merge(output: dict, changes: dict) -> tuple[dict, list[dict]]:
    """Apply the reconcile outcomes to the stored output: the VM's ``role_setup`` /
    ``securityonion`` and its role status, and drop that VM's "being set up" warning. Returns
    the new output and the VMs whose accounts the reconcile verified (for record_applied)."""
    applied: list[dict] = []
    warnings = list(output.get("warnings") or [])
    for vm in output.get("vms") or []:
        change = changes.get(vm.get("name"))
        if not change:
            continue
        if "role_setup" in change:
            vm["role_setup"] = change["role_setup"]
            role = change.get("role") or ""
            if role:
                vm.setdefault("roles", {})[role] = {"status": change["role_setup"]["status"],
                                                    "detail": change["role_setup"]["detail"]}
        if "securityonion" in change:
            vm["securityonion"] = change["securityonion"]
            if change.get("accounts_applied"):
                vm["accounts_applied"] = change["accounts_applied"]
                applied.append({"name": vm.get("name"), "node_id": change.get("node"),
                                "accounts_applied": change["accounts_applied"]})
        # Replace the per-VM "being set up" warning with the terminal one (none when ok).
        setup = change.get("role_setup") or change.get("securityonion") or {}
        warnings = [w for w in warnings if not w.startswith(f"VM {vm.get('name')}: ")
                    or "being set up" not in w and "continues in the guest" not in w]
        if setup.get("status") and setup["status"] != "ok":
            warnings.append(f"VM {vm.get('name')}: setup {setup['status']}: {setup.get('detail', '')}")
    output["warnings"] = warnings
    return output, applied


def _reconcile_range(range_id: str, name: str, output: dict, backend: str) -> None:
    from . import db_ops, range_accounts
    from .tasks import _db_session, _get_backend

    products, sos = _running(output)
    if not products and not sos:
        return
    provisioner = _get_backend(backend, range_id, output=output)
    if not getattr(provisioner, "supports_setup_reconcile", False):
        return
    declared, _lab = range_accounts.load(_db_session, range_id)
    domain_passwords = _domain_passwords(declared, products, range_accounts.default_password())
    so_logins = _so_logins(declared, sos, os.environ)
    changes, warnings = run_async(provisioner.reconcile_setups(
        range_id, output, domain_passwords=domain_passwords, so_logins=so_logins))
    for w in warnings:
        logger.warning("[reconcile] range %s: %s", range_id, w)
    if not changes:
        return
    # Re-read under the row lock and write back only while the range is still ready.
    with _db_session() as db:
        db_ops.lock_range(db, range_id)
        row = db_ops.range_output_and_backend(db, range_id)
        current = json.loads(row[0]) if row and row[0] else output
        merged, applied = _merge(current, changes)
        if db_ops.set_range_output(db, range_id, json.dumps(merged), only_states=("ready",)):
            logger.info("[reconcile] range %s: %d setup(s) finalised", range_id, len(changes))
    if applied:
        range_accounts.record_applied(_db_session, range_id, applied)


@app.task(base=ReliableTask, bind=True, name="worker.setup_reconcile.reconcile_setups")
def reconcile_setups(self) -> dict:
    """Periodic: finish every ``running`` product / Security Onion setup that is ready to be."""
    from .tasks import _db_session

    with _db_session() as db:
        rows = db_ops_rows(db)
    done = 0
    for range_id, name, output_json, backend in rows:
        try:
            output = json.loads(output_json) if output_json else {}
        except (json.JSONDecodeError, TypeError):
            continue
        try:
            _reconcile_range(str(range_id), str(name or ""), output,
                             backend or os.getenv("PROVISIONER_BACKEND", "mock"))
            done += 1
        except Exception as exc:  # noqa: BLE001 — one range's trouble must not stop the rest
            logger.warning("[reconcile] range %s failed: %s", range_id, exc)
    return {"status": "ok", "ranges": done}


def db_ops_rows(db):
    from . import db_ops
    return db_ops.ranges_with_running_setups(db)
