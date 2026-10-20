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
import time
import uuid
from contextlib import contextmanager

from celery.exceptions import SoftTimeLimitExceeded

from .base_tasks import ReliableTask
from .celery_app import app
from .db_ops import LIVE_RANGE_STATES
from .fencing import run_async

logger = logging.getLogger("truenorth.worker.setup_reconcile")

# Every state a built range can be in while its VMs exist. A stop/start moves a ready range
# to running/stopped and it never returns to ready, so reconcile follows it there; a
# destroying/destroyed/failed (or not yet built) range is left alone.
LIVE_STATES = LIVE_RANGE_STATES
# Guest waiting per range and per run, well inside the 5-minute beat and the task soft limit.
RANGE_SECONDS = float(os.getenv("SETUP_RECONCILE_RANGE_SECONDS", "240"))
RUN_SECONDS = float(os.getenv("SETUP_RECONCILE_RUN_SECONDS", "1200"))
LOCK_KEY = "truenorth:setup-reconcile:lock"
LOCK_SECONDS = int(RUN_SECONDS) + 300  # outlives a run; a dead worker's lock expires


@contextmanager
def _single_run(redis_client=None):
    """Yields True when this run holds the lock (Redis SET NX EX), False when another run does,
    so beat ticks never overlap. Without Redis (tests, a broken connection) the run proceeds:
    a missed lock must not stop setups from being finished; the beat's ``expires`` still bounds it."""
    token = uuid.uuid4().hex
    client = redis_client
    if client is None:
        try:
            import redis

            from .tasks import REDIS_URL
            client = redis.Redis.from_url(REDIS_URL, socket_timeout=5)
            acquired = bool(client.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[reconcile] no lock (%s); running without it", type(exc).__name__)
            client, acquired = None, True
    else:
        acquired = bool(client.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS))
    try:
        yield acquired
    finally:
        if client is not None and acquired:
            try:
                if (client.get(LOCK_KEY) or b"") in (token, token.encode()):
                    client.delete(LOCK_KEY)
            except Exception:  # noqa: BLE001 -- it expires on its own
                pass


def _running(output: dict) -> tuple[list[dict], list[dict]]:
    """(product VMs, Security Onion VMs) in the output whose setup is still ``running``."""
    vms = output.get("vms") or []
    products = [v for v in vms if (v.get("role_setup") or {}).get("status") == "running"
                and (v["role_setup"].get("reconcile"))]
    sos = [v for v in vms if (v.get("securityonion") or {}).get("status") == "running"
           and (v["securityonion"].get("reconcile"))]
    return products, sos


def _domain_passwords(declared: dict, products: list[dict]) -> dict:
    """DC node id -> its Administrator password: the range's own account (declared, or the
    per-range one the credential service generates for a range with product role images).
    Never the site-wide template default: one range's reconcile must not log in with a
    password another range shares. No account: no login (the setup ends by its timeout)."""
    out: dict[str, str] = {}
    for v in products:
        node = str((v["role_setup"]["reconcile"]).get("dc_node") or "")
        if not node or node in out:
            continue
        admin = next((a for a in declared.get(node, []) if a.username.casefold() == "administrator"), None)
        if admin is not None:
            out[node] = admin.password.reveal()
    return out


