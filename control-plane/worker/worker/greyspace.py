"""Greyspace provisioning seam (ADR 0007): what the worker does with a range's Greyspace block.

``after_provision(range_id, backend, template)`` is called by ``tasks.provision_range``
once the range is ready, and ``after_destroy(range_id)`` by ``tasks.destroy_range`` once
it is gone. Both are safe to call for every range: no block means nothing happens, and
they never raise into the range task, because a simulated internet must not fail a
range build.

What "deploy" means per backend (``DEPLOYERS``, ``HOSTED``):

* ``mock``: the block is recorded as ``deployed`` with the services it would run. This
  is the dev and CI path; the real stack is exercised by greyspace/scripts/check-t0.sh.
* ``vsphere_api``: a ``gs-core`` VM. ``plan_host`` (called by ``provision_range`` before
  the build) adds it to the rendered template (worker/greyspace_host.py: golden image
  greyspace-host, one NIC on the template's Greyspace network, cloud-init carrying the
  rendered stack), so the backend builds, places, tags and later destroys it like any
  range VM. ``after_provision`` marks the block ``configuring`` and sends the post-deploy
  ``configure_range`` task, which logs in through the backend's guest channel
  (``run_in_guest``: VMware guest operations), mounts the corpus, starts the stack, runs
  its health check and records ``deployed`` or ``failed``. Proven against vcsim and the
  T0 Docker stack, not a real vCenter (docs/greyspace-plan.md).
* any other backend: ``pending_infrastructure``.

Breadcrumbs (``deliver_breadcrumbs``, from the greyspace_breadcrumb injector through
inject_dispatch) go the same way: recorded on mock, ``bin/gs crumb ...`` on gs-core.

A template that declares a ``greyspace:`` block attaches it here if the range has none,
so a range built from such a template gets Greyspace without an API call.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from .tables import range_greyspace, ranges

logger = logging.getLogger("truenorth.worker.greyspace")

DEFAULT_BLOCK: dict[str, Any] = {
    "version": 1,
    "corpus_tier": "t0",
    "site_packs": None,
    "public_prefix": None,
    "npc_profile": "off",
    "threat_infra": True,
    "trust_ca": True,
    "network": "greyspace",
}
CONFIGURE_TIMEOUT = 1800  # seconds: Tools up, cloud-init done, stack started and healthy
CRUMB_TIMEOUT = 300


def template_block(template: dict | None) -> dict | None:
    """The template's ``greyspace:`` block with defaults filled in, or None.
    Unknown keys are dropped; the API validates blocks it is given (app/greyspace)."""
    raw = (template or {}).get("greyspace")
    if not isinstance(raw, dict):
        return None
    return {**DEFAULT_BLOCK, **{k: v for k, v in raw.items() if k in DEFAULT_BLOCK}}


def _now():
    return sa.func.now()


def _services(block: dict) -> list[str]:
    """The services the block's stack runs (rendered when the corpus is readable here)."""
    from . import greyspace_host

    try:
        return list(greyspace_host.render(block)[1].summary["services"])
    except Exception:  # noqa: BLE001 — a summary only; the configure stage renders for real
        base = ["isp-a", "isp-b", "dns-root", "dns-tld", "dns-auth", "resolver", "webfarm", "mail", "ntp"]
        return base + (["threat"] if block.get("threat_infra", True) else [])


def _record_only(backend: str, block: dict) -> tuple[str, dict]:
    return "deployed", {
        "backend": backend,
        "services": _services(block),
        "corpus_tier": block.get("corpus_tier", "t0"),
        "note": "Mock backend: recorded, not started. greyspace/scripts/check-t0.sh runs the real stack.",
    }


def _hosted(backend: str, block: dict) -> tuple[str, dict]:
    return "configuring", {"backend": backend, "note": "gs-core built with the range; the configure stage starts it."}


def _no_greyspace_host(backend: str, block: dict) -> tuple[str, dict]:
    return "pending_infrastructure", {
        "backend": backend,
        "todo": f"Greyspace has no host on the {backend} backend (gs-core is built on vsphere_api; ADR 0007).",
    }


