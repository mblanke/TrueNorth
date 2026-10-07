"""Greyspace provisioning seam (ADR 0007): what the worker does with a range's Greyspace block.

``after_provision(range_id, backend, template)`` is meant to be called by
``tasks.provision_range`` once the range is ready, and ``after_destroy(range_id)`` by
``tasks.destroy_range`` once it is gone. Neither is wired yet (tasks.py is owned
elsewhere this slice); the one-line calls are in docs/adr/0007-greyspace.md. Both are
safe to call for every range: no block means nothing happens, and they never raise into
the range task, because a simulated internet must not fail a range build.

What "deploy" means per backend:

* ``mock``: the block is recorded as ``deployed`` with the services it would run. This
  is the dev and CI path; the real stack is exercised by greyspace/scripts/check-t0.sh.
* ``vsphere_api`` (and any other backend): ``pending_infrastructure``. TODO (after
  stage 4): clone one ``gs-core`` VM (golden image greyspace-host: ubuntu-lts + docker +
  nfs-common) on the range's Greyspace port group, mount the corpus read-only over NFS,
  copy the rendered stack (app/greyspace/config.py) and ``docker compose up``. That
  needs the post-deploy configure stage and multi-NIC routers (docs/greyspace-plan.md,
  slice 0).

A template that declares a ``greyspace:`` block attaches it here if the range has none,
so a range built from such a template gets Greyspace without an API call.
"""

from __future__ import annotations

import logging
import uuid
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
}
SERVICES = ("isp-a", "isp-b", "dns-root", "dns-tld", "dns-auth", "resolver", "webfarm")
GS_CORE_TODO = (
    "A gs-core VM is not built on this backend yet: clone the greyspace-host image on the range's "
    "Greyspace port group, mount the corpus read-only, start the rendered stack (ADR 0007)."
)


def template_block(template: dict | None) -> dict | None:
    """The template's ``greyspace:`` block with defaults filled in, or None.
    Unknown keys are dropped; the API validates blocks it is given (app/greyspace)."""
    raw = (template or {}).get("greyspace")
    if not isinstance(raw, dict):
        return None
    return {**DEFAULT_BLOCK, **{k: v for k, v in raw.items() if k in DEFAULT_BLOCK}}


def _now():
    return sa.func.now()


def _record_only(backend: str, block: dict) -> tuple[str, dict]:
    services = [*SERVICES, "threat"] if block.get("threat_infra", True) else list(SERVICES)
    return "deployed", {
        "backend": backend,
        "services": services,
        "corpus_tier": block.get("corpus_tier", "t0"),
        "note": "Mock backend: recorded, not started. greyspace/scripts/check-t0.sh runs the real stack.",
    }


def _no_greyspace_host(backend: str, block: dict) -> tuple[str, dict]:
    return "pending_infrastructure", {"backend": backend, "todo": GS_CORE_TODO}


# Provisioner backend -> how Greyspace is deployed with it (ADR 0001: a registry, not
# branches). vsphere_api gets its gs-core deployer here after stage 4.
DEPLOYERS = {"mock": _record_only}


def plan(backend: str, block: dict) -> tuple[str, dict]:
    """(status, detail) for deploying ``block`` on ``backend``."""
    return DEPLOYERS.get(backend, _no_greyspace_host)(backend, block)


def apply_after_provision(db, range_id: str, backend: str, template: dict | None) -> str | None:
    """Record the range's Greyspace deployment in ``db`` (no commit). Returns the new status,
    or None when the range has no block."""
    row = db.execute(sa.select(range_greyspace).where(range_greyspace.c.range_id == range_id)).first()
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
    else:
        block = dict(row.block or {})
    status, detail = plan(backend, block)
    db.execute(
        range_greyspace.update()
        .where(range_greyspace.c.range_id == range_id)
        .values(
            status=status,
            detail=detail,
            deployed_at=_now() if status == "deployed" else None,
            updated_at=_now(),
        )
    )
    return status


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
        return status
    except Exception:  # noqa: BLE001 — Greyspace must never fail a range build
        logger.exception("[greyspace] range %s: could not record the deployment", range_id)
        return None


def after_destroy(range_id: str) -> None:
    """The seam ``destroy_range`` calls. Commits its own session; never raises."""
    from .task_plumbing import db_session

    try:
        with db_session() as db:
            apply_after_destroy(db, range_id)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[greyspace] range %s: could not record the teardown", range_id)
