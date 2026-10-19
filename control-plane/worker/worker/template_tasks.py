"""Pruning of host-local golden-template copies (ADR 0012 decision 6).

``infra/vsphere/packer/build.sh replicate`` puts a copy of each template on every range
host as ``<id>@<host>-<yyyymmdd>``; the vSphere provisioner clones the newest copy on the
VM's host and records it as ``vms[].template`` in the range's provisioner_output. This
task removes, per template and host, the copies older than the newest one, unless a range
that is not destroyed records a VM cloned from it.

Contract:

* **Dry run by default.** ``prune_template_copies()`` only reports. Deleting needs
  ``dry_run=False`` (an operator: ``celery call worker.template_tasks.prune_template_copies
  --kwargs '{"dry_run": false}'``), or beat with ``TEMPLATE_COPY_PRUNE=delete``.
* **Never races a build.** While any range is ``provisioning`` a deleting run is downgraded
  to a dry run (``deferred``): a build may have resolved a copy that a newer one has since
  superseded. The newest copy on a host is never a candidate at all.
* **Conservative "in use".** The names recorded by every undestroyed range, on any
  vCenter, are kept. A range whose output cannot be read defers deleting, as a build does.
* **Every vCenter.** Each active vSphere hypervisor connection, and the worker's own
  VSPHERE_* environment when ``VSPHERE_URL`` is set, deduplicated by endpoint. One that
  fails is reported; the others still run.
* **Unsupported is explicit.** A backend without ``prune_template_copies`` is reported
  ``unsupported``, never treated as success.

Kept out of tasks.py (ADR 0003).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from . import db_ops
from .base_tasks import ReliableTask
from .celery_app import app
from .provisioners import CREDENTIALS_HYPERVISOR, get_provisioner
from .provisioners.template_copies import names_in_use
from .task_plumbing import db_session as _db_session

logger = logging.getLogger(__name__)

BACKEND = "vsphere_api"


def _output(raw) -> dict | None:
    try:
        out = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _provisioners(connections: list[dict]) -> list[tuple[str, object]]:
    """(label, provisioner) per distinct endpoint: the environment's, then each connection's."""
    out: list[tuple[str, object]] = []
    seen: set[str] = set()
    sources = ([{}] if os.getenv("VSPHERE_URL", "").strip() else []) + list(connections)
    for creds in sources:
        label = creds.get("host") or "VSPHERE_URL"
        try:
            provisioner = get_provisioner(BACKEND, creds or None, teardown=True)
        except Exception as exc:  # noqa: BLE001 -- reported for this endpoint only
            out.append((label, exc))
            continue
        record = getattr(provisioner, "connection_record", None)
        rec = record() if callable(record) else None
        key = json.dumps(rec if isinstance(rec, dict) else label, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(((rec or {}).get("host") or label, provisioner))
    return out


def run_prune(*, dry_run: bool = True) -> dict:
    """The work of ``prune_template_copies`` (no Celery: callable from an admin action)."""
    with _db_session() as db:
        rows = db_ops.undestroyed_range_outputs(db)
        connections = db_ops.active_connection_creds(db, CREDENTIALS_HYPERVISOR[BACKEND])
    outputs, unreadable, building = [], [], []
    for range_id, state, raw in rows:
        out = _output(raw)
        if out is None:
            unreadable.append(str(range_id))
        else:
            outputs.append(out)
        if str(getattr(state, "value", state)) == "provisioning":
            building.append(str(range_id))
    in_use = names_in_use(outputs)
    result: dict = {"requested_dry_run": dry_run, "in_use": sorted(in_use), "endpoints": {}}
    if not dry_run and (building or unreadable):
        dry_run = True
        why = [f"{len(building)} range(s) provisioning"] if building else []
        why += [f"{len(unreadable)} range output(s) unreadable"] if unreadable else []
        result["deferred"] = "; ".join(why) + "; nothing deleted this run"
    if unreadable:
        result["unreadable_outputs"] = unreadable
    result["dry_run"] = dry_run
    for label, provisioner in _provisioners(connections):
        if isinstance(provisioner, Exception):
            result["endpoints"][label] = {"error": str(provisioner)}
            continue
        prune = getattr(provisioner, "prune_template_copies", None)
        if not callable(prune):
            result["endpoints"][label] = {"unsupported": True}
            continue
        try:
            got = asyncio.run(prune(in_use, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 -- the other vCenters still run
            logger.error("[templates] pruning copies on %s failed: %s", label, exc)
            result["endpoints"][label] = {"error": str(exc)}
            continue
        result["endpoints"][label] = got
        logger.info("[templates] %s: %d stale copies%s, %d kept (in use), %d deleted, %d errors", label,
                    len(got.get("deletable", [])), " (dry run)" if dry_run else "", len(got.get("kept_in_use", [])),
                    len(got.get("deleted", [])), len(got.get("errors", [])))
    result["status"] = "partial" if any("error" in e or e.get("errors") for e in result["endpoints"].values()) \
        else "ok"
    return result


@app.task(base=ReliableTask, bind=True, name="worker.template_tasks.prune_template_copies")
def prune_template_copies(self, dry_run: bool = True) -> dict:
    """Remove superseded host-local template copies; reports only unless ``dry_run=False``."""
    return run_prune(dry_run=dry_run)