# Provisioner backend -> how Greyspace is deployed with it (ADR 0001: a registry, not
# branches). HOSTED: backends that build a gs-core VM with the range and configure it
# through their guest channel afterwards.
DEPLOYERS = {"mock": _record_only, "vsphere_api": _hosted}
HOSTED = frozenset({"vsphere_api"})


def plan(backend: str, block: dict) -> tuple[str, dict]:
    """(status, detail) for deploying ``block`` on ``backend``."""
    return DEPLOYERS.get(backend, _no_greyspace_host)(backend, block)


def _row(db, range_id: str):
    return db.execute(sa.select(range_greyspace).where(range_greyspace.c.range_id == range_id)).first()


def _ensure_row(db, range_id: str, template: dict | None):
    """(row, block) of the range's Greyspace, attaching the template's block if it has none;
    None when there is no block at all."""
    row = _row(db, range_id)
    if row is None:
        block = template_block(template)
        if block is None:
            return None
        tenant = db.execute(sa.select(ranges.c.tenant_id).where(ranges.c.id == range_id)).scalar()
        if tenant is None:
            return None
        db.execute(
            range_greyspace.insert().values(
                range_id=uuid.UUID(str(range_id)),
                tenant_id=tenant,
                block=block,
                corpus_tier=block["corpus_tier"],
                status="configured",
                detail={"source": "template"},
                created_at=_now(),
                updated_at=_now(),
            )
        )
        row = _row(db, range_id)
    return row, {**DEFAULT_BLOCK, **dict(row.block or {})}


def _set(db, range_id: str, status: str, detail: dict) -> None:
    db.execute(
        range_greyspace.update()
        .where(range_greyspace.c.range_id == range_id)
        .values(status=status, detail=detail, deployed_at=_now() if status == "deployed" else None, updated_at=_now())
    )


def apply_after_provision(db, range_id: str, backend: str, template: dict | None) -> str | None:
    """Record the range's Greyspace deployment in ``db`` (no commit). Returns the new status,
    or None when the range has no block."""
    found = _ensure_row(db, range_id, template)
    if found is None:
        return None
    row, block = found
    status, detail = plan(backend, block)
    if backend in HOSTED:
        prior = dict(row.detail or {})
        if row.status == "failed" and prior.get("stage") == "plan":
            return "failed"  # plan_host said why gs-core could not be built
        if not prior.get("host"):
            status, detail = "failed", {
                "backend": backend, "stage": "plan",
                "error": "gs-core was not part of this build (the block was attached after it, or the range "
                         "rendered no VMs): provision the range again",
            }
        else:
            detail = {**prior, **detail, "stage": "configure"}
    _set(db, range_id, status, detail)
    return status


def plan_host(range_id: str, backend: str, template: dict) -> dict:
    """The rendered ``template`` with the range's gs-core VM added, on a backend in HOSTED
    with a Greyspace block; else ``template`` unchanged. Records why when gs-core cannot be
    planned (status ``failed``, ``stage: plan``) and builds the range anyway. Never raises."""
    if backend not in HOSTED or not template.get("vms"):
        return template
    from . import greyspace_host
    from .task_plumbing import db_session

    try:
        with db_session() as db:
            found = _ensure_row(db, range_id, template)
            if found is None:
                return template
            _, block = found
            try:
                vm, info = greyspace_host.host_vm(range_id, block, template)
            except greyspace_host.HostPlanError as exc:
                _set(db, range_id, "failed", {"backend": backend, "stage": "plan", "error": str(exc)})
                db.commit()
                logger.warning("[greyspace] range %s: no gs-core: %s", range_id, exc)
                return template
            out = {**template, "vms": [dict(v) for v in template["vms"]] + [vm]}
            info["router_vm"] = greyspace_host.route_router(out, info)
            if info["router_vm"] is None:
                info["warning"] = (f"no router holds {info['network']}'s gateway address {info['router']}: the range's "
                                   "zones have no route to the simulated internet")
            _set(db, range_id, "configuring", {"backend": backend, "stage": "build", **info})
            db.commit()
        logger.info("[greyspace] range %s: gs-core planned at %s on %s", range_id, info["ip"], info["network"])
        return out
    except Exception:  # noqa: BLE001 — Greyspace must never fail a range build
        logger.exception("[greyspace] range %s: could not plan gs-core", range_id)
        return template