def _so_logins(declared: dict, sos: list[dict], env: dict) -> dict:
    """Security Onion node id -> (install user, install password, declared admin password or
    ""): the template build login from the environment, else the machine's declared admin
    account; the admin's declared password is what the reconcile sets and verifies."""
    out: dict[str, tuple[str, str, str]] = {}
    for v in sos:
        info = v["securityonion"]["reconcile"]
        node = str(info.get("node") or "")
        admin = next((a for a in declared.get(node, []) if a.admin), None)
        admin_pw = admin.password.reveal() if admin is not None else ""
        if info.get("install_from_env") and env.get("VSPHERE_SO_INSTALL_USER") and env.get("VSPHERE_SO_INSTALL_PASSWORD"):
            out[node] = (env["VSPHERE_SO_INSTALL_USER"], env["VSPHERE_SO_INSTALL_PASSWORD"], admin_pw)
        elif admin is not None:
            out[node] = (admin.username, admin_pw, admin_pw)
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
        # Merge onto what the build stored (web_url, management_mac, ...), and drop the
        # reconcile record: the setup is final.
        if "role_setup" in change:
            vm["role_setup"] = {**(vm.get("role_setup") or {}), **change["role_setup"]}
            vm["role_setup"].pop("reconcile", None)
            role = change.get("role") or ""
            if role:
                vm.setdefault("roles", {})[role] = {"status": change["role_setup"]["status"],
                                                    "detail": change["role_setup"]["detail"]}
        if "securityonion" in change:
            vm["securityonion"] = {**(vm.get("securityonion") or {}), **change["securityonion"]}
            vm["securityonion"].pop("reconcile", None)
            if change.get("accounts_applied"):
                vm["accounts_applied"] = sorted(set(vm.get("accounts_applied") or []) | set(change["accounts_applied"]))
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
    declared, lab = range_accounts.load(_db_session, range_id)
    if lab:
        # A lab session's range has no range credentials (random per VM) and no product role
        # images; never fall back to the site default password for it.
        logger.info("[reconcile] range %s is a lab session's: skipped", range_id)
        return
    provisioner = _get_backend(backend, range_id, output=output)
    if not getattr(provisioner, "supports_setup_reconcile", False):
        return
    changes, warnings = run_async(provisioner.reconcile_setups(
        range_id, output, domain_passwords=_domain_passwords(declared, products),
        so_logins=_so_logins(declared, sos, os.environ), run_seconds=RANGE_SECONDS))
    for w in warnings:
        logger.warning("[reconcile] range %s: %s", range_id, w)
    if not changes:
        return
    # Re-read under the row lock; write back, and mark the verified accounts applied, in the
    # same transaction, and only while the range is still in a live state.
    with _db_session() as db:
        db_ops.lock_range(db, range_id)
        row = db_ops.range_output_and_backend(db, range_id)
        current = json.loads(row[0]) if row and row[0] else output
        merged, applied = _merge(current, changes)
        if not db_ops.set_range_output(db, range_id, json.dumps(merged), only_states=LIVE_STATES):
            logger.info("[reconcile] range %s left the live states; nothing recorded", range_id)
            return
        if pairs := {(str(vm["node_id"]), str(n)) for vm in applied if vm.get("node_id")
                     for n in vm["accounts_applied"]}:
            db_ops.mark_accounts_applied(db, range_id, pairs)
        logger.info("[reconcile] range %s: %d setup(s) finalised", range_id, len(changes))


@app.task(base=ReliableTask, bind=True, name="worker.setup_reconcile.reconcile_setups")
def reconcile_setups(self) -> dict:
    """Periodic: finish every ``running`` product / Security Onion setup that is ready to be.
    One run at a time (``_single_run``); at most RUN_SECONDS of work per run."""
    from .tasks import _db_session

    with _single_run() as mine:
        if not mine:
            return {"status": "skipped", "reason": "another reconcile run holds the lock"}
        with _db_session() as db:
            rows = db_ops_rows(db)
        done, end = 0, time.monotonic() + RUN_SECONDS
        for range_id, name, output_json, backend in rows:
            if time.monotonic() >= end:
                break  # the next beat run picks up the rest
            try:
                output = json.loads(output_json) if output_json else {}
            except (json.JSONDecodeError, TypeError):
                continue
            try:
                _reconcile_range(str(range_id), str(name or ""), output,
                                 backend or os.getenv("PROVISIONER_BACKEND", "mock"))
                done += 1
            except SoftTimeLimitExceeded:
                raise  # the task's own limit: end it, never swallow it as one range's trouble
            except Exception as exc:  # noqa: BLE001 — one range's trouble must not stop the rest
                logger.warning("[reconcile] range %s failed: %s", range_id, exc)
        return {"status": "ok", "ranges": done}


def db_ops_rows(db):
    from . import db_ops
    return db_ops.ranges_with_running_setups(db)
