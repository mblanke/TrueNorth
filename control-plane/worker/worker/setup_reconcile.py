"""Finish long in-guest setups a build left ``running`` (ADR 0012).

A product role image (Exchange 2019 / SharePoint (SE, 2019) / MECM) and Security Onion each take 20 min to
2 h in the guest, far longer than a provision may run before the Celery soft time limit
(worker/fencing.py). So the build starts the setup, records it ``running`` with the status
file to read and when it started, and finishes within its budget. This periodic task then
reads each ``running`` setup's status in its guest and records the real outcome
(``ok`` / ``failed`` / ``timeout``), replacing the "being set up" warning. Accounts a setup
sets (the SOC web login, the range admin) are marked ``applied`` only once verified here.

Secrets are never stored in the range output: the domain Administrator password is re-read
from the range's credentials each run (worker/range_accounts.py), used only for the
guest-operations auth, and the Security Onion install login is the range's own ``soadmin``
row once its password is set (ADR 0013 §3), else the template's from the environment, else
the range's admin account. The write back is conditional on the range still being ``ready`` and
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
LOCK_MARGIN = 300


def lock_ttl() -> int:
    """The lock outlives the longest a run can last: the task's soft time limit (and RUN_SECONDS),
    plus a margin, so a slow run never loses it to the next beat; a dead worker's lock expires."""
    soft = app.conf.task_soft_time_limit or 0
    return int(max(soft, RUN_SECONDS)) + LOCK_MARGIN


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
            acquired = bool(client.set(LOCK_KEY, token, nx=True, ex=lock_ttl()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[reconcile] no lock (%s); running without it", type(exc).__name__)
            client, acquired = None, True
    else:
        acquired = bool(client.set(LOCK_KEY, token, nx=True, ex=lock_ttl()))
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


READINESS_CHECKS = ("ip_check", "customization")  # vsphere_readiness, vsphere_customization (#255)
NOT_YET = ("guest network not verified yet", "guest customization (Sysprep) not finished yet")


def _readiness_running(vm: dict) -> bool:
    return any((vm.get(key) or {}).get("status") == "running" for key in READINESS_CHECKS)


def _ip_pending(output: dict) -> list[dict]:
    """VMs whose guest network check, or (Windows) guest customization check, the build left
    ``running`` (its budget ended the wait; provisioners/vsphere_readiness.py,
    vsphere_customization.py): settled here like a long setup."""
    return [v for v in output.get("vms") or [] if _readiness_running(v)]


def _merge_ips(output: dict, settled: dict) -> list[str]:
    """Store the settled readiness checks on their VMs; drop their "not verified yet"
    warning once none is still running. Returns the reasons of the VMs that are not ready
    (they fail the range)."""
    warnings = list(output.get("warnings") or [])
    failed: list[str] = []
    for vm in output.get("vms") or []:
        change = settled.get(vm.get("name"))
        if not change:
            continue
        reason = change.pop("not_ready", "")
        vm.update(change)
        if not _readiness_running(vm):
            warnings = [w for w in warnings if not (w.startswith(f"VM {vm.get('name')}: ")
                                                    and any(t in w for t in NOT_YET))]
        if reason:
            failed.append(f"VM {vm.get('name')}: {reason}")
            output["not_ready"] = sorted(set(output.get("not_ready") or []) | {vm.get("name")})
    output["warnings"] = warnings + failed
    return failed


def _merge_template_logins(output: dict, changes: dict) -> list[str]:
    """The template-login checks a reconcile made (a Security Onion node, once its setup
    changed ``soadmin``; ADR 0013 §3), as the build would record them: an ``accepted`` one is
    Not safe, and under RANGE_TEMPLATE_LOGIN_CHECK=enforce its VM is not ready (it fails the
    range, as a build would have). Returns those not-ready reasons."""
    from .provisioners import vsphere_credcheck as credcheck

    warnings = list(output.get("warnings") or [])
    failed: list[str] = []
    for vm in output.get("vms") or []:
        result = (changes.get(vm.get("name")) or {}).get("template_login")
        if not result:
            continue
        if reason := credcheck.report(str(vm.get("name")), result, warnings):
            failed.append(f"VM {vm.get('name')}: {reason}")
            output["not_ready"] = sorted(set(output.get("not_ready") or []) | {vm.get("name")})
    output["warnings"] = warnings + failed
    return failed


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


def _so_logins(declared: dict, sos: list[dict], env: dict,
               applied: set | frozenset = frozenset()) -> tuple[dict, list[str]]:
    """(Security Onion node id -> (install user, install password, admin password or "",
    the build login's range password or ""), warnings).

    The install login is the build login (``soadmin``): with its range password once that is
    set (its row ``applied``: ADR 0013 §3), else the template's from the environment
    (VSPHERE_SO_INSTALL_*), else the machine's admin account. The admin's password is what
    the reconcile verifies; the build login's range password is what it sets last.

    While the build login still has the template password (``admin_last``), its range
    password cannot log in: without VSPHERE_SO_INSTALL_* on this worker that node is skipped
    with a warning, never tried."""
    out: dict[str, tuple[str, str, str, str]] = {}
    warnings: list[str] = []
    for v in sos:
        info = v["securityonion"]["reconcile"]
        node = str(info.get("node") or "")
        accounts = declared.get(node, [])
        login_name = info.get("login_user") or (info.get("admin_user") if info.get("admin_last") else None)
        login = next((a for a in accounts if a.username == login_name), None) if login_name else None
        admin_name = info.get("admin_user")
        admin = (next((a for a in accounts if a.username == admin_name), None) if admin_name and admin_name != login_name
                 else None) if admin_name else next((a for a in accounts if a.admin), None)
        admin_pw = admin.password.reveal() if admin is not None else ""
        login_pw = login.password.reveal() if login is not None else ""
        if login is not None and (node, login.username) in applied:
            out[node] = (login.username, login_pw, admin_pw, "")  # already set: log in with it
        elif info.get("install_from_env") and env.get("VSPHERE_SO_INSTALL_USER") and env.get("VSPHERE_SO_INSTALL_PASSWORD"):
            out[node] = (env["VSPHERE_SO_INSTALL_USER"], env["VSPHERE_SO_INSTALL_PASSWORD"], admin_pw, login_pw)
        elif info.get("admin_last"):
            warnings.append(f"VM {v.get('name')}: Security Onion reconcile skipped: its admin is the build login, "
                            "and VSPHERE_SO_INSTALL_USER / VSPHERE_SO_INSTALL_PASSWORD are not set on this worker")
        elif admin is not None:
            out[node] = (admin.username, admin_pw, admin_pw, "")
    return out, warnings


def _expired(products: list[dict], sos: list[dict], now: float) -> dict:
    """Terminal ``timeout`` changes for every running setup past its timeout, worked out from
    the stored record alone: no backend, vCenter connection or guest operation, so an
    unreachable vCenter or a screening refusal still ends them."""
    out: dict[str, dict] = {}
    for key, vms in (("role_setup", products), ("securityonion", sos)):
        for v in vms:
            info = v[key]["reconcile"]
            timeout = float(info.get("timeout") or 0)
            if timeout and now - float(info.get("since") or 0) > timeout:
                change = {key: {"status": "timeout", "detail": f"not finished within its setup timeout "
                                                                f"({int(timeout)}s); see {info.get('status_file', '')} "
                                                                "in the guest"}}
                if key == "role_setup":
                    change["role"] = info.get("role", "")
                else:
                    change.update(accounts_applied=[], node=info.get("node"))
                out[v["name"]] = change
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
            info = (vm.get("securityonion") or {}).get("reconcile") or {}
            login = info.get("login_user") or info.get("admin_user")
            if info.get("admin_last") and login and login in (change.get("accounts_applied") or []):
                # The reconcile gave the build login its range password.
                warnings = [w for w in warnings if not (w.startswith(f"VM {vm.get('name')}: ")
                                                        and f"admin account {login} (also the build login)" in w)]
            if change.get("template_login"):  # ADR 0013 §3: checked once the login changed
                vm["template_login"] = change["template_login"]
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
    from .tasks import _get_backend

    products, sos = _running(output)
    ip_pending = _ip_pending(output)
    if not products and not sos and not ip_pending:
        return
    # Timeouts first, from the record alone: they end even if what follows cannot run.
    changes: dict[str, dict] = _expired(products, sos, time.time())
    ip_settled: dict[str, dict] = {}
    if ip_pending:  # one Tools read per VM; no guest login, so a lab session's range too
        try:
            settle = getattr(_get_backend(backend, range_id, output=output), "reconcile_guest_ips", None)
            if callable(settle):
                ip_settled = run_async(settle(range_id, output))
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — retried next beat; the window's end is in the record
            logger.warning("[reconcile] range %s: guest network check not possible this round: %s", range_id, exc)
    if products or sos:
        _reconcile_guest_setups(range_id, output, backend, products, sos, changes)
    if not changes and not ip_settled:
        return
    _record(range_id, output, changes, ip_settled)


def _reconcile_guest_setups(range_id: str, output: dict, backend: str, products: list[dict], sos: list[dict],
                            changes: dict) -> None:
    """Read the running product / Security Onion setups in their guests into ``changes``."""
    from . import db_ops, range_accounts
    from .tasks import _db_session, _get_backend

    # Every range has its own accounts now, a lab session's included (ADR 0013 §3).
    declared = range_accounts.load(_db_session, range_id)
    with _db_session() as db:
        applied = {(r["machine"], r["username"]) for r in db_ops.account_rows(db, range_id) if r["applied"]}
    so_logins, notes = _so_logins(declared, sos, os.environ, applied)
    try:
        provisioner = _get_backend(backend, range_id, output=output)
        if getattr(provisioner, "supports_setup_reconcile", False):
            found, warnings = run_async(provisioner.reconcile_setups(
                range_id, output, domain_passwords=_domain_passwords(declared, products),
                so_logins=so_logins, run_seconds=RANGE_SECONDS))
            changes.update(found)  # a guest-read outcome (and its clean-up) wins over the record's
            notes += warnings
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 — vCenter unreachable, connection gone: timeouts still recorded
        notes.append(f"guest reconcile not possible this round: {type(exc).__name__}: {exc}")
    for w in notes:
        logger.warning("[reconcile] range %s: %s", range_id, w)


def _record(range_id: str, output: dict, changes: dict, ip_settled: dict) -> None:
    """Re-read under the row lock; write back, and mark the verified accounts applied, in the
    same transaction, and only while the range is still in a live state."""
    from . import db_ops
    from .tasks import _db_session

    with _db_session() as db:
        db_ops.lock_range(db, range_id)
        row = db_ops.range_output_and_backend(db, range_id)
        current = json.loads(row[0]) if row and row[0] else output
        merged, applied = _merge(current, changes)
        not_ready = _merge_ips(merged, ip_settled) + _merge_template_logins(merged, changes)
        if not db_ops.set_range_output(db, range_id, json.dumps(merged), only_states=LIVE_STATES):
            logger.info("[reconcile] range %s left the live states; nothing recorded", range_id)
            return
        if not_ready:
            # As a build that waited it out would have (range_alloc.build_outcome): failed, with
            # the reason. Only from ready: a range Students have started keeps running, and
            # says why in its warnings.
            error = f"{len(not_ready)} VM(s) built but not ready: " + "; ".join(not_ready)
            if db_ops.update_range_state(db, range_id, "failed", error=error, only_from=("ready",)):
                from .tasks import _notify_api
                _notify_api("range", {"id": range_id, "state": "failed", "error": error})
            logger.warning("[reconcile] range %s: %s", range_id, error)
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