def apply_after_destroy(db, range_id: str) -> bool:
    """The range's VMs are gone, so its Greyspace is no longer deployed. True if a row changed."""
    result = db.execute(
        range_greyspace.update()
        .where(range_greyspace.c.range_id == range_id)
        .values(status="configured", detail={"note": "Range destroyed."}, deployed_at=None, updated_at=_now())
    )
    return bool(result.rowcount)


def after_provision(range_id: str, backend: str, template: dict | None) -> str | None:
    """The seam ``provision_range`` calls. Commits its own session; never raises."""
    from .task_plumbing import db_session

    try:
        with db_session() as db:
            status = apply_after_provision(db, range_id, backend, template)
            db.commit()
        if status:
            logger.info("[greyspace] range %s: %s (backend=%s)", range_id, status, backend)
        if status == "configuring":
            send_configure(range_id)
        return status
    except Exception:  # noqa: BLE001 — Greyspace must never fail a range build
        logger.exception("[greyspace] range %s: could not record the deployment", range_id)
        return None


CONFIGURE_TASK = "configure_range"


def send_configure(range_id: str) -> bool:
    """Queue the post-deploy configure stage (worker/configure_tasks.py) by its contract."""
    from .contracts import TASKS, validate_args

    try:
        validate_args(CONFIGURE_TASK, [range_id])
        from .celery_app import app

        contract = TASKS[CONFIGURE_TASK]
        app.send_task(contract.qualified_name, args=[range_id], queue=contract.queue)
        return True
    except Exception:  # noqa: BLE001 — recorded as configuring; the range itself is fine
        logger.exception("[greyspace] range %s: could not queue the configure stage", range_id)
        return False


def _host_and_provisioner(db, range_id: str):
    """(gs-core's recorded VM, the range's backend name, its provisioner) or a reason string."""
    import json
    import os

    from . import db_ops, greyspace_host
    from .base_tasks import _get_backend

    out = db_ops.range_output_and_backend(db, range_id)
    if out is None:
        return "the range no longer exists"
    try:
        output = json.loads(out[0]) if out[0] else {}
    except (TypeError, ValueError):
        output = {}
    backend = out[1] or os.getenv("PROVISIONER_BACKEND", "mock")
    vm = next((v for v in output.get("vms") or [] if isinstance(v, dict) and v.get("node_id") == greyspace_host.HOST_NODE),
              None)
    if vm is None or not vm.get("vm_id"):
        return "gs-core is not among the range's built VMs"
    return vm, backend, _get_backend(backend, range_id)


def configure(range_id: str) -> str:
    """The post-deploy configure stage for the range's Greyspace (task ``configure_range``):
    on gs-core, wait for cloud-init, mount the corpus, start the stack, check its health.
    Records ``deployed`` or ``failed`` with every step's outcome. Never raises."""
    from . import greyspace_host
    from .fencing import run_async
    from .task_plumbing import db_session

    try:
        with db_session() as db:
            row = _row(db, range_id)
            if row is None:
                return "not_attached"
            found = _host_and_provisioner(db, range_id)
        prior = dict(row.detail or {})
        if isinstance(found, str):
            status, detail = "failed", {**prior, "stage": "configure", "error": found}
        else:
            vm, backend, provisioner = found
            if not getattr(provisioner, "supports_guest_commands", False):
                status, detail = "failed", {**prior, "stage": "configure",
                                            "error": f"the {backend} backend cannot run commands in gs-core"}
            else:
                try:
                    steps = run_async(provisioner.run_in_guest(
                        vm["vm_id"], greyspace_host.login(range_id), greyspace_host.configure_steps(), CONFIGURE_TIMEOUT))
                    ok = bool(steps) and all(s["status"] == "ok" for s in steps)
                    status = "deployed" if ok else "failed"
                    detail = {**prior, "stage": "configure", "vm_id": vm["vm_id"], "configure": steps,
                              "configured_at": datetime.now(UTC).isoformat()}
                    if not ok:
                        bad = next(s for s in steps if s["status"] != "ok")
                        detail["error"] = f"step {bad['label']} {bad['status']} (exit {bad['exit_code']})"
                except Exception as exc:  # noqa: BLE001 — recorded, not raised
                    status, detail = "failed", {**prior, "stage": "configure", "vm_id": vm["vm_id"],
                                                "error": (str(exc) or type(exc).__name__)[:500]}
        detail.pop("note", None)
        with db_session() as db:
            _set(db, range_id, status, detail)
            db.commit()
        logger.info("[greyspace] range %s: configure -> %s", range_id, status)
        return status
    except Exception:  # noqa: BLE001
        logger.exception("[greyspace] range %s: the configure stage failed unexpectedly", range_id)
        return "failed"


def _crumbs_recorded(range_id: str, op: dict) -> tuple[bool, str]:
    return True, "recorded (mock backend: no Greyspace host)"


def _crumbs_on_host(range_id: str, op: dict) -> tuple[bool, str]:
    from . import greyspace_host
    from .fencing import run_async
    from .task_plumbing import db_session

    with db_session() as db:
        row = _row(db, range_id)
        if row is not None and row.status != "deployed":
            return False, f"the range's Greyspace is {row.status}, not deployed"
        found = _host_and_provisioner(db, range_id)
    if isinstance(found, str):
        return False, found
    vm, backend, provisioner = found
    steps = run_async(provisioner.run_in_guest(vm["vm_id"], greyspace_host.login(range_id),
                                               greyspace_host.crumb_steps(op), CRUMB_TIMEOUT))
    bad = next((s for s in steps if s["status"] != "ok"), None)
    if bad:
        return False, f"gs-core: {bad['label']} {bad['status']} (exit {bad['exit_code']})"
    return True, f"{op.get('operation')} on gs-core"


# Backend -> how breadcrumbs reach the range's Greyspace (ADR 0001 registry).
CRUMB_CHANNELS = {"mock": _crumbs_recorded, "vsphere_api": _crumbs_on_host}


def deliver_breadcrumbs(range_id: str, backend: str, op: dict) -> tuple[bool, str]:
    """Deliver a greyspace_breadcrumb injector's operation (``plant`` with a payload, or
    ``remove``) to the range's Greyspace, and keep a record of it on the range's Greyspace
    row (``detail.breadcrumbs``, the last 50). (ok, what happened). Never raises."""
    from .task_plumbing import db_session

    try:
        with db_session() as db:
            if _row(db, range_id) is None:
                return False, "the range has no Greyspace block attached"
        channel = CRUMB_CHANNELS.get(backend)
        if channel is None:
            return False, f"Greyspace breadcrumbs are not delivered on the {backend} backend"
        ok, what = channel(range_id, op)
        crumbs = op.get("payload", {}).get("crumbs") or []
        entry = {"at": datetime.now(UTC).isoformat(), "operation": op.get("operation"), "exercise": op.get("exercise"),
                 "ids": [c.get("id") for c in crumbs] if crumbs else op.get("ids"), "ok": ok, "detail": what}
        with db_session() as db:
            row = _row(db, range_id)
            if row is not None:
                detail = dict(row.detail or {})
                detail["breadcrumbs"] = [*(detail.get("breadcrumbs") or []), entry][-50:]
                db.execute(range_greyspace.update().where(range_greyspace.c.range_id == range_id)
                           .values(detail=detail, updated_at=_now()))
                db.commit()
        return ok, what
    except Exception as exc:  # noqa: BLE001 — an inject failure is recorded by the caller
        logger.exception("[greyspace] range %s: breadcrumb delivery failed", range_id)
        return False, f"breadcrumb delivery failed: {(str(exc) or type(exc).__name__)[:300]}"


def after_destroy(range_id: str) -> None:
    """The seam ``destroy_range`` calls. Commits its own session; never raises."""
    from .task_plumbing import db_session

    try:
        with db_session() as db:
            apply_after_destroy(db, range_id)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[greyspace] range %s: could not record the teardown", range_id)
